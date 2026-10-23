"""The investigation loop: the LLM chooses one tool at a time based on what it has
found so far, then submits a diagnosis that is checked against the tool outputs."""
import json
import re
import time
from pathlib import Path

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from . import config, evidence
from .tools import TOOL_SCHEMAS, ToolRunner

SYSTEM_PROMPT = """You are an experienced SRE acting as the on-call engineer for the OpenTelemetry \
Astronomy Shop, a microservices e-commerce app (frontend, frontend-proxy, cart + valkey-cart cache, \
checkout, payment, product-catalog + astronomy-db, recommendation, ad, shipping, quote, currency, email, \
image-provider, load-generator). Kafka, accounting and fraud-detection are NOT deployed, so otel-collector \
log errors about Kafka brokers are expected background noise. An alert has fired. Investigate with the tools and find the ROOT CAUSE.

How to work:
1. Start broad: get_service_overview and get_container_health.
2. Form a hypothesis, then call the tools that best test it. Request independent tools IN THE SAME STEP \
(several tool calls in one reply, e.g. get_service_overview + get_container_health together) to save steps; \
do not fetch everything. Aim to submit within 5-6 steps.
3. Separate symptom from cause. Errors usually propagate upstream: if services A and B fail because their \
dependency C fails, the root cause component is C. Follow failing_outgoing_calls and error spans down \
the call chain to the deepest failing hop. A dependency that is stopped emits no spans of its own; its \
callers show connection errors instead, so check container health.
4. Confirm with at least two independent sources (e.g. metrics + traces, traces + logs, container state + metrics).
5. Consider infrastructure as well as application faults: stopped or crash-looping containers, cache/database \
unavailable, CPU or memory starvation (check cpu_limit_cores and resource trends), memory leaks (steadily \
rising memory), network latency between services (slow client spans while the server side is fast), and \
traffic floods (request rate far above baseline).
6. Partial / intermittent failures matter: a 10% error rate on one operation is a real incident.

Rules:
- Use only facts returned by tools. Never invent trace IDs, numbers or log lines.
- Each evidence item cites the tool_call_id shown in the tool result header and a quote copied VERBATIM \
from that output (a short exact substring such as "error_rate": 0.25 or an exact status_message). \
A quote is ONE contiguous excerpt: never join fragments with "..." and never reformat numbers; use \
several evidence items instead.
- The root cause is something that CHANGED. Read anomalies_ranked in get_service_overview and investigate \
every service with NEW ERRORS (error spans and logs) before concluding, even if latency elsewhere looks \
dramatic. A problem that was already present in the baseline window, or a container with an old restart \
history, is pre-existing background unless it clearly got worse.
- If several problems exist, the root cause is the one that explains the alert. Mention unrelated \
pre-existing problems in ruled_out or symptoms rather than blaming them.
- Confidence must be calibrated. If evidence is thin or contradictory, use status "escalate" with what you \
found; a correct escalation beats a confident wrong answer. If everything is healthy, use "no_incident".
- Do not guess causes you cannot see. A container that exited with exit_code 0 and oom_killed false was \
stopped cleanly; say the reason for the stop is unknown rather than inventing one.
- You are in read-only investigation mode. Remediation is chosen in a separate step after your diagnosis, \
and nothing is executed without human approval.
- Finish by calling submit_diagnosis exactly once."""

PLANNER_PROMPT = """You choose a remediation for a diagnosed production incident. The diagnosis below is \
final and was made from telemetry only. Pick exactly ONE action from the allowlisted catalog, or "none".

Runbook:
- service_down / crash_loop -> restart_service on the root-cause component.
- memory_leak -> restart_service on the component (temporary mitigation; say so).
- traffic_spike -> throttle_load_generator with a lower user count.
- application_error / dependency_unavailable that started recently -> toggle_feature_flag ONLY if one flag \
in the catalog clearly matches BOTH the root-cause component and the observed failure; otherwise none.
- network_latency, resource_starvation, cpu_saturation, unknown -> none (no allowlisted action fixes these here).
Prefer "none" (escalate to a human) over an action you are not confident in.
The target must appear exactly in the catalog. Call choose_action once."""

PLANNER_TOOL = {"type": "function", "function": {
    "name": "choose_action",
    "description": "Select one allowlisted remediation, or none.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["restart_service", "toggle_feature_flag", "throttle_load_generator", "none"]},
        "target": {"type": "string"},
        "parameters": {"type": "object", "description": "e.g. {\"variant\": \"off\"} or {\"users\": 2}"},
        "reason": {"type": "string", "description": "Short reason tied to the diagnosis"},
        "expected_outcome": {"type": "string", "description": "What recovery should look like in telemetry"}},
        "required": ["action", "reason"]}}}


EXHAUSTED_FILE = Path(__file__).resolve().parent.parent / "reports" / ".llm_exhausted.json"


def _clients() -> list[OpenAI]:
    """One client per key; LLM_API_KEY may hold several comma-separated keys (e.g. one per teammate)."""
    keys = [k.strip() for k in (config.LLM_API_KEY or "").split(",") if k.strip()]
    if not keys:
        raise SystemExit("No LLM key: put LLM_API_KEY=... in botch_analyst_tool/.env")
    return [OpenAI(base_url=config.LLM_BASE_URL, api_key=k, max_retries=0, timeout=60) for k in keys]


def _load_exhausted() -> dict:
    try:
        data = json.loads(EXHAUSTED_FILE.read_text())
        return {k: v for k, v in data.items() if v > time.time()}
    except (OSError, ValueError):
        return {}


def _mark_exhausted(route: str, err: Exception):
    """Remember a (key, model) whose daily quota is used up, until Google's retry time."""
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", str(err))
    wait = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)) if m and m.group(0) != "retry in " else 3600
    data = _load_exhausted()
    data[route] = time.time() + max(wait, 60)
    EXHAUSTED_FILE.parent.mkdir(exist_ok=True)
    EXHAUSTED_FILE.write_text(json.dumps(data))


def _complete(clients: list[OpenAI], log, **kwargs):
    """Try every (key, model) route in order. Quota-exhausted routes are skipped until they reset;
    overloaded ones (503) get a short retry before moving on."""
    exhausted = _load_exhausted()
    for model in [config.LLM_MODEL, *config.LLM_FALLBACK_MODELS]:
        for ki, client in enumerate(clients):
            route = f"key{ki + 1}:{model}"
            if route in exhausted:
                continue
            for attempt in range(2):
                try:
                    return client.chat.completions.create(model=model, **kwargs), route
                except (APIStatusError, APITimeoutError, APIConnectionError) as e:
                    code = getattr(e, "status_code", None)
                    if code not in (None, 429, 500, 503):
                        raise
                    if code == 429:
                        log(f"  [llm] {route} quota used up, skipping it until it resets")
                        _mark_exhausted(route, e)
                        break
                    log(f"  [llm] {route} unavailable ({code or type(e).__name__}), retry {attempt + 1}/2")
                    if isinstance(e, APITimeoutError):
                        break  # a slow model stays slow; move on instead of waiting again
                    time.sleep(4 * (attempt + 1))
    raise RuntimeError("All configured LLM keys/models are unavailable or out of quota right now")


def investigate(alert: str, log=print) -> dict:
    clients = _clients()
    runner = ToolRunner()
    timeline = [{"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": f"alert received: {alert}"}]
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"ALERT: {alert}\nInvestigate and submit your diagnosis."}]
    diagnosis = None
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    models_used = set()

    for step in range(1, config.MAX_AGENT_STEPS + 1):
        last = step == config.MAX_AGENT_STEPS
        resp, model_used = _complete(
            clients, log, messages=messages, tools=TOOL_SCHEMAS, temperature=0.1,
            tool_choice={"type": "function", "function": {"name": "submit_diagnosis"}} if last else "auto")
        models_used.add(model_used)
        if resp.usage:
            usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            usage["completion_tokens"] += resp.usage.completion_tokens or 0
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if msg.content:
            log(f"  [think] {msg.content.strip()[:300]}")

        if not msg.tool_calls:
            messages.append({"role": "user", "content": "Continue investigating with tools, or call submit_diagnosis."})
            continue

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if tc.function.name == "submit_diagnosis":
                diagnosis = args
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": "received"})
                continue
            call_id, text = runner.run(tc.function.name, args)
            log(f"  [{call_id}] {tc.function.name}({json.dumps(args)}) -> {len(text)} chars")
            timeline.append({"time": runner.calls[-1]["time"], "event": f"{call_id} {tc.function.name} {json.dumps(args)}"})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": f"[tool_call_id={call_id}]\n{text}"})
        if diagnosis is not None:
            break

    if diagnosis is None:
        diagnosis = {"status": "escalate", "summary": "Agent ran out of steps without a diagnosis.",
                     "root_cause": {"component": "unknown", "category": "unknown", "description": "not determined"},
                     "confidence": 0.0, "evidence": []}

    check = evidence.verify(diagnosis, runner)
    timeline.append({"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "event": f"diagnosis submitted: {check['status']} (confidence {check['confidence']})"})

    return {"alert": alert, "diagnosis": diagnosis, "verification": check, "timeline": timeline,
            "tool_calls": runner.calls, "model": ", ".join(sorted(models_used)) or config.LLM_MODEL,
            "usage": usage, "steps": step}


def plan_action(diagnosis: dict, catalog: dict, log=print) -> dict:
    """Second, separate LLM step: map the locked diagnosis onto the allowlisted catalog.
    The catalog (which names the flags) is only shown here, after diagnosis is finished."""
    slim = {"root_cause": diagnosis.get("root_cause"), "summary": diagnosis.get("summary"),
            "symptoms": diagnosis.get("symptoms", [])}
    usable = {k: {"targets": v["targets"], "parameters": v["parameters"]}
              for k, v in catalog.items() if not v.get("unsupported")}
    messages = [{"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": f"DIAGNOSIS:\n{json.dumps(slim)}\n\nCATALOG:\n{json.dumps(usable)}"}]
    resp, route = _complete(_clients(), log, messages=messages, tools=[PLANNER_TOOL], temperature=0,
                            tool_choice={"type": "function", "function": {"name": "choose_action"}})
    calls = resp.choices[0].message.tool_calls or []
    choice = json.loads(calls[0].function.arguments or "{}") if calls else {"action": "none", "reason": "planner gave no choice"}
    choice.setdefault("parameters", {})
    choice["planned_by"] = route
    return choice

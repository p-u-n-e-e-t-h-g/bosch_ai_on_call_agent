# Incident Zero — AI On-Call Engineer

An agent that responds to incidents in the OpenTelemetry Demo (Astronomy Shop). It detects an alert,
investigates with read-only telemetry tools, names the root cause with verified evidence and a confidence
level, proposes an allowlisted fix, waits for human approval, executes it through the Action API, verifies
recovery from telemetry, and rolls back + escalates if recovery fails.

## Setup

1. Start the demo with its observability and AI layers (from `../opentelemetry-demo`):
   ```
   docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.observability.yaml -f compose.agent.yaml -f compose.extras.yaml up --remove-orphans --detach
   ```
   `compose.agent.yaml` adds the demo's AI shopping assistant (`chatbot` → `agent` → `mcp`). It replays recorded
   LLM responses (`USE_VCR=True` in the demo's `.env`), so it needs no API key. Without it, the load generator's
   chatbot requests (`POST /prompt`) fail constantly and there are no AI traces in Jaeger.
2. Python 3.11:
   ```
   py -3.11 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
3. Create `.env` (never committed):
   ```
   LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
   LLM_API_KEY=key1,key2          # one or more keys, comma-separated (rotated on quota errors)
   LLM_MODEL=gemini-3.8-flash
   LLM_FALLBACK_MODELS=gemini-3.7-flash,gemini-3.5-flash,gemini-3.1-flash-lite,gemini-3.5-flash-lite
   ```
   Any OpenAI-compatible endpoint works (OpenRouter, Groq, local Ollama).

## How to run

Terminal 1 — the Action API (the only component that can change the system) and the approval UI:
```
.venv\Scripts\python -m action_api
```
Open **http://127.0.0.1:8090/** to approve or reject actions, engage the kill switch and read the action log.

Terminal 2 — the agent:
```
.venv\Scripts\python -m incident_agent watch                   # real-time: keeps watching, handles each incident
.venv\Scripts\python -m incident_agent watch --once            # stops after the first incident
.venv\Scripts\python -m incident_agent watch --once --dry-run  # plans + validates the action, changes nothing
.venv\Scripts\python -m incident_agent investigate --no-act    # diagnosis only
.venv\Scripts\python -m incident_agent killswitch on|off|status
.venv\Scripts\python -m incident_agent actions                 # Action API log
.venv\Scripts\python -m incident_agent tools                   # test every telemetry tool, no LLM
```
Practice incidents (humans only; the agent never uses this):
```
.venv\Scripts\python faultctl\faultctl.py list | inject <name> | reset
```

## Tests

```
.venv\Scripts\python -m pytest tests/test_unit.py -q     # 85 fast tests, no Docker or LLM needed
.venv\Scripts\python -m tests.e2e_scenarios              # 8 live safety scenarios against the running demo
.venv\Scripts\python -m tests.llm_runs                   # real agent + real LLM on injected incidents
```
- `test_unit.py`: evidence check, Action API allowlist/approval/dry run/kill switch/cooldown/blast radius/rollback
  (Docker faked), approval records (single use, bound to the exact action, UI decision, timeout), prompt
  injection, LLM-unavailable fallback, flag-text redaction, report.
- `e2e_scenarios.py`: injects real faults and drives the real alert rules, tools, evidence check, Action API,
  approval prompt, execution, recovery verification and rollback. Only the LLM's diagnosis/plan is scripted, and
  its evidence must still quote real tool output. Reports go to `reports/e2e/`.
- `llm_runs.py`: runs the real CLI (`watch --once`) with the real LLM; approval is piped as `yes` and recorded
  as `claude-test-run`. The expected answers are only used to score the report afterwards.

Both live runners need the Action API; set `ACTION_API_URL` if it is not on the default port 8090.

## The loop

| Step | What happens | Where |
|---|---|---|
| 1 Detect | Threshold rules on Prometheus span metrics + Docker state (error rate vs baseline, per-operation errors, latency x2 vs baseline, traffic drop/spike, container down/unhealthy; container alerts listed first) | `telemetry.check_alerts` |
| 2 Investigate | The LLM picks one or more of 10 read-only tools per step, based on what it found so far | `agent.investigate`, `tools.py` |
| 3 Diagnose | Component, category, confidence, evidence. Every evidence quote must appear verbatim in the cited tool output; unverified quotes are dropped and lower confidence. Below 0.6 → escalate | `evidence.py` |
| 4 Plan | A *separate* LLM call maps the locked diagnosis onto the Action API catalog. It is only offered the actions the runbook permits for the diagnosis category (enforced in code); "none" = escalate. Then live-state checks: a "service down" diagnosis on a running, healthy container is refused, and an incident that already cleared ends as `resolved_without_action` | `agent.plan_action`, `responder.RUNBOOK` |
| 5 Approve | Diagnosis, evidence, action, expected outcome and rollback plan are registered with the Action API and shown in the browser UI and the terminal. The first human answer wins: **Approve** in the UI or `yes` in the terminal. No answer within 10 min (`APPROVAL_TIMEOUT_S`) = not approved | `responder._request_approval`, `action_api/ui.html` |
| 6 Execute | `POST /execute` with the approval id; the API checks it is approved, unused, and for exactly this incident/action/target/parameters | `action_api/server.py` |
| 7 Verify | Telemetry is polled until every error/traffic alert present at action time, and every alert about the component, is clear for 2 polls in a row (timeout 5 min). If the component recovered but other alerts remain → `partially_recovered_escalated` (fix kept). Otherwise → rollback + escalate | `responder.verify_recovery` |
| 8 Report | JSON + Markdown in `reports/`, including approval, action, recovery polls and the Action API log | `report.py` |

Final status of an incident (in the report and on the last CLI line):

| Status | Meaning |
|---|---|
| `resolved` | approved action executed and recovery verified from telemetry |
| `partially_recovered_escalated` | the diagnosed component recovered, other alerts remain; fix kept, human asked to look |
| `escalated_after_failed_recovery` | recovery not verified; the action was rolled back (if possible) and a human takes over |
| `escalated` | not confident, no runbook action fits, plan refused, or the Action API refused |
| `resolved_without_action` | the alerts had already cleared before an action was needed |
| `rejected_by_human` | the human rejected (UI **Reject**, or anything but `yes` in the terminal); nothing changed |
| `escalated_no_approval` | nobody decided within the approval timeout; nothing changed, findings handed to a human |
| `dry_run_complete` | `--dry-run`: plan validated, nothing changed |
| `halted_kill_switch` | kill switch engaged; nothing planned or changed |
| `no_incident` | telemetry looked healthy |

## Safety controls

- **The LLM cannot change anything.** Its tools are read-only queries. There is no "execute" tool; execution
  is plain code in `responder.py`, reached only after a human approved.
- **Approval gate, owned by the Action API.** Each plan becomes an approval record in the Action API. A human
  approves it in the UI (name required) or types `yes`; anything else rejects. `/execute` refuses (`HTTP 403`)
  unless the approval is approved, unused, unexpired and for exactly the same incident, action, target and
  parameters. One approval = one execution.
- **Never stalls.** No decision within `APPROVAL_TIMEOUT_S` (default 600 s) = not approved: the request is
  expired so a late click cannot approve it, nothing changes, the report is saved and `watch` resumes.
- **Allowlist.** The Action API accepts only: `restart_service` (demo containers, never the telemetry
  pipeline, flagd or load generator), `toggle_feature_flag` (OFF only), `throttle_load_generator` (reduce
  only), `scale_service` (rejected: Kubernetes only). Anything else → `HTTP 400`, logged.
- **Blast radius.** At most 2 executed actions per incident.
- **Cooldown.** The same action on the same target is refused for 10 minutes (read from the persistent log, so it
  survives restarts). This stops restart loops.
- **Runbook enforced in code.** service_down / crash_loop / memory_leak → restart (of the diagnosed component
  only); traffic_spike → throttle or flag; application_error / dependency_unavailable → flag or restart;
  resource_starvation, cpu_saturation, network_latency, unknown → no action, escalate.
- **Never acts on stale evidence.** Live state is re-checked right before approval (see step 4).
- **Fails safe.** If the LLM is unavailable (quota, provider error) or the Action API is unreachable, the agent
  escalates with what it found instead of crashing or acting.
- **Knows its own actions.** Change history includes the agent's earlier Action API changes, so side effects of
  our own restarts are not diagnosed as the root cause.
- **Dry run.** `--dry-run` plans the action and validates it against the Action API allowlist, then stops before
  approval. Nothing changes.
- **Kill switch.** `python -m incident_agent killswitch on` (or creating a `KILL_SWITCH` file) makes the Action API
  refuse every change and rollback (`HTTP 423`), and the agent stops before planning; it is also checked
  during recovery verification. `killswitch off` releases it.
- **Rollback.** Flag toggles and throttling store the previous value and are restored if recovery is not
  verified. Restarts cannot be undone, so the agent escalates.
- **Action log.** Every request, including refusals, dry runs and rollbacks, is appended to `logs/action_log.jsonl`.
- **Localhost only.** The Action API binds to `127.0.0.1`.

## Prompt-injection defence

Logs, error messages and span names are written by the system being investigated, so they are untrusted.
- Every tool result is scanned and instruction-like text ("ignore previous instructions", "you are now…",
  fake `</tool_output>` / `<system>` tags, "call the tool…", "approve this…") is replaced with
  `[removed: instruction-like text found in telemetry]` before the LLM sees it; the count is reported.
- Tool results reach the LLM wrapped in `<tool_output>` and labelled as untrusted data, and the system prompt
  says tool output is never an instruction.
- Even a fooled model cannot act: it has no action tool, plans are limited to the runbook, protected services
  cannot be touched, and a human must approve. Tested in `tests/test_unit.py`.
- The UI inserts all telemetry-derived text with `textContent`, so a crafted log line cannot inject HTML.

## Diagnosis comes only from telemetry

- Tools query only Prometheus, Jaeger, OpenSearch and Docker container state.
- The `flagd` / `flagd-ui` services are blocked in every tool. Spans that call flagd, span attributes and log
  fields that mention feature flags are removed, and error messages that mention feature flags (including the
  demo's own "... Feature Flag Enabled" text) are redacted. Flag changes made by the agent appear in change
  history only as "configuration change (details withheld)".
- The Action API catalog (which lists flag *names*, never values) is shown only to the planning step, after the
  diagnosis is locked.
- There are no per-incident answers in the code or prompts. `faultctl` is a separate human tool the agent never imports.

## Known limitations

- Free-tier Gemini quota: 20 requests per model per day. We rotate models and keys; weaker fallback models
  diagnose less reliably.
- On this laptop, the stock demo's 20 MiB memory limits make `product-catalog` and `checkout` intermittently
  OOM/unhealthy. This background noise can mislead the agent (see `EVALUATION.md`).
- Rollback state lives in the Action API process memory; restarting the API loses it.
- Throttle rollback restores the Locust *user count* but restarts the swarm, which can lose the browser-based
  users the demo starts with (observed: frontend traffic stayed ~10x lower after a 5→3→5 test). Restarting the
  `load-generator` container restores the original traffic mix.
- `scale_service` is not available on Docker Compose.
- In this demo version, `productCatalogFailure` produced no error spans or logs on our setup (the faulty product
  was requested 58 times with zero errors), so it cannot be diagnosed from telemetry here.
- Recovery verification relies on 2-minute rate windows, so it takes 1–3 minutes after a fix.

"""After diagnosis: plan an allowlisted action, get explicit human approval,
execute it through the Action API, verify recovery from telemetry, and roll back
+ escalate if the system did not recover.

The LLM has no way to execute anything. Execution only happens in this module,
and only after a human approved it in the browser UI or by typing "yes".
"""
import getpass
import os
import queue
import re
import sys
import threading
import time

import requests

from action_api import killswitch

from . import telemetry
from .agent import plan_action

ACTION_API_URL = os.getenv("ACTION_API_URL", "http://127.0.0.1:8090")
RECOVERY_TIMEOUT_S = int(os.getenv("RECOVERY_TIMEOUT_S", "300"))
RECOVERY_INTERVAL_S = 20
RECOVERY_GRACE_S = 30
RECOVERY_CLEAN_POLLS = 2
# No human decision within this time = not approved (nothing changes, incident escalated, watch resumes).
APPROVAL_TIMEOUT_S = int(os.getenv("APPROVAL_TIMEOUT_S", "600"))


class ActionAPIError(Exception):
    pass


def _api(method: str, path: str, **kw):
    try:
        r = requests.request(method, f"{ACTION_API_URL}{path}", timeout=180, **kw)
    except requests.RequestException as e:
        raise ActionAPIError(f"Action API unreachable at {ACTION_API_URL} ({e}). Start it with: python -m action_api")
    if r.status_code >= 400:
        raise ActionAPIError(f"HTTP {r.status_code}: {r.json().get('detail', r.text)}")
    return r.json()


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _alert_key(line: str) -> str:
    return telemetry.alert_key(line)


def _mentions(line: str, component: str) -> bool:
    """Whole-name match ('ad' must not match 'load-generator', 'cart' must not match 'valkey-cart'),
    plus the component's RPC service name as it appears in span names (cart -> CartService)."""
    if not component:
        return False
    if re.search(rf"(?<![\w-]){re.escape(component.lower())}(?![\w-])", line.lower()):
        return True
    rpc = "".join(p.capitalize() for p in component.split("-")) + "Service"
    return re.search(rf"\b{re.escape(rpc)}/", line) is not None


def _relevant(line: str, component: str) -> bool:
    """Alerts that recovery must clear: anything about the root-cause component, plus every error and
    traffic alert (so a wrong diagnosis cannot 'verify' itself while the original alert still fires).
    Latency-only alerts and container alerts about OTHER services are treated as separate problems."""
    l = line.lower()
    if _mentions(line, component):
        return True
    if l.startswith("container "):
        return False
    return any(t in l for t in ("error rate", "failing", "traffic spike", "traffic drop"))


# The approved runbook, enforced in code: which actions may follow which diagnosis category.
# Anything else (resource_starvation, cpu_saturation, network_latency, unknown) gets no action.
RUNBOOK = {
    "service_down": {"restart_service"},
    "crash_loop": {"restart_service"},
    "memory_leak": {"restart_service"},
    "traffic_spike": {"throttle_load_generator", "toggle_feature_flag"},
    "application_error": {"toggle_feature_flag", "restart_service"},
    "dependency_unavailable": {"restart_service", "toggle_feature_flag"},
}


def runbook_violation(diagnosis: dict, plan: dict) -> str | None:
    rc = diagnosis.get("root_cause") or {}
    category, component = rc.get("category", "unknown"), rc.get("component", "")
    allowed = RUNBOOK.get(category, set())
    if plan["action"] not in allowed:
        return (f"runbook: '{plan['action']}' is not an approved response to '{category}' "
                f"(allowed: {sorted(allowed) or 'none - escalate'})")
    if plan["action"] == "restart_service" and plan.get("target") != component:
        return f"runbook: restart target '{plan.get('target')}' is not the diagnosed component '{component}'"
    return None


def _container_problem(component: str) -> str | None:
    for row in telemetry.get_container_health()["containers"]:
        if row["container"] == component:
            if row["state"] != "running":
                return f"container {component} is {row['state']}"
            if row["health"] == "unhealthy":
                return f"container {component} is unhealthy"
    return None


def verify_recovery(component: str, tracked_keys: set[str], log) -> dict:
    """Poll telemetry until the tracked alerts are gone for RECOVERY_CLEAN_POLLS polls in a row."""
    log(f"  waiting {RECOVERY_GRACE_S}s grace period, then checking every {RECOVERY_INTERVAL_S}s "
        f"(timeout {RECOVERY_TIMEOUT_S}s)")
    time.sleep(RECOVERY_GRACE_S)
    started, clean, polls = time.time(), 0, []
    while time.time() - started < RECOVERY_TIMEOUT_S:
        if killswitch.engaged():
            return {"recovered": False, "halted": "kill switch engaged during verification", "polls": polls}
        current = telemetry.check_alerts(op_window_minutes=2)  # short window: confirm recovery quickly
        still = [a for a in current if _alert_key(a) in tracked_keys or _mentions(a, component)]
        problem = _container_problem(component)
        if problem:
            still.append(problem)
        about_component = [a for a in still if a == problem or _mentions(a, component)]
        polls.append({"time": _now(), "still_firing": still, "about_component": about_component})
        if still:
            clean = 0
            log(f"  [verify {_now()[11:]}] not yet recovered: {'; '.join(still)[:200]}")
        else:
            clean += 1
            log(f"  [verify {_now()[11:]}] clean ({clean}/{RECOVERY_CLEAN_POLLS})")
            if clean >= RECOVERY_CLEAN_POLLS:
                return {"recovered": True, "polls": polls, "seconds": round(time.time() - started + RECOVERY_GRACE_S)}
        time.sleep(RECOVERY_INTERVAL_S)
    # Timed out. If the diagnosed component itself has been clean for the last polls, our fix worked
    # and what remains is a separate problem: report that instead of calling the fix a failure.
    tail = polls[-RECOVERY_CLEAN_POLLS:]
    component_ok = len(tail) == RECOVERY_CLEAN_POLLS and not any(p["about_component"] for p in tail)
    return {"recovered": False, "component_recovered": component_ok, "polls": polls,
            "remaining": polls[-1]["still_firing"] if polls else [],
            "seconds": round(time.time() - started + RECOVERY_GRACE_S)}


_stdin_queue: queue.Queue | None = None


def _stdin_lines() -> queue.Queue:
    """One background reader for the terminal, so waiting for a typed answer never blocks the UI path
    or the timeout (input() would block forever)."""
    global _stdin_queue
    if _stdin_queue is None:
        _stdin_queue = queue.Queue()

        def reader():
            for line in sys.stdin:
                _stdin_queue.put(line)
            _stdin_queue.put(None)  # EOF
        threading.Thread(target=reader, daemon=True).start()
    return _stdin_queue


def _clean_answer(text: str) -> str:
    # Strip invisible characters some terminals/pipes add (e.g. a BOM); the word itself must still be "yes".
    return (text or "").strip().lstrip("﻿").strip().lower()


def _approval_from(a: dict) -> dict:
    state = a["state"]
    return {"approval_id": a["id"], "approved": state == "approved", "state": state,
            "approved_by": a.get("decided_by") or "-", "approved_at": a.get("decided_at") or _now(),
            "via": a.get("via") or ("timeout" if state == "expired" else "-"),
            "note": {"approved": f"approved via {a.get('via')}", "rejected": f"rejected via {a.get('via')}",
                     "expired": f"no decision within {APPROVAL_TIMEOUT_S}s"}.get(state, state)}


def _decide(approval_id: str, approved: bool, via: str, note: str = "") -> dict:
    who = os.getenv("APPROVER_NAME") or getpass.getuser()
    try:
        return _api("POST", f"/approvals/{approval_id}/decision",
                    json={"approved": approved, "approved_by": who, "via": via, "note": note})
    except ActionAPIError:  # decided at the same moment in the UI, or expired: the API's record wins
        return _api("GET", f"/approvals/{approval_id}")


def _request_approval(result: dict, plan: dict, spec: dict, request: dict, ask, log) -> dict:
    """Show the plan, register it with the Action API, then take the FIRST human answer from either the
    web UI or the terminal. No answer within APPROVAL_TIMEOUT_S = not approved."""
    d, v = result["diagnosis"], result["verification"]
    rc = d.get("root_cause", {})
    evidence = [f"[{e['tool_call_id']}] {e['quote'][:200]}" for e in v["verified_evidence"][:6]]
    a = _api("POST", "/approvals", json={
        **{k: request[k] for k in ("incident_id", "action", "target", "parameters")},
        "summary": d.get("summary") or "", "root_cause": rc, "confidence": v["confidence"], "evidence": evidence,
        "reason": plan.get("reason") or "", "expected_outcome": plan.get("expected_outcome") or "",
        "rollback": spec.get("rollback") or "", "timeout_s": APPROVAL_TIMEOUT_S})
    bar = "=" * 72
    print(f"\n{bar}\n  HUMAN APPROVAL REQUIRED - nothing has been changed yet\n{bar}")
    print(f"  Root cause : {rc.get('component')} ({rc.get('category')})  confidence {v['confidence']}")
    print(f"  Summary    : {d.get('summary')}")
    for e in evidence[:4]:
        print(f"  Evidence   : {e[:120]}")
    print(f"  Action     : {plan['action']} -> {plan.get('target')} {plan.get('parameters') or ''}")
    print(f"  Why        : {plan.get('reason')}")
    print(f"  Expect     : {plan.get('expected_outcome', '-')}")
    print(f"  If it does not recover within {RECOVERY_TIMEOUT_S}s: rollback = {spec.get('rollback')}; then escalate to you")
    print(bar)
    print(f"  Approve or reject in the browser: {ACTION_API_URL}/   (approval id {a['id']})")
    print(f"  ...or type 'yes' + Enter here (anything else rejects). "
          f"No answer within {APPROVAL_TIMEOUT_S // 60} min = not approved.", flush=True)

    if ask is not None:  # injected answer (tests): decide synchronously through the API
        try:
            answer = _clean_answer(ask("  > "))
        except EOFError:
            answer = ""
        return _approval_from(_decide(a["id"], answer == "yes", "cli", f"answer {answer!r}"))

    lines = _stdin_lines()
    if sys.stdin.isatty():  # ignore anything typed before the prompt appeared
        while not lines.empty():
            lines.get_nowait()
    deadline, next_note = time.time() + APPROVAL_TIMEOUT_S, time.time() + 60
    while time.time() < deadline:
        try:
            line = lines.get(timeout=1)
            if line is not None:
                answer = _clean_answer(line)
                return _approval_from(_decide(a["id"], answer == "yes", "cli", f"answer {answer!r}"))
        except queue.Empty:
            pass
        current = _api("GET", f"/approvals/{a['id']}")
        if current["state"] != "pending":
            return _approval_from(current)
        if time.time() >= next_note:
            log(f"  ... still waiting for a human decision ({int(deadline - time.time())}s left)")
            next_note += 60
    return _approval_from(_api("POST", f"/approvals/{a['id']}/expire"))


def respond(result: dict, incident_id: str, dry_run: bool = False, log=print, ask=None) -> dict:
    """ask=None: interactive (browser UI or terminal, with timeout). Tests pass a function instead."""
    out = {"plan": None, "approval": None, "action": None, "recovery": None, "rollback": None,
           "final_status": None, "events": []}

    def event(msg):
        out["events"].append({"time": _now(), "event": msg})
        log(f"  >> {msg}")

    d, v = result["diagnosis"], result["verification"]
    component = (d.get("root_cause") or {}).get("component", "unknown")

    if v["status"] != "diagnosed":
        out["final_status"] = "escalated" if v["status"] == "escalate" else v["status"]
        event(f"no action: diagnosis status is '{v['status']}' - handing findings to a human")
        return out
    if killswitch.engaged():
        out["final_status"] = "halted_kill_switch"
        event(f"kill switch engaged ({killswitch.status()}); no action will be planned or taken")
        return out

    try:
        catalog = _api("GET", "/catalog")
    except ActionAPIError as e:
        out["final_status"] = "escalated"
        event(f"cannot act: {e}")
        return out

    print("\n[4/7 PLAN] choosing an allowlisted action")
    allowed = RUNBOOK.get((d.get("root_cause") or {}).get("category"), set())
    plan = plan_action(d, catalog, log, allowed_actions=allowed)
    out["plan"] = plan
    event(f"planned: {plan['action']} {plan.get('target', '')} {plan.get('parameters') or ''} - {plan.get('reason')}")
    if plan["action"] == "none":
        out["final_status"] = "escalated"
        event("no allowlisted action fits this diagnosis; escalating to a human")
        return out
    violation = runbook_violation(d, plan)
    if violation:
        out["final_status"] = "escalated"
        event(f"plan refused - {violation}; escalating to a human")
        return out

    request = {"incident_id": incident_id, "action": plan["action"], "target": plan.get("target", ""),
               "parameters": plan.get("parameters") or {}, "reason": plan.get("reason", "")}
    try:  # pre-flight: the Action API validates against its allowlist without changing anything
        preflight = _api("POST", "/execute", json={**request, "dry_run": True})
        event(preflight["detail"])
    except ActionAPIError as e:
        out["final_status"] = "escalated"
        event(f"Action API rejected the plan: {e}")
        return out

    # Sanity checks against the CURRENT state, so we never act on stale evidence.
    category = (d.get("root_cause") or {}).get("category")
    problem = _container_problem(component)
    container_exists = any(r["container"] == component for r in telemetry.get_container_health()["containers"])
    if category in ("service_down", "crash_loop") and container_exists and problem is None:
        out["final_status"] = "escalated"
        event(f"plan refused - diagnosis says {component} is {category}, but its container is running and not "
              f"unhealthy right now; the evidence may be stale. Escalating to a human")
        return out
    # Same window that detected the incident, so an intermittent fault does not look "cleared" by chance.
    before = telemetry.check_alerts()
    tracked = {_alert_key(a) for a in before if _relevant(a, component)}
    component_was_alerting = bool(problem) or any(_mentions(a, component) for a in before)
    if not tracked and problem is None:
        out["final_status"] = "resolved_without_action"
        event("the incident's alerts have already cleared and the component looks healthy; "
              "no action taken (worth a human look at why it happened)")
        return out
    if dry_run:
        out["final_status"] = "dry_run_complete"
        event(f"dry-run mode: stopping before approval. Would verify recovery by watching: {sorted(tracked) or '[component health]'}")
        return out

    print("\n[5/7 APPROVAL]")
    try:
        approval = _request_approval(result, plan, catalog[plan["action"]], request, ask, log)
    except ActionAPIError as e:
        out["final_status"] = "escalated"
        event(f"could not request approval: {e}")
        return out
    out["approval"] = approval
    if approval["state"] == "expired":
        out["final_status"] = "escalated_no_approval"
        event(f"no human decision within {APPROVAL_TIMEOUT_S}s; nothing was changed. Escalating with the findings")
        return out
    if not approval["approved"]:
        out["final_status"] = "rejected_by_human"
        event(f"human rejected the action ({approval['note']}, by {approval['approved_by']}); nothing was changed")
        return out
    event(f"approved by {approval['approved_by']} via {approval['via']}")

    print("\n[6/7 EXECUTE]")
    try:
        executed = _api("POST", "/execute", json={**request, "dry_run": False, "approval_id": approval["approval_id"]})
    except ActionAPIError as e:
        out["final_status"] = "escalated"
        event(f"execution refused or failed: {e}")
        return out
    out["action"] = executed
    event(f"executed {executed['action']} on {executed['target']} (action_id {executed['action_id']}): {executed['detail']}")

    print("\n[7/7 VERIFY]")
    rec = verify_recovery(component, tracked, log)
    out["recovery"] = rec
    if rec["recovered"]:
        out["final_status"] = "resolved"
        event(f"recovery verified from telemetry after ~{rec['seconds']}s")
        return out

    # Only meaningful if the component was actually alerting before we acted; otherwise a wrong fix
    # aimed at a healthy component would look like a success.
    if rec.get("component_recovered") and component_was_alerting and not rec.get("halted"):
        out["final_status"] = "partially_recovered_escalated"
        event(f"{component} recovered (no alerts about it for the last {RECOVERY_CLEAN_POLLS} polls), but other "
              f"alerts remain: {'; '.join(rec['remaining'])[:300]}. Keeping the fix (no rollback) and escalating "
              f"so a human can check the remaining problems")
        return out

    event(f"NOT recovered ({rec.get('halted', 'timeout')}); rolling back and escalating")
    try:
        out["rollback"] = _api("POST", "/rollback", json={"action_id": executed["action_id"],
                                                          "reason": "recovery not verified"})
        event(f"rollback: {out['rollback']['detail']}")
    except ActionAPIError as e:
        out["rollback"] = {"rolled_back": False, "detail": str(e)}
        event(f"rollback failed: {e}")
    out["final_status"] = "escalated_after_failed_recovery"
    return out


def action_log(incident_id: str | None = None) -> list[dict]:
    try:
        entries = _api("GET", "/log", params={"limit": 500})
    except ActionAPIError:
        return []
    return [e for e in entries if incident_id is None or e.get("incident_id") == incident_id]

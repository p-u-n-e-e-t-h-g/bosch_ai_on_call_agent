"""After diagnosis: plan an allowlisted action, get explicit human approval,
execute it through the Action API, verify recovery from telemetry, and roll back
+ escalate if the system did not recover.

The LLM has no way to execute anything. Execution only happens in this module,
and only after a human typed "yes".
"""
import getpass
import os
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
    return line.split(":")[0].strip()


def _relevant(line: str, component: str) -> bool:
    """Alerts that recovery should clear: anything about the root-cause component, and error/availability
    alerts. Latency/traffic alerts about other services are treated as background noise."""
    l = line.lower()
    return component.lower() in l or any(t in l for t in ("error rate", "failing", "container"))


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
        current = telemetry.check_alerts()
        still = [a for a in current if _alert_key(a) in tracked_keys or
                 (component.lower() in a.lower() and _relevant(a, component))]
        problem = _container_problem(component)
        if problem:
            still.append(problem)
        polls.append({"time": _now(), "still_firing": still})
        if still:
            clean = 0
            log(f"  [verify {_now()[11:]}] not yet recovered: {'; '.join(still)[:200]}")
        else:
            clean += 1
            log(f"  [verify {_now()[11:]}] clean ({clean}/{RECOVERY_CLEAN_POLLS})")
            if clean >= RECOVERY_CLEAN_POLLS:
                return {"recovered": True, "polls": polls, "seconds": round(time.time() - started + RECOVERY_GRACE_S)}
        time.sleep(RECOVERY_INTERVAL_S)
    return {"recovered": False, "polls": polls, "seconds": round(time.time() - started + RECOVERY_GRACE_S)}


def _ask_approval(result: dict, plan: dict, spec: dict, ask) -> dict:
    d, v = result["diagnosis"], result["verification"]
    rc = d.get("root_cause", {})
    bar = "=" * 72
    print(f"\n{bar}\n  HUMAN APPROVAL REQUIRED - nothing has been changed yet\n{bar}")
    print(f"  Root cause : {rc.get('component')} ({rc.get('category')})  confidence {v['confidence']}")
    print(f"  Summary    : {d.get('summary')}")
    for e in v["verified_evidence"][:4]:
        print(f"  Evidence   : [{e['tool_call_id']}] {e['quote'][:110]}")
    print(f"  Action     : {plan['action']} -> {plan.get('target')} {plan.get('parameters') or ''}")
    print(f"  Why        : {plan.get('reason')}")
    print(f"  Expect     : {plan.get('expected_outcome', '-')}")
    print(f"  If it does not recover within {RECOVERY_TIMEOUT_S}s: rollback = {spec.get('rollback')}; then escalate to you")
    print(bar)
    try:
        # Strip invisible characters some terminals/pipes add (e.g. a BOM); the word itself must still be "yes".
        answer = ask("  Type 'yes' to approve, anything else to reject: ").strip().lstrip("﻿").strip().lower()
    except EOFError:
        answer = ""
    who = os.getenv("APPROVER_NAME") or getpass.getuser()
    return {"approved": answer == "yes", "approved_by": who, "approved_at": _now(),
            "note": "approved at CLI" if answer == "yes" else f"rejected at CLI (answer: {answer!r})"}


def respond(result: dict, incident_id: str, dry_run: bool = False, log=print, ask=input) -> dict:
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
    plan = plan_action(d, catalog, log)
    out["plan"] = plan
    event(f"planned: {plan['action']} {plan.get('target', '')} {plan.get('parameters') or ''} - {plan.get('reason')}")
    if plan["action"] == "none":
        out["final_status"] = "escalated"
        event("no allowlisted action fits this diagnosis; escalating to a human")
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

    tracked = {_alert_key(a) for a in telemetry.check_alerts() if _relevant(a, component)}
    if dry_run:
        out["final_status"] = "dry_run_complete"
        event(f"dry-run mode: stopping before approval. Would verify recovery by watching: {sorted(tracked) or '[component health]'}")
        return out

    print("\n[5/7 APPROVAL]")
    approval = _ask_approval(result, plan, catalog[plan["action"]], ask)
    out["approval"] = approval
    if not approval["approved"]:
        out["final_status"] = "rejected_by_human"
        event(f"human rejected the action ({approval['note']}); nothing was changed")
        return out
    event(f"approved by {approval['approved_by']}")

    print("\n[6/7 EXECUTE]")
    try:
        executed = _api("POST", "/execute", json={**request, "dry_run": False, "approval": approval})
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

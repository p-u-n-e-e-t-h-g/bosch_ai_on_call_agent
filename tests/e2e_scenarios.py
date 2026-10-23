"""Live end-to-end safety scenarios against the running demo.

TEST HARNESS ONLY. The real agent never uses anything in this file. To exercise the
safety pipeline without spending LLM quota, the LLM's two jobs (diagnosis and action
choice) are replaced by a scripted stand-in per scenario. Everything else is real:
fault injection, alert rules, telemetry tools, the evidence check (scripted quotes must
literally appear in real tool output), the Action API, the approval prompt (answered
by piping text), execution, recovery verification, rollback and the report.

  .venv\\Scripts\\python -m tests.e2e_scenarios               # all scenarios
  .venv\\Scripts\\python -m tests.e2e_scenarios reject flag   # selected ones
Needs the Action API running (ACTION_API_URL, default http://127.0.0.1:8090).
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("APPROVER_NAME", "claude-test-run")
ROOT = Path(__file__).resolve().parent.parent
FAULTCTL = [sys.executable, str(ROOT / "faultctl" / "faultctl.py")]
FLAG_FILE = ROOT.parent / "opentelemetry-demo" / "src" / "flagd" / "demo.flagd.json"

from action_api import killswitch  # noqa: E402
from incident_agent import evidence, report, responder, telemetry  # noqa: E402
from incident_agent.report import build_report, new_incident_id, save  # noqa: E402
from incident_agent.tools import ToolRunner  # noqa: E402

RESULTS = []
# Scripted runs are kept apart from reports produced by the real agent.
report.REPORTS_DIR = ROOT / "reports" / "e2e"


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def faultctl(*args):
    out = subprocess.run([*FAULTCTL, *args], capture_output=True, text=True).stdout.strip()
    log(f"faultctl {' '.join(args)}: {out.splitlines()[-1] if out else ''}")


def container_state(name):
    return subprocess.run(["docker", "inspect", name, "--format", "{{.State.Status}}"],
                          capture_output=True, text=True).stdout.strip()


def flag(name):
    return json.loads(FLAG_FILE.read_text())["flags"][name]["defaultVariant"]


def wait_for(pred, what, timeout=240, every=10):
    start = time.time()
    while time.time() - start < timeout:
        alerts = telemetry.check_alerts()
        if pred(alerts):
            log(f"{what}: yes after {int(time.time() - start)}s")
            return alerts
        time.sleep(every)
    log(f"{what}: NOT reached within {timeout}s")
    return None


def calm(names, timeout=300):
    """Wait until no alert mentions the given services (lets the previous scenario's effects fade)."""
    return wait_for(lambda a: not any(responder._mentions(x, n) for x in a for n in names),
                    f"calm for {names}", timeout=timeout, every=15)


def quote(output, pattern):
    m = re.search(pattern, output)
    return m.group(0) if m else None


def scripted_result(alert, component, category, probes):
    """Run REAL tools and build a diagnosis whose quotes come from their real output.
    probes: list of (tool, args, regex). Returns an investigate()-shaped result."""
    runner, evs = ToolRunner(), []
    for tool, args, pattern in probes:
        cid, out = runner.run(tool, args)
        q = quote(out, pattern)
        log(f"  probe {tool}{args}: {'found ' + repr(q[:80]) if q else 'NO MATCH for ' + pattern}")
        if q:
            evs.append({"tool_call_id": cid, "quote": q, "explanation": f"scripted probe {pattern}"})
    diagnosis = {"status": "diagnosed", "summary": f"[scripted test diagnosis] {component} {category}",
                 "root_cause": {"component": component, "category": category, "description": "scripted"},
                 "confidence": 0.9, "evidence": evs}
    return {"alert": alert, "diagnosis": diagnosis, "verification": evidence.verify(diagnosis, runner),
            "timeline": [{"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": f"alert received: {alert}"}],
            "tool_calls": runner.calls, "model": "scripted-test-double", "usage": {}, "steps": len(probes)}


def respond(name, result, plan, answer="yes", dry_run=False, ask=None):
    responder.plan_action = lambda d, c, log=None: dict(plan)  # test double for the planner LLM
    inc = new_incident_id()
    resp = responder.respond(result, inc, dry_run=dry_run, ask=ask or (lambda _: answer),
                             log=lambda m: log(m.strip()))
    rep = build_report(result, f"e2e-{name}", inc, resp, responder.action_log(inc))
    save(rep, result)
    return resp, responder.action_log(inc)


def check(name, conditions):
    ok = all(v for _, v in conditions)
    RESULTS.append((name, ok, [(c, v) for c, v in conditions]))
    log(f"==> {name}: {'PASS' if ok else 'FAIL'}")
    for c, v in conditions:
        log(f"      [{'x' if v else ' '}] {c}")


CART_PROBES = [("get_container_health", {}, r'"container": "cart", "state": "exited"'),
               ("get_service_overview", {}, r'"operation": "oteldemo\.CartService/\w+", "error_rps": [\d.]+')]
RESTART_CART = {"action": "restart_service", "target": "cart", "parameters": {}, "reason": "cart container exited",
                "expected_outcome": "cart running, cart call errors clear"}


def cart_down(name):
    faultctl("inject", "stop-cart")
    # Wait for two independent signals: the container state AND callers failing to reach it.
    alerts = wait_for(lambda a: any(x.startswith("Container cart is exited") for x in a)
                      and any("CartService" in x for x in a), "cart-down + caller errors", 180, 10)
    return scripted_result("; ".join(alerts or []), "cart", "service_down", CART_PROBES)


# ---------------------------------------------------------------- scenarios

def s_reject():
    res = cart_down("reject")
    resp, entries = respond("reject", res, RESTART_CART, answer="no")
    check("reject: human says no -> nothing changes", [
        ("evidence verified from real tool output", len(res["verification"]["verified_evidence"]) == 2),
        ("final status rejected_by_human", resp["final_status"] == "rejected_by_human"),
        ("cart still exited", container_state("cart") == "exited"),
        ("only a dry-run pre-flight in the action log", [e["event"] for e in entries] == ["dry_run"]),
    ])


def s_dry_run():
    res = cart_down("dryrun")
    resp, entries = respond("dryrun", res, RESTART_CART, dry_run=True)
    check("dry run: plans + validates, changes nothing, no approval asked", [
        ("final status dry_run_complete", resp["final_status"] == "dry_run_complete"),
        ("approval never requested", resp["approval"] is None),
        ("cart still exited", container_state("cart") == "exited"),
        ("no executed entry in log", not any(e["event"] == "executed" for e in entries)),
    ])


def s_kill_before():
    res = cart_down("kill-before")
    killswitch.engage(by="e2e", reason="test")
    try:
        resp, entries = respond("kill-before", res, RESTART_CART)
    finally:
        killswitch.release()
    check("kill switch engaged before run -> agent halts", [
        ("final status halted_kill_switch", resp["final_status"] == "halted_kill_switch"),
        ("no plan, no approval", resp["plan"] is None and resp["approval"] is None),
        ("cart still exited", container_state("cart") == "exited"),
    ])


def s_kill_after_approval():
    res = cart_down("kill-after")

    def approve_then_pull_switch(_):
        killswitch.engage(by="e2e", reason="pulled right after approval")
        return "yes"
    try:
        resp, entries = respond("kill-after", res, RESTART_CART, ask=approve_then_pull_switch)
    finally:
        killswitch.release()
    check("kill switch pulled after approval -> Action API refuses (423)", [
        ("approved by human", (resp["approval"] or {}).get("approved") is True),
        ("final status escalated", resp["final_status"] == "escalated"),
        ("Action API logged refused_kill_switch", any(e["event"] == "refused_kill_switch" for e in entries)),
        ("cart still exited", container_state("cart") == "exited"),
    ])


def s_restart():
    res = cart_down("restart")
    resp, entries = respond("restart", res, RESTART_CART)
    check("happy path: approve restart -> executed -> recovery verified", [
        ("final status resolved", resp["final_status"] == "resolved"),
        ("action executed via Action API", any(e["event"] == "executed" for e in entries)),
        ("cart running", container_state("cart") == "running"),
        ("recovery verified by telemetry polls", (resp["recovery"] or {}).get("recovered") is True),
    ])


def s_cooldown():
    res = cart_down("cooldown")
    resp, entries = respond("cooldown", res, RESTART_CART)
    check("cooldown: same restart again within 10 min -> refused, escalated", [
        ("final status escalated", resp["final_status"] == "escalated"),
        ("rejected by Action API with cooldown", any("cooldown" in (e.get("detail") or "") for e in entries)),
        ("approval never requested", resp["approval"] is None),
    ])
    faultctl("reset")


def s_flag():
    calm(["cart", "checkout", "payment"])
    faultctl("inject", "payment-failure")
    alerts = wait_for(lambda a: any("checkout" in x.lower() and ("error" in x.lower() or "failing" in x.lower())
                                    for x in a), "checkout error alert", 300, 15)
    res = scripted_result("; ".join(alerts or []), "payment", "application_error", [
        ("search_error_traces", {"service": "checkout"}, r'"error_message": "[^"]*[Pp]ayment[^"]*"'),
        ("search_logs", {"service": "payment"}, r'"message": "[^"]*[Pp]ayment[^"]*"'),
    ])
    plan = {"action": "toggle_feature_flag", "target": "paymentFailure", "parameters": {"variant": "off"},
            "reason": "payment rejects charges", "expected_outcome": "checkout errors clear"}
    resp, entries = respond("flag", res, plan)
    check("feature flag fix: approve flag off -> recovery verified", [
        ("evidence verified from real traces/logs", len(res["verification"]["verified_evidence"]) == 2),
        ("final status resolved", resp["final_status"] == "resolved"),
        ("flag is off", flag("paymentFailure") == "off"),
        ("recovery verified", (resp["recovery"] or {}).get("recovered") is True),
    ])


def s_rollback():
    faultctl("inject", "ad-failure")  # gives the flag a non-off value that rollback must restore
    res = cart_down("rollback")
    plan = {"action": "toggle_feature_flag", "target": "adFailure", "parameters": {"variant": "off"},
            "reason": "deliberately WRONG fix (cart is down) to exercise rollback", "expected_outcome": "will not help"}
    old = responder.RECOVERY_TIMEOUT_S
    responder.RECOVERY_TIMEOUT_S = 90
    try:
        resp, entries = respond("rollback", res, plan)
    finally:
        responder.RECOVERY_TIMEOUT_S = old
    check("wrong fix: recovery NOT verified -> rollback -> escalate", [
        ("flag was changed by the approved action", any(e["event"] == "executed" for e in entries)),
        ("final status escalated_after_failed_recovery", resp["final_status"] == "escalated_after_failed_recovery"),
        ("Action API logged rolled_back", any(e["event"] == "rolled_back" for e in entries)),
        ("flag restored to its previous value (on)", flag("adFailure") == "on"),
        ("cart still exited (wrong fix did not touch it)", container_state("cart") == "exited"),
    ])
    faultctl("reset")


SCENARIOS = {"reject": s_reject, "dryrun": s_dry_run, "kill-before": s_kill_before,
             "kill-after": s_kill_after_approval, "restart": s_restart, "cooldown": s_cooldown,
             "flag": s_flag, "rollback": s_rollback}


def main():
    names = sys.argv[1:] or list(SCENARIOS)
    log(f"Action API: {responder.ACTION_API_URL}  approver: {os.environ['APPROVER_NAME']}")
    responder._api("GET", "/health")
    faultctl("reset")
    calm(["cart", "payment", "checkout"])
    for n in names:
        log(f"\n######## scenario: {n}")
        try:
            SCENARIOS[n]()
        except Exception as e:
            RESULTS.append((n, False, [(f"crashed: {type(e).__name__}: {e}", False)]))
            log(f"==> {n}: CRASHED {type(e).__name__}: {e}")
            faultctl("reset")
    faultctl("reset")
    print("\n================ SUMMARY ================")
    for n, ok, _ in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {n}")
    print(f"  {sum(ok for _, ok, _ in RESULTS)}/{len(RESULTS)} passed")
    return 0 if all(ok for _, ok, _ in RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())

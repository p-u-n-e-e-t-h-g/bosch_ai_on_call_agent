"""CLI.

  python -m incident_agent watch --once              # alert -> diagnose -> approve -> act -> verify -> report
  python -m incident_agent watch --once --dry-run    # same, but stops before approval; changes nothing
  python -m incident_agent investigate --no-act      # diagnosis only
  python -m incident_agent killswitch on|off|status  # stop all actions immediately
  python -m incident_agent actions                   # show the Action API log
  python -m incident_agent tools                     # smoke-test every telemetry tool (no LLM)
"""
import argparse
import json
import sys
import time

from action_api import killswitch

from . import config, responder, telemetry
from .agent import investigate
from .report import build_report, new_incident_id, save, to_markdown


def run_incident(alert: str, label: str | None, act: bool, dry_run: bool):
    incident_id = new_incident_id()
    mode = "DRY RUN" if dry_run else ("diagnose + act with approval" if act else "diagnose only")
    print(f"\n=== {incident_id}  [{mode}]  model={config.LLM_MODEL}")
    print(f"[1/7 DETECT] {alert}")
    print("\n[2/7 INVESTIGATE] the LLM picks read-only telemetry tools step by step")
    result = investigate(alert)
    v = result["verification"]
    rc = result["diagnosis"].get("root_cause") or {}
    print(f"\n[3/7 DIAGNOSIS] {v['status']}: {rc.get('component')} / {rc.get('category')} "
          f"(confidence {v['confidence']}, {len(v['verified_evidence'])} verified evidence, "
          f"{len(v['rejected_evidence'])} rejected)")
    response = responder.respond(result, incident_id, dry_run=dry_run) if act else None
    rep = build_report(result, label, incident_id, response, responder.action_log(incident_id))
    path = save(rep, result)
    print("\n" + to_markdown(rep))
    print(f"FINAL STATUS: {rep['status']}\nReport saved: {path}")
    return rep


def cmd_watch(args):
    print(f"Watching every {args.interval}s (error rate >= {config.ALERT_ERROR_RATE:.0%}, "
          f"p95 >= {config.ALERT_LATENCY_P95_MS} ms, container down). Ctrl+C to stop.")
    while True:
        alerts = telemetry.check_alerts()
        if alerts:
            print(f"{time.strftime('%H:%M:%S')} ALERT: " + " | ".join(alerts))
            run_incident("; ".join(alerts), args.label, not args.no_act, args.dry_run)
            if args.once:
                return
            time.sleep(args.cooldown)
        else:
            print(f"{time.strftime('%H:%M:%S')} ok")
            time.sleep(args.interval)


def cmd_killswitch(args):
    if args.state == "on":
        killswitch.engage(reason=args.reason)
    elif args.state == "off":
        killswitch.release()
    print(f"kill switch: {killswitch.status()}")


def cmd_actions(args):
    entries = responder.action_log(args.incident)
    if not entries:
        print("no Action API log entries (is the Action API running? python -m action_api)")
    for e in entries:
        print(f"{e['time']}  {e['event']:<26} {e.get('incident_id') or '-':<20} {e.get('action')} "
              f"{e.get('target') or ''}  by={e.get('approved_by')}  {e.get('detail', '')[:80]}")


def cmd_tools(_):
    checks = [
        ("get_service_overview", lambda: telemetry.get_service_overview()),
        ("get_container_health", telemetry.get_container_health),
        ("get_operation_breakdown(checkout)", lambda: telemetry.get_operation_breakdown("checkout")),
        ("search_error_traces(frontend)", lambda: telemetry.search_error_traces("frontend")),
        ("search_slow_traces(frontend)", lambda: telemetry.search_slow_traces("frontend")),
        ("search_logs()", lambda: telemetry.search_logs()),
        ("get_container_resources(recommendation)", lambda: telemetry.get_container_resources("recommendation")),
        ("get_recent_changes", telemetry.get_recent_changes),
        ("check_alerts", telemetry.check_alerts),
    ]
    for name, fn in checks:
        try:
            out = json.dumps(fn(), default=str)
            print(f"OK   {name}: {len(out)} chars  {out[:220]}")
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}")


def main():
    p = argparse.ArgumentParser(prog="incident_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("investigate", "watch"):
        s = sub.add_parser(name)
        s.add_argument("--label", default=None, help="Tag for the report file, e.g. practice-1")
        s.add_argument("--no-act", action="store_true", help="Diagnose only; never propose or execute an action")
        s.add_argument("--dry-run", action="store_true", help="Plan and validate the action, but change nothing")
        if name == "investigate":
            s.add_argument("--alert", default=None, help="Alert text; omitted = use current alert rules")
        else:
            s.add_argument("--interval", type=int, default=20)
            s.add_argument("--cooldown", type=int, default=120)
            s.add_argument("--once", action="store_true")
    ks = sub.add_parser("killswitch")
    ks.add_argument("state", choices=["on", "off", "status"])
    ks.add_argument("--reason", default="manual")
    ac = sub.add_parser("actions")
    ac.add_argument("--incident", default=None)
    sub.add_parser("tools")
    args = p.parse_args()

    if args.cmd == "investigate":
        alert = args.alert or "; ".join(telemetry.check_alerts()) or "Manual investigation requested: no alert rule is firing"
        run_incident(alert, args.label, not args.no_act, args.dry_run)
    elif args.cmd == "watch":
        cmd_watch(args)
    elif args.cmd == "killswitch":
        cmd_killswitch(args)
    elif args.cmd == "actions":
        cmd_actions(args)
    else:
        cmd_tools(args)


if __name__ == "__main__":
    sys.exit(main())

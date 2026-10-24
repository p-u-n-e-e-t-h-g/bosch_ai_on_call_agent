"""Evaluation runner: real agent, real LLM, real CLI, injected practice incidents.

EVALUATION ONLY. The expected answers below are used to SCORE the agent's report after it
finished; the agent never sees them. Approval is given by piping "yes" (approver recorded
as claude-test-run), exactly like a human typing it.

  .venv\\Scripts\\python -m tests.llm_runs                 # all
  .venv\\Scripts\\python -m tests.llm_runs stop-cart       # selected
"""
import json
import re
import os
import subprocess
import sys
import time
from pathlib import Path

from tests.e2e_scenarios import faultctl, log, wait_for

ROOT = Path(__file__).resolve().parent.parent

# incident -> (alert predicate, acceptable root-cause components, acceptable final statuses)
CASES = {
    "stop-cart": (lambda a: any(x.startswith("Container cart is exited") for x in a) and any("CartService" in x for x in a),
                  {"cart"}, {"resolved", "partially_recovered_escalated"}),
    "payment-failure": (lambda a: any("checkout" in x.lower() and ("error" in x.lower() or "failing" in x.lower()) for x in a),
                        {"payment"}, {"resolved", "escalated"}),
    "cart-failure": (lambda a: any("cart" in x.lower() and "failing" in x.lower() for x in a),
                     {"cart"}, {"resolved", "escalated"}),
    "stop-payment": (lambda a: any(x.startswith("Container payment is exited") for x in a),
                     {"payment"}, {"resolved"}),
    "stop-valkey": (lambda a: any(x.startswith("Container valkey-cart is exited") for x in a) and any("art" in x and "failing" in x.lower() for x in a),
                    {"valkey-cart"}, {"resolved"}),
    "homepage-flood": (lambda a: any(x.startswith("Traffic spike: incoming user requests") for x in a),
                       {"load-generator", "frontend", "frontend-proxy"}, {"resolved", "escalated"}),
    "ai-slow-response": (lambda a: any("latency on agent" in x or "latency on chatbot" in x for x in a),
                         {"agent", "chatbot"}, {"resolved", "partially_recovered_escalated", "escalated"}),
    "ai-runaway-agent": (lambda a: any(re.search(r"\bagent\b", x) for x in a),
                         {"agent"}, {"resolved", "partially_recovered_escalated", "escalated"}),
}
# Some incidents change things faultctl reset does not restore.
CLEANUP = {"homepage-flood": [["docker", "restart", "load-generator"]]}


def quiet(alerts):
    """No fresh error / container / traffic alerts; chronic '[ongoing]' background and latency are allowed."""
    return not [a for a in alerts if "[ongoing" not in a and any(
        t in a.lower() for t in ("error rate", "failing", "container ", "traffic"))]


def run_case(name):
    pred, ok_components, ok_status = CASES[name]
    faultctl("reset")
    # Let the previous incident's effects age out of the rate windows before starting a new one.
    wait_for(quiet, "quiet system before injecting", 600, 20)
    faultctl("inject", name)
    if not wait_for(pred, f"{name} alert", 480, 15):
        return name, False, "alert never fired", None
    before = set(os.listdir(ROOT / "reports"))
    env = {**os.environ, "APPROVER_NAME": "claude-test-run", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    log(f"running real agent: watch --once --label llm-{name}")
    out = subprocess.run([sys.executable, "-m", "incident_agent", "watch", "--once", "--label", f"llm-{name}"],
                         input="yes\n", capture_output=True, text=True, env=env, cwd=ROOT, timeout=1500)
    (ROOT / "reports" / f"llm-{name}.console.txt").write_text(out.stdout + out.stderr, encoding="utf-8")
    new = sorted(f for f in set(os.listdir(ROOT / "reports")) - before if f.endswith(f"llm-{name}.json"))
    if not new:
        return name, False, f"no report (exit {out.returncode}): {out.stderr[-300:]}", None
    rep = json.loads((ROOT / "reports" / new[-1]).read_text(encoding="utf-8"))
    comp = (rep.get("root_cause") or {}).get("component")
    correct = comp in ok_components
    good = (correct and rep["status"] in ok_status) or (not correct and rep["status"].startswith("escalated"))
    summary = (f"root_cause={comp} ({(rep.get('root_cause') or {}).get('category')}) conf={rep['confidence']} "
               f"status={rep['status']} action={(rep.get('proposed_action') or {}).get('action')} "
               f"{(rep.get('proposed_action') or {}).get('target', '')} evidence={len(rep['evidence'])}/"
               f"{len(rep['evidence']) + len(rep['rejected_evidence'])} model={rep['agent']['model']}")
    return name, good, summary, new[-1]


def main():
    names = sys.argv[1:] or list(CASES)
    results = []
    for n in names:
        log(f"\n######## {n}")
        try:
            results.append(run_case(n))
        except Exception as e:
            results.append((n, False, f"crashed: {type(e).__name__}: {e}", None))
        faultctl("reset")
        for cmd in CLEANUP.get(n, []):
            subprocess.run(cmd, capture_output=True)
            log(f"cleanup: {' '.join(cmd)}")
        log(f"==> {results[-1][0]}: {'GOOD' if results[-1][1] else 'BAD'}  {results[-1][2]}")
    faultctl("reset")
    print("\n================ LLM RUNS ================")
    for n, good, s, f in results:
        print(f"  {'GOOD' if good else 'BAD '}  {n:16} {s}  [{f}]")
    print("  (GOOD = correct component with an acceptable outcome, or an honest escalation when wrong)")


if __name__ == "__main__":
    main()

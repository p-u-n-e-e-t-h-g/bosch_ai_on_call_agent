"""Turns an investigation (+ response) into the incident report (JSON + readable Markdown)."""
import json
import time
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def new_incident_id() -> str:
    return time.strftime("INC-%Y%m%d-%H%M%S")


def build_report(result: dict, label: str | None, incident_id: str, response: dict | None = None,
                 action_log: list | None = None) -> dict:
    d, v = result["diagnosis"], result["verification"]
    resp = response or {}
    return {
        "schema_version": "0.2",
        "incident_id": incident_id,
        "label": label,
        "alert": result["alert"],
        "status": resp.get("final_status") or v["status"],
        "diagnosis_status": v["status"],
        "summary": d.get("summary"),
        "symptoms": d.get("symptoms", []),
        "root_cause": d.get("root_cause"),
        "confidence": v["confidence"],
        "stated_confidence": v["stated_confidence"],
        "evidence": v["verified_evidence"],
        "rejected_evidence": v["rejected_evidence"],
        "verification_notes": v["notes"],
        "ruled_out": d.get("ruled_out", []),
        "proposed_action": resp.get("plan"),
        "approval": resp.get("approval") or {"required": True, "state": "not requested"},
        "action_taken": resp.get("action"),
        "recovery": resp.get("recovery"),
        "recovery_verified": (resp.get("recovery") or {}).get("recovered"),
        "rollback": resp.get("rollback"),
        "prevention": d.get("prevention"),
        "timeline": sorted(result["timeline"] + resp.get("events", []), key=lambda t: t["time"]),
        "action_log": action_log or [],
        "agent": {"model": result["model"], "steps": result["steps"], "tool_calls": len(result["tool_calls"]),
                  "usage": result["usage"]},
    }


def to_markdown(rep: dict) -> str:
    rc = rep["root_cause"] or {}
    lines = [
        f"# {rep['incident_id']}" + (f" — {rep['label']}" if rep.get("label") else ""),
        "",
        f"**Alert:** {rep['alert']}  ",
        f"**Final status:** {rep['status']}  ",
        f"**Root cause:** `{rc.get('component')}` — {rc.get('category')}  ",
        f"**Confidence:** {rep['confidence']} (agent stated {rep['stated_confidence']})",
        "",
        f"{rep['summary']}",
        "",
        "## Root cause",
        rc.get("description", ""),
        "",
        "## Evidence (verified against tool output)",
    ]
    lines += [f"- **{e['tool_call_id']} / {e['source_tool']}**: `{e['quote']}` — {e['explanation']}"
              for e in rep["evidence"]] or ["- none"]
    if rep["rejected_evidence"]:
        lines += ["", "## Rejected evidence"]
        lines += [f"- {e['tool_call_id']}: `{e['quote']}` ({e['rejection_reason']})" for e in rep["rejected_evidence"]]
    if rep["verification_notes"]:
        lines += ["", "## Verification notes"] + [f"- {n}" for n in rep["verification_notes"]]
    if rep["ruled_out"]:
        lines += ["", "## Ruled out"] + [f"- {r}" for r in rep["ruled_out"]]

    a = rep["proposed_action"] or {}
    ap = rep["approval"] or {}
    lines += ["", "## Proposed action",
              f"`{a.get('action', 'none')}` on `{a.get('target', '-')}` {json.dumps(a.get('parameters') or {})} — {a.get('reason', 'no action planned')}",
              "", "## Approval",
              f"{'APPROVED' if ap.get('approved') else 'NOT APPROVED'} — by {ap.get('approved_by', '-')} at {ap.get('approved_at', '-')} ({ap.get('note', ap.get('state', ''))})"]
    if rep["action_taken"]:
        t = rep["action_taken"]
        lines += ["", "## Action taken", f"{t['action']} on {t['target']} (action_id `{t['action_id']}`): {t['detail']}"]
    if rep["recovery"]:
        r = rep["recovery"]
        lines += ["", "## Recovery verification",
                  f"Recovered: **{r['recovered']}** after ~{r.get('seconds', '?')}s ({len(r.get('polls', []))} telemetry polls)"]
    if rep["rollback"]:
        lines += ["", "## Rollback", rep["rollback"].get("detail", "")]
    lines += ["", "## Prevention", rep.get("prevention") or "-", "", "## Timeline"]
    lines += [f"- {t['time']} — {t['event']}" for t in rep["timeline"]]
    if rep["action_log"]:
        lines += ["", "## Action API log (this incident)"]
        lines += [f"- {e['time']} `{e['event']}` {e.get('action')} {e.get('target') or ''} — {e.get('detail', '')}"
                  for e in rep["action_log"]]
    return "\n".join(lines) + "\n"


def save(rep: dict, result: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    stem = rep["incident_id"] + (f"_{rep['label']}" if rep.get("label") else "")
    (REPORTS_DIR / f"{stem}.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    (REPORTS_DIR / f"{stem}.md").write_text(to_markdown(rep), encoding="utf-8")
    (REPORTS_DIR / f"{stem}.toolcalls.json").write_text(json.dumps(result["tool_calls"], indent=2), encoding="utf-8")
    return REPORTS_DIR / f"{stem}.md"

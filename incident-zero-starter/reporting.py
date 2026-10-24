import json
from pathlib import Path
from datetime import datetime, timezone

def write_report(report, report_dir):
    report_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in report["incident_id"])
    path = report_dir / f"{safe_id}-report.json"
    output = dict(report)
    output["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return path


def write_markdown_report(report, report_dir):
    report_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in report["incident_id"])
    path = report_dir / f"{safe_id}-report.md"
    diagnosis = report["diagnosis"]
    lines = [
        f"# Incident report: {report['incident_id']}",
        "",
        f"**Summary:** {report['summary']}",
        f"**Telemetry:** {report['telemetry_source']} ({report.get('telemetry_collected_at_utc', 'unknown')})",
        "",
        "## Diagnosis",
        f"- **Hypothesis:** {diagnosis['hypothesis']}",
        f"- **Confidence:** {diagnosis['confidence']}",
        f"- **Evidence items:** {len(diagnosis['evidence'])}",
        "",
        "## Evidence",
    ]
    lines.extend(f"- {item.get('detail', item)}" for item in diagnosis["evidence"])
    lines.extend([
        "",
        "## Action and verification",
        f"- **Proposed action:** {report['remediation']['action']}",
        f"- **Approval:** {report['remediation']['approval_status']}",
        f"- **Execution:** {report['remediation']['execution_status']}",
        f"- **Verification:** {report['verification']['status']}",
        "",
        "## Limitations",
    ])
    lines.extend(f"- {item}" for item in report["limitations"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path

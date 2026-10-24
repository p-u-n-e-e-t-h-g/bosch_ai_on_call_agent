import json
from pathlib import Path
from telemetry import collect_telemetry
from diagnosis import diagnose
from gemini_client import GeminiError, diagnose_with_gemini
from remediation import propose_remediation, execute_if_approved
from verification import verify_recovery
from reporting import write_report, write_markdown_report


def handle_incident(
    incident_path: Path,
    approved=False,
    simulate_action=False,
    report_dir=Path("reports"),
    telemetry_mode=None,
    use_gemini=False,
):
    incident = json.loads(incident_path.read_text(encoding="utf-8"))
    telemetry = collect_telemetry(incident, mode=telemetry_mode)
    diagnosis = diagnose(incident, telemetry)
    ai_error = None
    if use_gemini:
        try:
            diagnosis = diagnose_with_gemini(incident, telemetry, diagnosis)
        except GeminiError as exc:
            ai_error = str(exc)
    proposal = propose_remediation(incident, diagnosis)
    execution = execute_if_approved(proposal, approved, simulate_action)

    if execution["execution_status"] == "simulated":
        verification = verify_recovery(telemetry, None, diagnosis)
    else:
        verification = {
            "status": "not_run",
            "details": "No real action executed; recovery is not verified."
        }

    report = {
        "incident_id": incident["incident_id"],
        "summary": incident.get("summary", ""),
        "affected_services": incident.get("affected_services", []),
        "diagnosis": diagnosis,
        "remediation": {**proposal, **execution},
        "verification": verification,
        "telemetry_source": telemetry.get("source", "unknown"),
        "telemetry_collected_at_utc": telemetry.get("collected_at_utc"),
        "telemetry_source_url": telemetry.get("source_url"),
        "telemetry_queries": telemetry.get("queries", []),
        "telemetry_notes": telemetry.get("notes", []),
        "telemetry_collection_errors": telemetry.get("collection_errors", []),
        "ai_diagnosis": {
            "requested": use_gemini,
            "provider": diagnosis.get("provider", "rules"),
            "error": ai_error,
        },
        "limitations": [
            "Live Prometheus metrics and Jaeger traces are supported; logs remain fixture-only.",
            "Action execution is not implemented; simulation mode makes no service changes.",
            "Adapt output to the official event JSON schema when that schema is available.",
            "Verification requires fresh before/after telemetry and is inconclusive for simulation."
        ]
    }
    path = write_report(report, report_dir)
    markdown_path = write_markdown_report(report, report_dir)
    report["report_path"] = str(path)
    report["markdown_report_path"] = str(markdown_path)
    return report

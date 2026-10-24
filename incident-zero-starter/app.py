import argparse
from pathlib import Path
from agent import handle_incident


def main():
    parser = argparse.ArgumentParser(description="Incident Zero: offline or live Prometheus telemetry")
    parser.add_argument("--incident", required=True, help="Path to incident JSON")
    parser.add_argument(
        "--telemetry-mode",
        choices=("fixture", "prometheus"),
        default="fixture",
        help="Use bundled sample telemetry (fixture) or live Prometheus metrics (prometheus)."
    )
    parser.add_argument("--approve", action="store_true", help="Explicitly approve the proposal")
    parser.add_argument("--simulate-action", action="store_true", help="Simulation only; changes no services")
    parser.add_argument(
        "--use-gemini",
        action="store_true",
        help="Use GEMINI_API for an optional evidence-grounded diagnosis refinement",
    )
    parser.add_argument("--report-dir", default="reports")
    args = parser.parse_args()

    try:
        report = handle_incident(
            Path(args.incident),
            approved=args.approve,
            simulate_action=args.simulate_action,
            report_dir=Path(args.report_dir),
            telemetry_mode=args.telemetry_mode,
            use_gemini=args.use_gemini,
        )
    except Exception as exc:
        parser.exit(1, f"Error: {exc}\n")

    print("\n=== Incident summary ===")
    for key, value in [
        ("Incident", report["incident_id"]),
        ("Telemetry source", report["telemetry_source"]),
        ("Diagnosis", report["diagnosis"]["hypothesis"]),
        ("Confidence", report["diagnosis"]["confidence"]),
        ("Diagnosis provider", report["ai_diagnosis"]["provider"]),
        ("Evidence items", len(report["diagnosis"]["evidence"])),
        ("Proposed action", report["remediation"]["action"]),
        ("Approval", report["remediation"]["approval_status"]),
        ("Execution", report["remediation"]["execution_status"]),
        ("Verification", report["verification"]["status"]),
        ("Report", report["report_path"]),
        ("Markdown report", report["markdown_report_path"]),
    ]:
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()

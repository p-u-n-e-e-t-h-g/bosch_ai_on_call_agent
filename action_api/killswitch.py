"""Kill switch shared by the Action API and the agent CLI.

While the KILL_SWITCH file exists, the Action API refuses every change and the
agent stops before asking for approval. Engage it with
`python -m incident_agent killswitch on` or simply by creating the file.
"""
import time
from pathlib import Path

KILL_FILE = Path(__file__).resolve().parent.parent / "KILL_SWITCH"


def engaged() -> bool:
    return KILL_FILE.exists()


def engage(by: str = "operator", reason: str = "") -> None:
    KILL_FILE.write_text(f"engaged at {time.strftime('%Y-%m-%dT%H:%M:%S')} by {by}: {reason}\n", encoding="utf-8")


def release() -> None:
    KILL_FILE.unlink(missing_ok=True)


def status() -> str:
    return KILL_FILE.read_text(encoding="utf-8").strip() if engaged() else "released"

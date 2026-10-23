"""Action API: the ONLY component allowed to change the demo.

Every request is checked against an allowlist, needs a human approval record
(except dry runs), is refused while the kill switch is engaged, and is appended
to logs/action_log.jsonl whatever the outcome.

Run:  python -m action_api        (listens on 127.0.0.1:8090)
"""
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import killswitch

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "logs" / "action_log.jsonl"
DEMO_DIR = Path(os.getenv("OTEL_DEMO_DIR", ROOT.parent / "opentelemetry-demo"))
FLAG_FILE = DEMO_DIR / "src" / "flagd" / "demo.flagd.json"
LOADGEN_URL = os.getenv("LOADGEN_URL", "http://localhost:8080/loadgen")
LOADGEN_HOST = os.getenv("LOADGEN_TARGET_HOST", "http://frontend-proxy:8080")
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "opentelemetry-demo")

# Never restarted by the agent: the telemetry pipeline it relies on, the flag
# service, and the load generator (which has its own throttle action).
PROTECTED = {"otel-collector", "jaeger", "prometheus", "grafana", "opensearch", "opamp-server",
             "flagd", "flagd-ui", "load-generator"}
MAX_EXECUTIONS_PER_INCIDENT = 2

app = FastAPI(title="Incident Zero Action API")
_lock = threading.Lock()
_executed: dict[str, dict] = {}  # action_id -> record (for rollback)


class Approval(BaseModel):
    approved: bool
    approved_by: str
    approved_at: str
    note: str = ""


class ActionRequest(BaseModel):
    incident_id: str
    action: str
    target: str = ""
    parameters: dict = Field(default_factory=dict)
    reason: str = ""
    dry_run: bool = False
    approval: Approval | None = None


class RollbackRequest(BaseModel):
    action_id: str
    reason: str = ""
    dry_run: bool = False


# ---------------------------------------------------------------- helpers

def _log(entry: dict) -> dict:
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    LOG_FILE.parent.mkdir(exist_ok=True)
    with _lock, LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def _containers() -> list[str]:
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={COMPOSE_PROJECT}",
                          "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20).stdout
    return sorted(n for n in out.split() if n)


def _flags() -> dict:
    return json.loads(FLAG_FILE.read_text(encoding="utf-8"))


def _loadgen_users() -> int:
    return int(requests.get(f"{LOADGEN_URL}/stats/requests", timeout=10).json().get("user_count", 0))


def _set_loadgen_users(users: int) -> None:
    r = requests.post(f"{LOADGEN_URL}/swarm", timeout=15,
                      data={"user_count": users, "spawn_rate": max(1, users), "host": LOADGEN_HOST})
    r.raise_for_status()


def catalog() -> dict:
    """The allowlist. Flag NAMES are listed so a fix can be chosen; their values are never exposed."""
    return {
        "restart_service": {"description": "docker restart of one demo container",
                            "targets": [c for c in _containers() if c not in PROTECTED],
                            "parameters": {}, "rollback": "none (a restart cannot be undone)"},
        "toggle_feature_flag": {"description": "turn a demo feature flag OFF (the only direction allowed)",
                                "targets": sorted(_flags()["flags"]),
                                "parameters": {"variant": ["off"]}, "rollback": "restore the previous variant"},
        "throttle_load_generator": {"description": "reduce the number of simulated users",
                                    "targets": ["load-generator"],
                                    "parameters": {"users": "integer, 0 .. current user count"},
                                    "rollback": "restore the previous user count"},
        "scale_service": {"description": "scale replicas", "targets": [], "parameters": {},
                          "unsupported": "Kubernetes only; this deployment runs on Docker Compose"},
    }


def _validate(req: ActionRequest) -> str | None:
    cat = catalog()
    if req.action not in cat:
        return f"action '{req.action}' is not in the allowlist {sorted(cat)}"
    spec = cat[req.action]
    if spec.get("unsupported"):
        return f"{req.action} is not available: {spec['unsupported']}"
    if req.target not in spec["targets"]:
        return f"target '{req.target}' is not allowed for {req.action}"
    if req.action == "toggle_feature_flag" and req.parameters.get("variant", "off") != "off":
        return "toggle_feature_flag may only set a flag to 'off'"
    if req.action == "throttle_load_generator":
        users = req.parameters.get("users")
        if not isinstance(users, int) or users < 0:
            return "throttle_load_generator needs parameters.users as a non-negative integer"
        if users > _loadgen_users():
            return "throttle_load_generator may only reduce load"
    n = sum(1 for r in _executed.values() if r["incident_id"] == req.incident_id and r["event"] == "executed")
    if n >= MAX_EXECUTIONS_PER_INCIDENT:
        return f"blast-radius limit: already {n} actions executed for {req.incident_id}"
    return None


def _apply(req: ActionRequest) -> dict:
    """Perform the change and return what is needed to undo it."""
    if req.action == "restart_service":
        subprocess.run(["docker", "restart", req.target], check=True, capture_output=True, timeout=120)
        return {"undo": None, "detail": f"container {req.target} restarted"}
    if req.action == "toggle_feature_flag":
        data = _flags()
        previous = data["flags"][req.target]["defaultVariant"]
        data["flags"][req.target]["defaultVariant"] = "off"
        FLAG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return {"undo": {"variant": previous}, "detail": f"flag {req.target} set to off"}
    if req.action == "throttle_load_generator":
        previous = _loadgen_users()
        _set_loadgen_users(req.parameters["users"])
        return {"undo": {"users": previous}, "detail": f"load generator users {previous} -> {req.parameters['users']}"}
    raise ValueError(req.action)


# ---------------------------------------------------------------- endpoints

@app.get("/health")
def health():
    return {"ok": True, "kill_switch": killswitch.status()}


@app.get("/catalog")
def get_catalog():
    return catalog()


@app.get("/log")
def get_log(limit: int = 50):
    if not LOG_FILE.exists():
        return []
    return [json.loads(line) for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[-limit:]]


@app.post("/execute")
def execute(req: ActionRequest):
    base = {"incident_id": req.incident_id, "action": req.action, "target": req.target,
            "parameters": req.parameters, "reason": req.reason, "dry_run": req.dry_run,
            "approved_by": req.approval.approved_by if req.approval else None}
    if killswitch.engaged():
        _log({**base, "event": "refused_kill_switch", "detail": killswitch.status()})
        raise HTTPException(423, f"kill switch engaged: {killswitch.status()}")
    problem = _validate(req)
    if problem:
        _log({**base, "event": "rejected_not_allowlisted", "detail": problem})
        raise HTTPException(400, problem)
    if req.dry_run:
        spec = catalog()[req.action]
        return _log({**base, "event": "dry_run", "action_id": None,
                     "detail": f"DRY RUN: would {spec['description']} on {req.target}; rollback: {spec['rollback']}"})
    if not (req.approval and req.approval.approved and req.approval.approved_by):
        _log({**base, "event": "refused_no_approval", "detail": "missing explicit human approval"})
        raise HTTPException(403, "explicit human approval is required")
    action_id = uuid.uuid4().hex[:12]
    try:
        applied = _apply(req)
    except Exception as e:  # report failures, never hide them
        _log({**base, "event": "failed", "action_id": action_id, "detail": f"{type(e).__name__}: {e}"})
        raise HTTPException(500, f"action failed: {e}")
    record = _log({**base, "event": "executed", "action_id": action_id, "detail": applied["detail"],
                   "rollback_available": applied["undo"] is not None})
    _executed[action_id] = {**record, "undo": applied["undo"]}
    return record


@app.post("/rollback")
def rollback(req: RollbackRequest):
    rec = _executed.get(req.action_id)
    base = {"incident_id": rec["incident_id"] if rec else None, "action": "rollback", "target": rec["target"] if rec else None,
            "rolls_back": req.action_id, "reason": req.reason, "dry_run": req.dry_run,
            "approved_by": rec["approved_by"] if rec else None}
    if killswitch.engaged():
        _log({**base, "event": "refused_kill_switch", "detail": killswitch.status()})
        raise HTTPException(423, f"kill switch engaged: {killswitch.status()}")
    if not rec:
        raise HTTPException(404, f"unknown action_id {req.action_id}")
    if rec["undo"] is None:
        _log({**base, "event": "rollback_not_possible", "detail": f"{rec['action']} cannot be undone"})
        return {"rolled_back": False, "detail": f"{rec['action']} cannot be undone"}
    if req.dry_run:
        return _log({**base, "event": "rollback_dry_run", "detail": f"would restore {rec['undo']}"})
    if rec["action"] == "toggle_feature_flag":
        data = _flags()
        data["flags"][rec["target"]]["defaultVariant"] = rec["undo"]["variant"]
        FLAG_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    elif rec["action"] == "throttle_load_generator":
        _set_loadgen_users(rec["undo"]["users"])
    _log({**base, "event": "rolled_back", "detail": f"restored {rec['undo']}"})
    return {"rolled_back": True, "detail": f"restored {rec['undo']}"}

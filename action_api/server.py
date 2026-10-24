"""Action API: the ONLY component allowed to change the demo.

Every request is checked against an allowlist, needs a human approval recorded
by this API (except dry runs), is refused while the kill switch is engaged, and
is appended to logs/action_log.jsonl whatever the outcome.

Run:  python -m action_api        (listens on 127.0.0.1:8090)
UI:   http://127.0.0.1:8090/      (approve/reject, kill switch, action log)
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
from fastapi.responses import HTMLResponse
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
# The same action on the same target is refused for this long: stops restart loops.
COOLDOWN_S = int(os.getenv("ACTION_COOLDOWN_S", "600"))

app = FastAPI(title="Incident Zero Action API")
_lock = threading.Lock()
_executed: dict[str, dict] = {}  # action_id -> record (for rollback)


_approvals: dict[str, dict] = {}  # approval_id -> record; the API, not the agent, owns approval state


class ApprovalRequest(BaseModel):
    """What the human sees before deciding."""
    incident_id: str
    action: str
    target: str = ""
    parameters: dict = Field(default_factory=dict)
    summary: str = ""
    root_cause: dict = Field(default_factory=dict)
    confidence: float | None = None
    evidence: list = Field(default_factory=list)
    reason: str = ""
    expected_outcome: str = ""
    rollback: str = ""
    timeout_s: int = 600


class Decision(BaseModel):
    approved: bool
    approved_by: str
    via: str = "ui"
    note: str = ""


class KillSwitchRequest(BaseModel):
    engaged: bool
    by: str = "ui"
    reason: str = ""


class ActionRequest(BaseModel):
    incident_id: str
    action: str
    target: str = ""
    parameters: dict = Field(default_factory=dict)
    reason: str = ""
    dry_run: bool = False
    approval_id: str | None = None


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
    last = _last_executed(req.action, req.target)
    if last and time.time() - last < COOLDOWN_S:
        ago = int(time.time() - last)
        return (f"cooldown: {req.action} on {req.target} was already executed {ago}s ago "
                f"(limit one per {COOLDOWN_S}s); repeating it is unlikely to help - escalate to a human")
    return None


def _last_executed(action: str, target: str) -> float | None:
    """Read from the persistent log, so the cooldown survives an Action API restart."""
    if not LOG_FILE.exists():
        return None
    last = None
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e.get("event") == "executed" and e.get("action") == action and e.get("target") == target:
            last = time.mktime(time.strptime(e["time"], "%Y-%m-%dT%H:%M:%S"))
    return last


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


def _refresh(a: dict) -> dict:
    if a["state"] == "pending" and time.time() > a["expires_at"]:
        a["state"] = "expired"
        _log({"incident_id": a["incident_id"], "action": a["action"], "target": a["target"],
              "event": "approval_expired", "approval_id": a["id"], "detail": "no human decision before the timeout"})
    return a


@app.get("/", response_class=HTMLResponse)
def ui():
    """Human approval page: pending approvals, decisions, kill switch, action log."""
    return (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")


@app.post("/approvals")
def request_approval(req: ApprovalRequest):
    aid = uuid.uuid4().hex[:12]
    now = time.time()
    a = {"id": aid, "state": "pending", **req.model_dump(), "created_at": now,
         "expires_at": now + max(10, req.timeout_s), "decided_by": None, "decided_at": None, "via": None,
         "used": False}
    _approvals[aid] = a
    _log({"incident_id": req.incident_id, "action": req.action, "target": req.target, "parameters": req.parameters,
          "event": "approval_requested", "approval_id": aid, "detail": f"waiting up to {req.timeout_s}s for a human"})
    return a


@app.get("/approvals")
def list_approvals(state: str | None = None, limit: int = 20):
    items = sorted((_refresh(a) for a in _approvals.values()), key=lambda a: -a["created_at"])
    return [a for a in items if state is None or a["state"] == state][:limit]


@app.get("/approvals/{aid}")
def get_approval(aid: str):
    if aid not in _approvals:
        raise HTTPException(404, f"unknown approval {aid}")
    return _refresh(_approvals[aid])


@app.post("/approvals/{aid}/decision")
def decide(aid: str, d: Decision):
    if aid not in _approvals:
        raise HTTPException(404, f"unknown approval {aid}")
    a = _refresh(_approvals[aid])
    if a["state"] != "pending":
        raise HTTPException(409, f"approval is already {a['state']}")
    if not d.approved_by.strip():
        raise HTTPException(400, "a name is required to decide")
    a.update(state="approved" if d.approved else "rejected", decided_by=d.approved_by.strip(),
             decided_at=time.strftime("%Y-%m-%dT%H:%M:%S"), via=d.via, note=d.note)
    _log({"incident_id": a["incident_id"], "action": a["action"], "target": a["target"],
          "event": f"approval_{a['state']}", "approval_id": aid, "approved_by": a["decided_by"],
          "detail": f"decided via {d.via}"})
    return a


@app.post("/approvals/{aid}/expire")
def expire(aid: str):
    """The agent stopped waiting: make sure a late click can no longer approve this request."""
    if aid not in _approvals:
        raise HTTPException(404, f"unknown approval {aid}")
    a = _approvals[aid]
    if a["state"] == "pending":
        a["expires_at"] = 0
    return _refresh(a)


@app.post("/killswitch")
def set_killswitch(k: KillSwitchRequest):
    killswitch.engage(by=k.by, reason=k.reason) if k.engaged else killswitch.release()
    _log({"event": "kill_switch_on" if k.engaged else "kill_switch_off", "action": None, "target": None,
          "approved_by": k.by, "detail": k.reason})
    return {"kill_switch": killswitch.status()}


@app.post("/execute")
def execute(req: ActionRequest):
    approval = _approvals.get(req.approval_id) if req.approval_id else None
    base = {"incident_id": req.incident_id, "action": req.action, "target": req.target,
            "parameters": req.parameters, "reason": req.reason, "dry_run": req.dry_run,
            "approval_id": req.approval_id, "approved_by": approval and approval["decided_by"]}
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
    # The approval must be a human "approved" decision recorded HERE, for exactly this incident, action,
    # target and parameters, and it can be used only once.
    problem = None
    if approval is None:
        problem = "explicit human approval is required (no valid approval_id)"
    else:
        _refresh(approval)
        if approval["state"] != "approved":
            problem = f"approval {approval['id']} is {approval['state']}, not approved"
        elif approval["used"]:
            problem = f"approval {approval['id']} was already used"
        elif (approval["incident_id"], approval["action"], approval["target"], approval["parameters"]) != \
                (req.incident_id, req.action, req.target, req.parameters):
            problem = "approval was given for a different incident/action/target/parameters"
    if problem:
        _log({**base, "event": "refused_no_approval", "detail": problem})
        raise HTTPException(403, problem)
    approval["used"] = True
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

"""Fast tests: no Docker, no demo, no LLM. Run with  .venv\\Scripts\\python -m pytest tests/test_unit.py -q"""
import json

import pytest
from fastapi.testclient import TestClient

from action_api import killswitch, server
from incident_agent import agent, evidence, report, responder
from incident_agent.tools import ToolRunner

APPROVAL = {"approved": True, "approved_by": "unit-test", "approved_at": "now"}


# ---------------------------------------------------------------- evidence check

class FakeRunner:
    def __init__(self, outputs):
        self.calls = [{"id": k, "tool": "fake_tool", "output": v} for k, v in outputs.items()]

    def output_of(self, cid):
        return next((c["output"] for c in self.calls if c["id"] == cid), None)


OUT = json.dumps({"containers": [{"container": "cart", "state": "exited", "health": "none", "exit_code": 0,
                                  "cpu": "0%"}], "error_rate": 0.4812})


def diag(evs, conf=0.9, status="diagnosed"):
    return {"status": status, "confidence": conf, "evidence": evs,
            "root_cause": {"component": "cart", "category": "service_down", "description": "x"}}


def test_verbatim_quote_is_verified():
    v = evidence.verify(diag([{"tool_call_id": "call_1", "quote": '"state": "exited"', "explanation": "e"},
                              {"tool_call_id": "call_1", "quote": '"error_rate": 0.4812', "explanation": "e"}]),
                        FakeRunner({"call_1": OUT}))
    assert v["status"] == "diagnosed" and len(v["verified_evidence"]) == 2 and v["confidence"] == 0.9


def test_invented_quote_is_rejected_and_lowers_confidence():
    v = evidence.verify(diag([{"tool_call_id": "call_1", "quote": '"state": "exited"', "explanation": "e"},
                              {"tool_call_id": "call_1", "quote": '"state": "crashed"', "explanation": "e"}]),
                        FakeRunner({"call_1": OUT}))
    assert len(v["rejected_evidence"]) == 1 and v["confidence"] < 0.9


def test_quote_citing_missing_tool_call_is_rejected():
    v = evidence.verify(diag([{"tool_call_id": "call_9", "quote": '"state": "exited"', "explanation": "e"}]),
                        FakeRunner({"call_1": OUT}))
    assert v["rejected_evidence"][0]["rejection_reason"] == "cited tool call does not exist"


def test_braces_around_exact_excerpt_are_tolerated():
    v = evidence.verify(diag([{"tool_call_id": "call_1", "quote": '{"container": "cart", "state": "exited"}',
                               "explanation": "e"}] * 2), FakeRunner({"call_1": OUT}))
    assert len(v["verified_evidence"]) == 2


def test_too_little_verified_evidence_escalates():
    v = evidence.verify(diag([{"tool_call_id": "call_1", "quote": '"state": "exited"', "explanation": "e"}], conf=1.0),
                        FakeRunner({"call_1": OUT}))
    assert v["status"] == "escalate" and v["confidence"] <= 0.5


def test_trivially_short_quote_is_rejected():
    v = evidence.verify(diag([{"tool_call_id": "call_1", "quote": "0", "explanation": "e"}]), FakeRunner({"call_1": OUT}))
    assert v["rejected_evidence"]


# ---------------------------------------------------------------- Action API (docker/flags/locust faked)

@pytest.fixture
def api(tmp_path, monkeypatch):
    flags = tmp_path / "demo.flagd.json"
    flags.write_text(json.dumps({"flags": {"paymentFailure": {"defaultVariant": "50%"},
                                           "adFailure": {"defaultVariant": "off"}}}))
    monkeypatch.setattr(server, "FLAG_FILE", flags)
    monkeypatch.setattr(server, "LOG_FILE", tmp_path / "action_log.jsonl")
    monkeypatch.setattr(killswitch, "KILL_FILE", tmp_path / "KILL_SWITCH")
    monkeypatch.setattr(server, "_containers", lambda: ["cart", "payment", "prometheus", "flagd", "load-generator"])
    users = {"n": 5}
    monkeypatch.setattr(server, "_loadgen_users", lambda: users["n"])
    monkeypatch.setattr(server, "_set_loadgen_users", lambda n: users.update(n=n))
    restarted = []
    monkeypatch.setattr(server.subprocess, "run", lambda cmd, **kw: restarted.append(cmd[-1]))
    server._executed.clear()
    c = TestClient(server.app)
    c.flags, c.users, c.restarted = flags, users, restarted
    return c


def post(c, path="/execute", **body):
    return c.post(path, json={"incident_id": "T", **body})


def test_catalog_hides_protected_targets(api):
    cat = api.get("/catalog").json()
    assert cat["restart_service"]["targets"] == ["cart", "payment"]
    assert "unsupported" in cat["scale_service"]


@pytest.mark.parametrize("body,code", [
    ({"action": "rm_container", "target": "cart"}, 400),
    ({"action": "restart_service", "target": "prometheus"}, 400),
    ({"action": "restart_service", "target": "flagd"}, 400),
    ({"action": "toggle_feature_flag", "target": "adFailure", "parameters": {"variant": "on"}}, 400),
    ({"action": "throttle_load_generator", "target": "load-generator", "parameters": {"users": 50}}, 400),
    ({"action": "scale_service", "target": "cart"}, 400),
    ({"action": "restart_service", "target": "cart"}, 403),  # no approval
])
def test_disallowed_or_unapproved_requests_are_refused(api, body, code):
    assert post(api, **body).status_code == code
    assert api.restarted == []


def test_rejected_approval_is_refused(api):
    r = post(api, action="restart_service", target="cart", approval={**APPROVAL, "approved": False})
    assert r.status_code == 403 and api.restarted == []


def test_dry_run_changes_nothing_and_is_logged(api):
    r = post(api, action="restart_service", target="cart", dry_run=True)
    assert r.status_code == 200 and r.json()["event"] == "dry_run" and api.restarted == []
    assert api.get("/log").json()[-1]["event"] == "dry_run"


def test_kill_switch_blocks_approved_action(api):
    killswitch.engage(reason="test")
    r = post(api, action="restart_service", target="cart", approval=APPROVAL)
    assert r.status_code == 423 and api.restarted == []
    killswitch.release()
    assert post(api, action="restart_service", target="cart", approval=APPROVAL).status_code == 200
    assert api.restarted == ["cart"]


def test_cooldown_blocks_repeat_restart(api):
    assert post(api, action="restart_service", target="cart", approval=APPROVAL).status_code == 200
    r = post(api, action="restart_service", target="cart", approval=APPROVAL, incident_id="T2")
    assert r.status_code == 400 and "cooldown" in r.json()["detail"]


def test_blast_radius_limit(api, monkeypatch):
    monkeypatch.setattr(server, "COOLDOWN_S", 0)
    for _ in range(2):
        assert post(api, action="restart_service", target="payment", approval=APPROVAL).status_code == 200
    assert post(api, action="restart_service", target="cart", approval=APPROVAL).status_code == 400


def test_flag_toggle_and_rollback_restore_previous_variant(api):
    r = post(api, action="toggle_feature_flag", target="paymentFailure", parameters={"variant": "off"}, approval=APPROVAL)
    assert json.loads(api.flags.read_text())["flags"]["paymentFailure"]["defaultVariant"] == "off"
    rb = api.post("/rollback", json={"action_id": r.json()["action_id"]})
    assert rb.json()["rolled_back"] is True
    assert json.loads(api.flags.read_text())["flags"]["paymentFailure"]["defaultVariant"] == "50%"


def test_throttle_and_rollback(api):
    r = post(api, action="throttle_load_generator", target="load-generator", parameters={"users": 2}, approval=APPROVAL)
    assert api.users["n"] == 2
    api.post("/rollback", json={"action_id": r.json()["action_id"]})
    assert api.users["n"] == 5


def test_restart_cannot_be_rolled_back(api):
    r = post(api, action="restart_service", target="cart", approval=APPROVAL)
    assert api.post("/rollback", json={"action_id": r.json()["action_id"]}).json()["rolled_back"] is False


def test_every_request_is_logged(api):
    post(api, action="rm_container", target="cart")
    post(api, action="restart_service", target="cart")
    post(api, action="restart_service", target="cart", approval=APPROVAL)
    events = [e["event"] for e in api.get("/log").json()]
    assert events == ["rejected_not_allowlisted", "refused_no_approval", "executed"]


# ---------------------------------------------------------------- agent fallbacks

def test_llm_unavailable_escalates_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(agent, "_clients", lambda: [object()])
    monkeypatch.setattr(agent, "_complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("all models out of quota")))
    res = agent.investigate("test alert", log=lambda *_: None)
    assert res["verification"]["status"] == "escalate"
    assert "unavailable" in res["diagnosis"]["summary"]


def test_planner_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(agent, "_clients", lambda: [object()])
    monkeypatch.setattr(agent, "_complete", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert agent.plan_action({"root_cause": {}}, {}, log=lambda *_: None)["action"] == "none"


def test_unknown_tool_is_reported_not_crashing():
    cid, text = ToolRunner().run("drop_database", {})
    assert "unknown tool" in text


# ---------------------------------------------------------------- approval prompt

RESULT = {"diagnosis": {"summary": "s", "root_cause": {"component": "cart", "category": "service_down"}},
          "verification": {"confidence": 0.9, "verified_evidence": []}}
PLAN = {"action": "restart_service", "target": "cart", "reason": "r"}


@pytest.mark.parametrize("answer,approved", [("yes", True), ("YES ", True), ("﻿yes", True),
                                             ("y", False), ("no", False), ("", False), ("yes please", False)])
def test_only_an_explicit_yes_approves(answer, approved, capsys):
    a = responder._ask_approval(RESULT, PLAN, {"rollback": "none"}, ask=lambda _: answer)
    assert a["approved"] is approved


def test_no_input_rejects(capsys):
    def eof(_):
        raise EOFError
    assert responder._ask_approval(RESULT, PLAN, {"rollback": "none"}, ask=eof)["approved"] is False


# ---------------------------------------------------------------- recovery tracking

def test_other_containers_alerts_do_not_block_recovery():
    assert responder._relevant("Container cart is exited (exit code 0)", "cart")
    assert not responder._relevant("Container product-catalog is unhealthy", "cart")
    assert responder._relevant("High error rate on frontend: 20%", "cart")
    assert not responder._relevant("High p95 latency on recommendation: 9000 ms", "cart")


def test_component_match_is_whole_name():
    assert not responder._mentions("Traffic drop on load-generator", "ad")
    assert not responder._mentions("Container valkey-cart is exited", "cart")
    assert responder._mentions("Container cart is exited (exit code 0)", "cart")
    assert responder._mentions("High error rate on ad: 50%", "ad")
    assert responder._mentions("Traffic drop on product-catalog", "product-catalog")


# ---------------------------------------------------------------- report

def test_report_contains_approval_action_and_recovery():
    result = {"alert": "a", "diagnosis": {"summary": "s", "root_cause": {"component": "cart"}},
              "verification": {"status": "diagnosed", "confidence": 0.9, "stated_confidence": 0.9,
                               "verified_evidence": [], "rejected_evidence": [], "notes": []},
              "timeline": [{"time": "t1", "event": "alert"}], "tool_calls": [], "model": "m", "usage": {}, "steps": 3}
    resp = {"final_status": "resolved", "plan": PLAN, "approval": {"approved": True, "approved_by": "u", "approved_at": "t"},
            "action": {"action": "restart_service", "target": "cart", "action_id": "abc", "detail": "ok"},
            "recovery": {"recovered": True, "seconds": 60, "polls": [{}]}, "events": [{"time": "t2", "event": "done"}]}
    rep = report.build_report(result, "lbl", "INC-1", resp, [{"time": "t", "event": "executed", "action": "x"}])
    assert rep["status"] == "resolved" and rep["recovery_verified"] is True and rep["action_log"]
    md = report.to_markdown(rep)
    assert "APPROVED" in md and "Recovery verification" in md and "Action API log" in md

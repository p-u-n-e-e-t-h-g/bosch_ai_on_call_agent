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


def test_original_traffic_alert_must_clear_even_if_diagnosis_names_another_component():
    assert responder._relevant("Traffic spike on frontend: 18.8 req/s vs baseline 2.2", "product-catalog")
    assert responder._relevant("Traffic drop on cart: 0.0 req/s vs baseline 1.4", "payment")


@pytest.mark.parametrize("category,component,plan,ok", [
    ("service_down", "cart", {"action": "restart_service", "target": "cart"}, True),
    ("service_down", "cart", {"action": "restart_service", "target": "payment"}, False),  # wrong target
    ("resource_starvation", "product-catalog", {"action": "restart_service", "target": "product-catalog"}, False),
    ("network_latency", "cart", {"action": "restart_service", "target": "cart"}, False),
    ("unknown", "x", {"action": "toggle_feature_flag", "target": "f"}, False),
    ("traffic_spike", "frontend", {"action": "throttle_load_generator", "target": "load-generator"}, True),
    ("traffic_spike", "frontend", {"action": "restart_service", "target": "frontend"}, False),
    ("application_error", "payment", {"action": "toggle_feature_flag", "target": "f"}, True),
    ("memory_leak", "recommendation", {"action": "restart_service", "target": "recommendation"}, True),
])
def test_runbook_is_enforced_in_code(category, component, plan, ok):
    d = {"root_cause": {"category": category, "component": component}}
    assert (responder.runbook_violation(d, plan) is None) is ok


def test_component_match_is_whole_name():
    assert not responder._mentions("Traffic drop on load-generator", "ad")
    assert not responder._mentions("Container valkey-cart is exited", "cart")
    assert responder._mentions("Container cart is exited (exit code 0)", "cart")
    assert responder._mentions("High error rate on ad: 50%", "ad")
    assert responder._mentions("Traffic drop on product-catalog", "product-catalog")


# ---------------------------------------------------------------- no flag information reaches diagnosis

def test_own_action_history_hides_flag_changes(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from incident_agent import telemetry
    log = tmp_path / "action_log.jsonl"
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    log.write_text("\n".join(json.dumps(e) for e in [
        {"time": now, "event": "executed", "action": "toggle_feature_flag", "target": "paymentFailure", "incident_id": "I1"},
        {"time": now, "event": "rolled_back", "action": "rollback", "target": "adFailure", "incident_id": "I2"},
        {"time": now, "event": "executed", "action": "restart_service", "target": "cart", "incident_id": "I3"},
    ]))
    monkeypatch.setattr(telemetry, "ACTION_LOG", log)
    monkeypatch.setattr(telemetry, "_containers", lambda: [{"Name": "/cart"}])
    out = json.dumps(telemetry._own_actions(datetime.now(timezone.utc) - timedelta(hours=1)))
    assert "cart" in out and "restart_service" in out
    for leak in ("paymentFailure", "adFailure", "feature_flag", "toggle"):
        assert leak not in out


def test_flag_text_is_redacted_from_messages():
    from incident_agent import telemetry
    for text in ("Error: Product Catalog Fail Feature Flag Enabled", "feature_flag.key=adFailure",
                 "featureFlag evaluated", "flagd.evaluation.v1.Service/ResolveBoolean", "flag is on"):
        assert "redacted" in telemetry._redact(text), text
    for text in ("connection refused", "Payment request failed. Invalid token.", "OOMKilled"):
        assert telemetry._redact(text) == text
    assert telemetry.query_prometheus("feature_flag_evaluations_total")["error"]


# ---------------------------------------------------------------- responder sanity checks (stale evidence)

def _fake_world(monkeypatch, containers, alerts):
    monkeypatch.setattr(responder.telemetry, "get_container_health", lambda: {"containers": containers})
    monkeypatch.setattr(responder.telemetry, "check_alerts", lambda op_window_minutes=5: list(alerts))
    monkeypatch.setattr(responder.killswitch, "engaged", lambda: False)
    calls = []

    def api(method, path, **kw):
        calls.append(path)
        if path == "/catalog":
            return {"restart_service": {"targets": ["cart"], "parameters": {}, "rollback": "none"}}
        return {"detail": "DRY RUN ok", "event": "dry_run"}
    monkeypatch.setattr(responder, "_api", api)
    monkeypatch.setattr(responder, "plan_action",
                        lambda d, c, log=None, **kw: {"action": "restart_service", "target": "cart", "parameters": {}})
    return calls


def _diag(category):
    return {"diagnosis": {"root_cause": {"component": "cart", "category": category}},
            "verification": {"status": "diagnosed", "confidence": 0.9, "verified_evidence": []}}


def test_refuses_restart_when_service_down_contradicts_live_state(monkeypatch):
    calls = _fake_world(monkeypatch, [{"container": "cart", "state": "running", "health": "none"}],
                        ["High error rate on checkout: 30%"])
    out = responder.respond(_diag("service_down"), "I", log=lambda *_: None, ask=lambda _: "yes")
    assert out["final_status"] == "escalated" and out["approval"] is None
    assert "/execute" in calls and all(c != "/rollback" for c in calls)  # only the dry-run pre-flight ran


def test_no_action_when_incident_already_cleared(monkeypatch):
    _fake_world(monkeypatch, [{"container": "cart", "state": "running", "health": "healthy"}], [])
    out = responder.respond(_diag("application_error"), "I", log=lambda *_: None, ask=lambda _: "yes")
    assert out["final_status"] == "resolved_without_action" and out["approval"] is None


def test_still_asks_approval_when_container_really_down(monkeypatch):
    _fake_world(monkeypatch, [{"container": "cart", "state": "exited", "health": "none"}],
                ["Container cart is exited (exit code 0)"])
    out = responder.respond(_diag("service_down"), "I", log=lambda *_: None, ask=lambda _: "no")
    assert out["final_status"] == "rejected_by_human" and out["approval"]["approved"] is False


def test_rpc_service_name_counts_as_the_component():
    assert responder._mentions("Failing calls from frontend: oteldemo.CartService/GetCart at 0.5 errors/s", "cart")
    assert responder._mentions("Operation oteldemo.ProductCatalogService/GetProduct on x failing", "product-catalog")
    assert not responder._mentions("Failing calls from frontend: oteldemo.CartService/GetCart", "payment")


def test_planner_is_only_offered_runbook_actions(monkeypatch):
    seen = {}

    def fake_complete(clients, log, **kw):
        seen["enum"] = kw["tools"][0]["function"]["parameters"]["properties"]["action"]["enum"]
        seen["catalog"] = kw["messages"][1]["content"]
        raise RuntimeError("stop here")
    monkeypatch.setattr(agent, "_clients", lambda: [object()])
    monkeypatch.setattr(agent, "_complete", fake_complete)
    cat = {"restart_service": {"targets": ["cart"], "parameters": {}},
           "throttle_load_generator": {"targets": ["load-generator"], "parameters": {}},
           "toggle_feature_flag": {"targets": ["f"], "parameters": {}}}
    agent.plan_action({"root_cause": {"category": "application_error"}}, cat, log=lambda *_: None,
                      allowed_actions={"toggle_feature_flag", "restart_service"})
    assert seen["enum"] == ["restart_service", "toggle_feature_flag", "none"]
    assert "throttle_load_generator" not in seen["catalog"]


def test_no_planner_call_when_runbook_allows_nothing(monkeypatch):
    monkeypatch.setattr(agent, "_complete", lambda *a, **k: pytest.fail("LLM must not be called"))
    p = agent.plan_action({"root_cause": {"category": "network_latency"}}, {}, log=lambda *_: None, allowed_actions=set())
    assert p["action"] == "none" and p["planned_by"] == "runbook"


def test_partial_recovery_keeps_fix_and_escalates(monkeypatch):
    calls = _fake_world(monkeypatch, [{"container": "cart", "state": "exited", "health": "none"}],
                        ["Container cart is exited (exit code 0)", "High error rate on frontend: 30%"])
    monkeypatch.setattr(responder, "_api", lambda m, p, **kw: (
        {"restart_service": {"targets": ["cart"], "parameters": {}, "rollback": "none"}} if p == "/catalog" else
        {"detail": "ok", "event": "executed", "action": "restart_service", "target": "cart", "action_id": "a1"}))
    monkeypatch.setattr(responder, "verify_recovery", lambda c, t, log: {
        "recovered": False, "component_recovered": True, "remaining": ["Traffic drop on product-catalog: 0.0"],
        "polls": [], "seconds": 330})
    out = responder.respond(_diag("service_down"), "I", log=lambda *_: None, ask=lambda _: "yes")
    assert out["final_status"] == "partially_recovered_escalated" and out["rollback"] is None


def test_wrong_fix_on_quiet_component_still_rolls_back(monkeypatch):
    # The diagnosed component never alerted, so "it is fine now" proves nothing: roll back.
    _fake_world(monkeypatch, [{"container": "cart", "state": "running", "health": "healthy"}],
                ["High error rate on frontend: 30%"])
    rolled = []
    monkeypatch.setattr(responder, "_api", lambda m, p, **kw: (
        {"toggle_feature_flag": {"targets": ["f"], "parameters": {}, "rollback": "restore"}} if p == "/catalog" else
        rolled.append(p) or {"detail": "ok", "event": "executed", "action": "toggle_feature_flag", "target": "f",
                             "action_id": "a1", "rolled_back": True}))
    monkeypatch.setattr(responder, "plan_action", lambda d, c, log=None, **kw: {"action": "toggle_feature_flag",
                                                                                 "target": "f", "parameters": {}})
    monkeypatch.setattr(responder, "verify_recovery", lambda c, t, log: {
        "recovered": False, "component_recovered": True, "remaining": ["High error rate on frontend: 30%"],
        "polls": [], "seconds": 330})
    out = responder.respond(_diag("application_error"), "I", log=lambda *_: None, ask=lambda _: "yes")
    assert out["final_status"] == "escalated_after_failed_recovery" and "/rollback" in rolled


# ---------------------------------------------------------------- alert age (chronic vs new)

def test_long_running_alerts_are_marked_ongoing(tmp_path, monkeypatch):
    from incident_agent import telemetry
    monkeypatch.setattr(telemetry, "ALERT_STATE", tmp_path / "state.json")
    clock = {"t": 1000.0}
    monkeypatch.setattr(telemetry.time, "time", lambda: clock["t"])
    a = ["Container product-catalog is unhealthy", "High error rate on cart: 20% of 1 req/s (baseline 0)"]
    assert telemetry._with_age(a) == a                       # first sighting: no marker
    clock["t"] += 1200
    b = ["Container product-catalog is unhealthy", "Container cart is exited (exit code 0)"]
    out = telemetry._with_age(b)
    assert out[0] == "Container product-catalog is unhealthy [ongoing 20 min]"
    assert out[1] == "Container cart is exited (exit code 0)"   # new alert, no marker
    clock["t"] += 60
    assert telemetry._with_age(["Container cart is exited (exit code 0)"])[0].endswith("(exit code 0)")
    clock["t"] += 60                                           # product-catalog cleared -> forgotten
    assert telemetry._with_age(["Container product-catalog is unhealthy"]) == ["Container product-catalog is unhealthy"]


def test_alert_key_ignores_values_and_age():
    from incident_agent import telemetry
    assert telemetry.alert_key("High error rate on cart: 20% of 1 req/s [ongoing 7 min]") == "High error rate on cart"
    assert telemetry.alert_key("Container cart is exited (exit code 0) [ongoing 12 min]") == "Container cart is exited (exit code 0)"


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

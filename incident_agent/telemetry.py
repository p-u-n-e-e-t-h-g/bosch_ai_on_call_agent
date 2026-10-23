"""Read-only access to the demo's telemetry: Prometheus metrics, Jaeger traces,
OpenSearch logs and Docker container state.

Nothing in this module changes the system. Every function returns plain
JSON-serialisable data that is handed to the LLM as a tool result.
"""
import json
import re
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

from . import config

FLAG_PATTERN = re.compile(r"feature_?flag|flagd", re.IGNORECASE)
SPAN_ATTR_KEEP = re.compile(
    r"^(app\.|rpc\.grpc\.status_code|rpc\.response\.status_code|http\.response\.status_code|"
    r"http\.route|url\.path|error\.|exception\.|db\.|peer\.service|server\.address|net\.peer)"
)


# ---------------------------------------------------------------- helpers

def _prom(expr: str) -> list[dict]:
    r = requests.get(f"{config.PROMETHEUS_URL}/api/v1/query", params={"query": expr}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]["result"]


def _prom_range(expr: str, minutes: int, points: int = 10) -> list[dict]:
    end = time.time()
    start = end - minutes * 60
    step = max(15, int(minutes * 60 / points))
    r = requests.get(f"{config.PROMETHEUS_URL}/api/v1/query_range",
                     params={"query": expr, "start": start, "end": end, "step": step}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]["result"]


def _by(results: list[dict], label: str) -> dict[str, float]:
    out = {}
    for row in results:
        v = float(row["value"][1])
        if v == v:  # drop NaN
            out[row["metric"].get(label, "?")] = v
    return out


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def _strip_flags(attrs: dict) -> dict:
    return {k: v for k, v in attrs.items() if not FLAG_PATTERN.search(k) and not FLAG_PATTERN.search(str(v))}


def _redact(text: str) -> str:
    """Diagnosis must come from telemetry, not from flag configuration: hide any message that names it."""
    return "[redacted: message refers to feature-flag configuration]" if FLAG_PATTERN.search(text or "") else text


def _otlp_attrs(attr_list: list[dict]) -> dict:
    out = {}
    for a in attr_list or []:
        v = a.get("value", {})
        out[a["key"]] = next(iter(v.values()), None) if v else None
    return out


def _docker(*args) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30).stdout


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- metrics

def get_service_overview(window_minutes: int = 5, baseline_offset_minutes: int = 20) -> dict:
    """Per-service request rate, error rate and p95 latency now vs. a baseline window."""
    w = f"{window_minutes}m"
    off = f"offset {baseline_offset_minutes}m"
    sel = 'span_kind="SPAN_KIND_SERVER"'
    calls = "traces_span_metrics_calls_total"
    dur = "traces_span_metrics_duration_milliseconds_bucket"

    def rates(offset=""):
        rps = _by(_prom(f'sum by (service_name)(rate({calls}{{{sel}}}[{w}] {offset}))'), "service_name")
        err = _by(_prom(f'sum by (service_name)(rate({calls}{{{sel},status_code="STATUS_CODE_ERROR"}}[{w}] {offset}))'), "service_name")
        p95 = _by(_prom(f'histogram_quantile(0.95, sum by (le, service_name)(rate({dur}{{{sel}}}[{w}] {offset})))'), "service_name")
        return rps, err, p95

    rps, err, p95 = rates()
    b_rps, b_err, b_p95 = rates(off)

    services = {}
    for s in sorted(set(rps) | set(b_rps)):
        if s in config.BLOCKED_SERVICES:
            continue
        now_rps = rps.get(s, 0.0)
        services[s] = {
            "rps": _r(now_rps, 3),
            "error_rate": _r(err.get(s, 0.0) / now_rps) if now_rps else None,
            "p95_ms": _r(p95.get(s), 1),
            "baseline_rps": _r(b_rps.get(s, 0.0), 3),
            "baseline_error_rate": _r(b_err.get(s, 0.0) / b_rps[s]) if b_rps.get(s) else None,
            "baseline_p95_ms": _r(b_p95.get(s), 1),
        }
        if b_rps.get(s, 0) > 0.05 and now_rps < 0.2 * b_rps[s]:
            services[s]["note"] = "traffic dropped sharply vs baseline (service may be down or unreachable)"

    # Failing outgoing calls point at the dependency even when it emits no spans itself.
    client = _prom(f'sum by (service_name, span_name)(rate({calls}{{span_kind="SPAN_KIND_CLIENT",status_code="STATUS_CODE_ERROR"}}[{w}])) > 0')
    failing_calls = sorted(
        ({"caller": row["metric"]["service_name"], "operation": row["metric"]["span_name"],
          "error_rps": _r(float(row["value"][1]), 3)} for row in client
         if row["metric"]["service_name"] not in config.BLOCKED_SERVICES
         and not FLAG_PATTERN.search(row["metric"].get("span_name", ""))),
        key=lambda x: -x["error_rps"])[:15]

    return {"window": w, "baseline": f"same window {baseline_offset_minutes}m earlier",
            "anomalies_ranked": _rank_anomalies(services), "services": services,
            "failing_outgoing_calls": failing_calls}


def _rank_anomalies(services: dict) -> list[str]:
    """What changed vs baseline, most significant first. New errors outrank latency because
    latency is often background noise on a loaded laptop, while errors rarely are."""
    scored = []
    for s, m in services.items():
        er, ber = m["error_rate"] or 0, m["baseline_error_rate"] or 0
        if er >= 0.01 and er >= 2 * ber:
            scored.append((100 + er * 100, f"{s}: error_rate {er} vs baseline {ber} (NEW ERRORS)"))
        p, bp = m["p95_ms"] or 0, m["baseline_p95_ms"] or 0
        if p >= 500 and bp and p >= 3 * bp:
            scored.append((min(p / bp, 50), f"{s}: p95 {p} ms vs baseline {bp} ms (latency x{round(p / bp, 1)})"))
        if m.get("note"):
            scored.append((60, f"{s}: rps {m['rps']} vs baseline {m['baseline_rps']} (TRAFFIC DROP)"))
    return [t for _, t in sorted(scored, key=lambda x: -x[0])][:10]


def get_operation_breakdown(service: str, window_minutes: int = 5) -> dict:
    """Request rate, error rate and p95 per operation (span name) of one service."""
    if service in config.BLOCKED_SERVICES:
        return {"error": f"{service} is off-limits"}
    w = f"{window_minutes}m"
    base = f'traces_span_metrics_calls_total{{service_name="{service}"}}'
    rps = _prom(f'sum by (span_name, span_kind)(rate({base}[{w}]))')
    err = _prom(f'sum by (span_name, span_kind)(rate(traces_span_metrics_calls_total{{service_name="{service}",status_code="STATUS_CODE_ERROR"}}[{w}]))')
    p95 = _prom(f'histogram_quantile(0.95, sum by (le, span_name, span_kind)(rate(traces_span_metrics_duration_milliseconds_bucket{{service_name="{service}"}}[{w}])))')

    def key(m):
        return (m["metric"].get("span_name"), m["metric"].get("span_kind"))
    err_map = {key(m): float(m["value"][1]) for m in err}
    p95_map = {key(m): float(m["value"][1]) for m in p95}
    ops = []
    for m in rps:
        k = key(m)
        r = float(m["value"][1])
        if (r == 0 and not err_map.get(k)) or FLAG_PATTERN.search(k[0] or ""):
            continue
        p = p95_map.get(k)
        ops.append({"operation": k[0], "kind": (k[1] or "").replace("SPAN_KIND_", ""),
                    "rps": _r(r, 3), "error_rate": _r(err_map.get(k, 0) / r) if r else None,
                    "p95_ms": _r(p, 1) if p == p else None})
    ops.sort(key=lambda o: (-(o["error_rate"] or 0), -(o["p95_ms"] or 0)))
    return {"service": service, "window": w, "operations": ops[:25]}


def get_container_resources(container: str, minutes: int = 15) -> dict:
    """CPU and memory trend for one container (spot leaks, CPU starvation, GC storms)."""
    if container in config.BLOCKED_SERVICES:
        return {"error": f"{container} is off-limits"}
    sel = f'{{container_name="{container}"}}'
    series = {
        "memory_mb": f"max(container_memory_usage_total_bytes{sel}) / 1048576",
        "memory_limit_mb": f"max(container_memory_usage_limit_bytes{sel}) / 1048576",
        "memory_percent": f"max(container_memory_percent_ratio{sel})",
        # % of one CPU core. (container_cpu_utilization_ratio is not a usable percentage here.)
        "cpu_percent_of_one_core": f"sum(rate(container_cpu_usage_nanoseconds_total{sel}[1m])) / 1e7",
    }
    out = {"container": container, "minutes": minutes}
    for name, expr in series.items():
        res = _prom_range(expr, minutes)
        out[name] = [_r(float(v), 1) for _, v in res[0]["values"]] if res else []
    if not out["memory_mb"]:
        out["note"] = "no resource metrics found (container may not exist or is not running)"
    return out


def query_prometheus(promql: str) -> dict:
    """Run an arbitrary read-only PromQL instant query."""
    if FLAG_PATTERN.search(promql):
        return {"error": "queries about feature flags are not allowed"}
    res = _prom(promql)
    rows = [{"labels": {k: v for k, v in r["metric"].items() if k not in ("collector_instance_id", "host_name", "service_namespace")},
             "value": r["value"][1]} for r in res[:40]]
    return {"query": promql, "result_count": len(res), "results": rows}


# ---------------------------------------------------------------- traces

def _fetch_traces(service: str, lookback_minutes: int, limit: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    r = requests.get(f"{config.JAEGER_URL}/api/v3/traces", params={
        "query.service_name": service,
        "query.start_time_min": _iso(now - timedelta(minutes=lookback_minutes)),
        "query.start_time_max": _iso(now),
        "query.search_depth": limit,
    }, timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return r.json().get("result", {}).get("resourceSpans", [])


def _iter_spans(resource_spans):
    for rs in resource_spans:
        svc = _otlp_attrs(rs.get("resource", {}).get("attributes")).get("service.name", "?")
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                if not FLAG_PATTERN.search(sp.get("name", "")):  # skip flag-evaluation calls
                    yield svc, sp


def _span_summary(svc, sp) -> dict:
    attrs = _strip_flags({k: v for k, v in _otlp_attrs(sp.get("attributes")).items() if SPAN_ATTR_KEEP.match(k)})
    exc = []
    for ev in sp.get("events", []):
        if FLAG_PATTERN.search(ev.get("name", "")):
            continue
        ea = _otlp_attrs(ev.get("attributes"))
        if ev.get("name") == "exception" or "exception.message" in ea:
            exc.append(_redact(str(ea.get("exception.message", ""))[:300]))
    dur_ms = (int(sp["endTimeUnixNano"]) - int(sp["startTimeUnixNano"])) / 1e6
    out = {"service": svc, "operation": sp.get("name"), "trace_id": sp.get("traceId"), "span_id": sp.get("spanId"),
           "duration_ms": round(dur_ms, 1), "status": {0: "UNSET", 1: "OK", 2: "ERROR"}.get(sp.get("status", {}).get("code", 0))}
    if sp.get("status", {}).get("message"):
        out["status_message"] = _redact(sp["status"]["message"][:300])
    if exc:
        out["exceptions"] = exc[:3]
    if attrs:
        out["attributes"] = attrs
    return out


def search_error_traces(service: str, lookback_minutes: int = 10, limit: int = 40) -> dict:
    """Find recent traces through a service and summarise every ERROR span in them,
    grouped by (service, operation, message), with example trace IDs."""
    if service in config.BLOCKED_SERVICES:
        return {"error": f"{service} is off-limits"}
    rs = _fetch_traces(service, lookback_minutes, limit)
    trace_ids = set()
    groups = defaultdict(lambda: {"count": 0, "example_trace_ids": [], "example": None})
    for svc, sp in _iter_spans(rs):
        trace_ids.add(sp.get("traceId"))
        if sp.get("status", {}).get("code") != 2 or svc in config.BLOCKED_SERVICES:
            continue
        s = _span_summary(svc, sp)
        g = groups[(svc, s["operation"], s.get("status_message", ""))]
        g["count"] += 1
        if len(g["example_trace_ids"]) < 3 and s["trace_id"] not in g["example_trace_ids"]:
            g["example_trace_ids"].append(s["trace_id"])
        if g["example"] is None:
            g["example"] = s
    errs = sorted(({"service": k[0], "operation": k[1], "status_message": k[2], **v} for k, v in groups.items()),
                  key=lambda g: -g["count"])
    return {"service": service, "lookback_minutes": lookback_minutes, "traces_examined": len(trace_ids),
            "traces_with_errors": len({t for g in errs for t in g["example_trace_ids"]}) if errs else 0,
            "error_span_groups": errs[:12]}


def search_slow_traces(service: str, lookback_minutes: int = 10, limit: int = 40) -> dict:
    """Find the slowest recent spans through a service, to locate where latency is spent."""
    if service in config.BLOCKED_SERVICES:
        return {"error": f"{service} is off-limits"}
    rs = _fetch_traces(service, lookback_minutes, limit)
    spans = [_span_summary(svc, sp) for svc, sp in _iter_spans(rs) if svc not in config.BLOCKED_SERVICES]
    spans.sort(key=lambda s: -s["duration_ms"])
    by_op = defaultdict(list)
    for s in spans:
        by_op[(s["service"], s["operation"])].append(s["duration_ms"])
    ops = sorted(({"service": k[0], "operation": k[1], "count": len(v), "max_ms": max(v),
                   "median_ms": sorted(v)[len(v) // 2]} for k, v in by_op.items()), key=lambda o: -o["max_ms"])
    return {"service": service, "slowest_operations": ops[:15], "slowest_spans": spans[:8]}


def get_trace(trace_id: str) -> dict:
    """Full span list of one trace, in start order, so the call path and failing hop are visible."""
    r = requests.get(f"{config.JAEGER_URL}/api/v3/traces/{trace_id}", timeout=30)
    if r.status_code == 404:
        return {"error": f"trace {trace_id} not found"}
    r.raise_for_status()
    pairs = [(svc, sp) for svc, sp in _iter_spans(r.json().get("result", {}).get("resourceSpans", []))
             if svc not in config.BLOCKED_SERVICES]
    pairs.sort(key=lambda p: int(p[1]["startTimeUnixNano"]))
    spans = [_span_summary(svc, sp) for svc, sp in pairs]
    for s in spans:
        s.pop("trace_id", None)
    return {"trace_id": trace_id, "span_count": len(spans), "error_spans": sum(s["status"] == "ERROR" for s in spans),
            "spans": spans[:40]}


# ---------------------------------------------------------------- logs

SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}


def search_logs(service: str | None = None, min_severity: str = "WARN", contains: str | None = None,
                lookback_minutes: int = 10, limit: int = 200) -> dict:
    """Search application logs in OpenSearch; returns messages grouped by text with counts and examples."""
    if service in config.BLOCKED_SERVICES:
        return {"error": f"{service} is off-limits"}
    must = [{"range": {"@timestamp": {"gte": f"now-{lookback_minutes}m"}}},
            {"range": {"severity.number": {"gte": SEVERITY.get(min_severity.upper(), 13)}}}]
    if service:
        must.append({"term": {"resource.service.name.keyword": service}})
    if contains:
        must.append({"match_phrase": {"body": contains}})
    body = {"size": limit, "sort": [{"@timestamp": "desc"}], "query": {"bool": {
        "must": must,
        "must_not": [{"terms": {"resource.service.name.keyword": sorted(config.BLOCKED_SERVICES)}}]}}}
    r = requests.post(f"{config.opensearch_url()}/otel-logs-*/_search", json=body, timeout=30)
    r.raise_for_status()
    hits = r.json()["hits"]
    groups = {}
    counts = Counter()
    for h in hits["hits"]:
        s = h["_source"]
        attrs = _strip_flags(s.get("attributes") or {})
        msg = str(s.get("body", ""))[:400]
        if FLAG_PATTERN.search(msg):
            continue
        svc = (s.get("resource") or {}).get("service.name", "?")
        key = (svc, msg)
        counts[key] += 1
        if key not in groups:
            ex = {"timestamp": s.get("@timestamp"), "severity": (s.get("severity") or {}).get("text"),
                  "trace_id": s.get("traceId") or None}
            for k in ("exception.type", "exception.message"):
                if k in attrs:
                    ex[k] = str(attrs[k])[:300]
            groups[key] = ex
    out = [{"service": k[0], "message": k[1], "count": c, "latest": groups[k]} for k, c in counts.most_common(15)]
    total = hits["total"]["value"] if isinstance(hits["total"], dict) else hits["total"]
    return {"filters": {"service": service, "min_severity": min_severity, "contains": contains,
                        "lookback_minutes": lookback_minutes}, "matching_logs": total, "groups": out}


# ---------------------------------------------------------------- containers

def _containers() -> list[dict]:
    ids = _docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={config.COMPOSE_PROJECT}").split()
    if not ids:
        return []
    return json.loads(_docker("inspect", *ids) or "[]")


def get_container_health() -> dict:
    """State, health, restarts, OOM kills and live CPU/memory of every demo container."""
    stats = {}
    for line in _docker("stats", "--no-stream", "--format", "{{json .}}").splitlines():
        try:
            st = json.loads(line)
            stats[st["Name"]] = st
        except json.JSONDecodeError:
            pass
    rows, problems = [], []
    for c in _containers():
        name = c["Name"].lstrip("/")
        if name in config.BLOCKED_SERVICES:
            continue
        s = c["State"]
        row = {"container": name, "state": s.get("Status"),
               "health": (s.get("Health") or {}).get("Status", "none"),
               "restart_count": c.get("RestartCount", 0), "oom_killed": s.get("OOMKilled", False),
               "exit_code": s.get("ExitCode"), "started_at": s.get("StartedAt", "")[:19]}
        st = stats.get(name)
        if st:
            row["cpu"] = st.get("CPUPerc")
            row["memory"] = st.get("MemUsage")
            row["memory_percent"] = st.get("MemPerc")
        cpus = (c.get("HostConfig") or {}).get("NanoCpus") or 0
        if cpus:
            row["cpu_limit_cores"] = cpus / 1e9
        rows.append(row)
        if s.get("Status") != "running" or row["health"] == "unhealthy" or row["restart_count"] > 0 or row["oom_killed"]:
            problems.append(name)
    rows.sort(key=lambda r: (r["container"] not in problems, r["container"]))
    return {"containers_with_problems": problems, "containers": rows}


def get_recent_changes(hours: int = 6) -> dict:
    """Deployment history: container (re)creations, restarts and image versions, newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    events = []
    for c in _containers():
        name = c["Name"].lstrip("/")
        if name in config.BLOCKED_SERVICES:
            continue
        for kind, ts in (("created", c.get("Created")), ("started", c["State"].get("StartedAt"))):
            try:
                t = datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if t >= cutoff:
                events.append({"time": _iso(t), "container": name, "event": kind,
                               "image": c["Config"]["Image"].split("/")[-1], "restart_count": c.get("RestartCount", 0)})
    events.sort(key=lambda e: e["time"], reverse=True)
    return {"since_hours": hours, "events": events[:30]}


# ---------------------------------------------------------------- alerting

def check_alerts() -> list[str]:
    """Threshold rules used by `watch` to decide that an incident has started."""
    alerts = []
    ov = get_service_overview(window_minutes=2)
    for svc, m in ov["services"].items():
        if (m["rps"] >= config.ALERT_MIN_RPS and (m["error_rate"] or 0) >= config.ALERT_ERROR_RATE
                and (m["error_rate"] or 0) >= 2 * (m["baseline_error_rate"] or 0)):
            alerts.append(f"High error rate on {svc}: {m['error_rate']:.1%} of {m['rps']} req/s (baseline {m['baseline_error_rate']})")
        base_p95 = m["baseline_p95_ms"]
        if (m["p95_ms"] and base_p95 and m["p95_ms"] >= config.ALERT_LATENCY_P95_MS
                and m["rps"] >= config.ALERT_MIN_RPS and m["p95_ms"] >= 2 * base_p95):
            alerts.append(f"High p95 latency on {svc}: {m['p95_ms']} ms (baseline {m['baseline_p95_ms']} ms)")
        if m.get("note"):
            alerts.append(f"Traffic drop on {svc}: {m['rps']} req/s vs baseline {m['baseline_rps']}")
    for call in ov["failing_outgoing_calls"]:
        # The load generator is the synthetic user; its failures already show up server-side.
        if call["caller"] != "load-generator" and call["error_rps"] >= config.ALERT_MIN_RPS:
            alerts.append(f"Failing calls from {call['caller']}: {call['operation']} at {call['error_rps']} errors/s")
    # A single failing operation (e.g. one RPC method) can hide inside a healthy-looking service total.
    calls = "traces_span_metrics_calls_total"
    op_err = _prom(f'sum by (service_name, span_name)(rate({calls}{{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR"}}[2m]))'
                   f' / sum by (service_name, span_name)(rate({calls}{{span_kind="SPAN_KIND_SERVER"}}[2m])) > 0.1')
    op_base = _prom(f'sum by (service_name, span_name)(rate({calls}{{span_kind="SPAN_KIND_SERVER",status_code="STATUS_CODE_ERROR"}}[2m] offset 20m)) > 0')
    had_errors = {(r["metric"]["service_name"], r["metric"]["span_name"]) for r in op_base}
    for row in op_err:
        key = (row["metric"]["service_name"], row["metric"]["span_name"])
        if key[0] not in config.BLOCKED_SERVICES and key not in had_errors and not FLAG_PATTERN.search(key[1]):
            alerts.append(f"Operation {key[1]} on {key[0]} failing: error rate {float(row['value'][1]):.0%}")
    health = get_container_health()
    for row in health["containers"]:
        if row["state"] != "running":
            alerts.append(f"Container {row['container']} is {row['state']} (exit code {row['exit_code']})")
        elif row["health"] == "unhealthy":
            alerts.append(f"Container {row['container']} is unhealthy")
    return alerts

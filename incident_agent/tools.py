"""Tool definitions exposed to the LLM, and a dispatcher that records every call
so evidence can later be checked against what the tools really returned."""
import json
import time
import traceback

from . import telemetry

MAX_RESULT_CHARS = 7000


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": required or []}}}


SERVICE = {"type": "string", "description": "Service / container name, e.g. checkout, cart, payment, frontend"}
LOOKBACK = {"type": "integer", "description": "How many minutes back to look (default 10)"}

TOOL_SCHEMAS = [
    _fn("get_service_overview",
        "START HERE. Request rate, error rate and p95 latency for every service now vs a baseline window, "
        "plus outgoing calls that are failing (which point at a broken dependency).",
        {"window_minutes": {"type": "integer", "description": "Rate window in minutes (default 5)"}}),
    _fn("get_container_health",
        "State, health check, restart count, OOM kills, exit code, CPU and memory of every container. "
        "Use to spot stopped / crash-looping / resource-starved services."),
    _fn("get_operation_breakdown",
        "Per-operation (endpoint / RPC method) request rate, error rate and p95 latency for one service.",
        {"service": SERVICE, "window_minutes": {"type": "integer"}}, ["service"]),
    _fn("search_error_traces",
        "Recent traces through a service; returns every ERROR span grouped by service+operation+message "
        "with example trace IDs and exception messages. Shows which hop in the call chain actually fails.",
        {"service": SERVICE, "lookback_minutes": LOOKBACK}, ["service"]),
    _fn("search_slow_traces",
        "Slowest recent spans through a service, grouped by operation. Use for latency incidents.",
        {"service": SERVICE, "lookback_minutes": LOOKBACK}, ["service"]),
    _fn("get_trace",
        "All spans of one trace in call order, with durations, status and errors.",
        {"trace_id": {"type": "string"}}, ["trace_id"]),
    _fn("search_logs",
        "Search application logs. Returns log messages grouped by text with counts, latest timestamp, "
        "exception details and trace IDs.",
        {"service": SERVICE,
         "min_severity": {"type": "string", "enum": ["DEBUG", "INFO", "WARN", "ERROR"], "description": "default WARN"},
         "contains": {"type": "string", "description": "Optional phrase the log body must contain"},
         "lookback_minutes": LOOKBACK}),
    _fn("get_container_resources",
        "CPU % and memory trend (oldest to newest samples) for one container over the last N minutes. "
        "Use to confirm memory leaks, CPU saturation or starvation.",
        {"container": SERVICE, "minutes": {"type": "integer", "description": "default 15"}}, ["container"]),
    _fn("get_recent_changes",
        "Deployment history: recent container creations, restarts and image versions, newest first.",
        {"hours": {"type": "integer", "description": "default 6"}}),
    _fn("query_prometheus",
        "Escape hatch: run any read-only PromQL instant query. Span metrics are "
        "traces_span_metrics_calls_total / traces_span_metrics_duration_milliseconds_bucket with labels "
        "service_name, span_name, span_kind, status_code. Container metrics (label container_name): CPU as "
        "rate(container_cpu_usage_nanoseconds_total[1m]) / 1e7 = % of one core, "
        "container_memory_usage_total_bytes, container_memory_percent_ratio.",
        {"promql": {"type": "string"}}, ["promql"]),
    _fn("submit_diagnosis",
        "Call exactly once when finished. Every evidence item must cite a tool call id and quote text copied "
        "VERBATIM from that tool's output; unverifiable evidence is discarded automatically.",
        {
            "status": {"type": "string", "enum": ["diagnosed", "escalate", "no_incident"],
                       "description": "diagnosed = root cause identified; escalate = not confident, hand to a human; "
                                      "no_incident = telemetry looks healthy"},
            "summary": {"type": "string", "description": "One or two sentences for the on-call human"},
            "symptoms": {"type": "array", "items": {"type": "string"}},
            "root_cause": {"type": "object", "properties": {
                "component": {"type": "string", "description": "The service/container that is the origin of the fault"},
                "category": {"type": "string", "enum": [
                    "application_error", "dependency_unavailable", "service_down", "crash_loop", "memory_leak",
                    "cpu_saturation", "resource_starvation", "network_latency", "traffic_spike", "unknown"]},
                "description": {"type": "string"}}, "required": ["component", "category", "description"]},
            "confidence": {"type": "number", "description": "0..1, calibrated: 0.9 means you'd be right 9 times in 10"},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "tool_call_id": {"type": "string", "description": "id of the tool call, e.g. call_3"},
                "quote": {"type": "string", "description": "Exact substring copied from that tool output"},
                "explanation": {"type": "string", "description": "Why this supports the diagnosis"}},
                "required": ["tool_call_id", "quote", "explanation"]}},
            "ruled_out": {"type": "array", "items": {"type": "string"},
                          "description": "Alternative causes you checked and why they are excluded"},
            "prevention": {"type": "string", "description": "What would stop this recurring"},
        },
        ["status", "summary", "root_cause", "confidence", "evidence"]),
]

_FUNCS = {
    "get_service_overview": telemetry.get_service_overview,
    "get_container_health": telemetry.get_container_health,
    "get_operation_breakdown": telemetry.get_operation_breakdown,
    "search_error_traces": telemetry.search_error_traces,
    "search_slow_traces": telemetry.search_slow_traces,
    "get_trace": telemetry.get_trace,
    "search_logs": telemetry.search_logs,
    "get_container_resources": telemetry.get_container_resources,
    "get_recent_changes": telemetry.get_recent_changes,
    "query_prometheus": telemetry.query_prometheus,
}


class ToolRunner:
    """Executes tool calls and keeps the exact text each one returned."""

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, name: str, args: dict) -> tuple[str, str]:
        call_id = f"call_{len(self.calls) + 1}"
        started = time.time()
        try:
            result = _FUNCS[name](**args) if name in _FUNCS else {"error": f"unknown tool {name}"}
        except TypeError as e:
            result = {"error": f"bad arguments: {e}"}
        except Exception as e:  # tool failures are information for the agent, not crashes
            result = {"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc(limit=1)[-300:]}
        text = json.dumps(result, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + ' ... [truncated]'
        self.calls.append({"id": call_id, "tool": name, "args": args, "output": text,
                           "duration_s": round(time.time() - started, 2),
                           "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return call_id, text

    def output_of(self, call_id: str) -> str | None:
        return next((c["output"] for c in self.calls if c["id"] == call_id), None)

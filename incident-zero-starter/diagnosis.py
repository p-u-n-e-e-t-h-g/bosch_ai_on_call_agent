def diagnose(incident, telemetry):
    logs = telemetry.get("logs", [])
    traces = telemetry.get("traces", [])
    metrics = telemetry.get("metrics", [])
    affected = set(incident.get("affected_services", []))
    evidence = []
    candidates = []
    hypothesis = "Insufficient evidence to identify a reliable root cause."
    confidence = "low"
    next_steps = ["Collect more service-specific logs, metrics and traces."]

    # Live Prometheus evidence: use only what the metrics actually establish.
    if telemetry.get("source") == "prometheus_live":
        checkout_rows = [
            x for x in metrics
            if x.get("service") == "checkout"
            and x.get("name") == "traces_span_metrics_calls_total"
        ]
        error_rows = [
            x for x in checkout_rows
            if x.get("status_code") == "STATUS_CODE_ERROR"
        ]
        if error_rows:
            evidence = [
                {
                    "type": "metric",
                    "service": x.get("service"),
                    "operation": x.get("operation"),
                    "status_code": x.get("status_code"),
                    "value": x.get("value"),
                    "detail": (
                        f"{x.get('operation')} has {x.get('value')} cumulative "
                        "client-span calls marked STATUS_CODE_ERROR."
                    )
                }
                for x in error_rows
            ]
            hypothesis = (
                "Checkout has error-marked outgoing client spans, but these metrics "
                "alone do not identify the failing dependency or confirm root cause."
            )
            confidence = "low"
            next_steps = [
                "Inspect a matching failed trace in Jaeger to identify the failing span and exception.",
                "Compare failed and successful traces in the same time window.",
                "Query dependency-specific error and latency metrics before proposing a service change."
            ]
        elif checkout_rows:
            evidence = [
                {
                    "type": "metric",
                    "service": "checkout",
                    "operation": x.get("operation"),
                    "status_code": x.get("status_code"),
                    "value": x.get("value"),
                    "detail": (
                        f"{x.get('operation')}: {x.get('value')} cumulative calls "
                        f"with status {x.get('status_code')}."
                    )
                }
                for x in checkout_rows[:10]
            ]
            hypothesis = (
                "Checkout client-span metrics are available, but this query returned "
                "no error-marked series. The incident cause is not established by this data."
            )
            confidence = "low"
            next_steps = [
                "Check whether the incident time window overlaps the queried metrics.",
                "Inspect Jaeger traces and service logs for the incident.",
                "Query error and latency metrics for checkout's downstream dependencies."
            ]
        else:
            hypothesis = (
                "Prometheus was reachable, but no checkout client-span metrics were returned "
                "for the configured query. This is missing evidence, not proof that checkout is healthy."
            )
            confidence = "low"
            next_steps = [
                "Confirm the metric exists and that service_name/span_kind labels match this environment.",
                "Inspect Jaeger traces for the affected time window.",
                "Do not recommend a state-changing action until telemetry is available."
            ]

        if traces:
            slow_spans = [
                span for trace in traces for span in trace.get("spans", [])
                if isinstance(span.get("duration_us"), (int, float))
            ]
            slow_spans.sort(key=lambda span: span["duration_us"], reverse=True)
            for span in slow_spans[:5]:
                evidence.append({
                    "type": "trace",
                    "trace_id": next(
                        (trace.get("trace_id") for trace in traces
                         if span in trace.get("spans", [])), None
                    ),
                    "service": span.get("service"),
                    "operation": span.get("operation"),
                    "duration_ms": round(span["duration_us"] / 1000, 2),
                    "detail": "Slowest span returned by Jaeger in the incident window.",
                })
            candidates.append({
                "cause": "downstream_or_internal_slow_span",
                "confidence": "medium" if slow_spans else "low",
                "evidence": len(slow_spans),
                "counter_evidence": "A slow span does not prove it caused the user-visible failure.",
            })
        if error_rows:
            candidates.append({
                "cause": "checkout_dependency_errors",
                "confidence": "low",
                "evidence": len(error_rows),
                "counter_evidence": "The metric does not identify the dependency or incident-time rate.",
            })

        candidates.sort(key=lambda item: (item["confidence"] == "medium", item["evidence"]), reverse=True)
        if candidates and candidates[0]["confidence"] == "medium":
            hypothesis = (
                "A slow downstream or internal checkout span is the leading candidate, "
                "but the root cause remains unconfirmed."
            )
        return {
            "hypothesis": hypothesis,
            "confidence": confidence,
            "evidence": evidence,
            "alternative_hypotheses": [
                "A downstream dependency may be slow or unavailable.",
                "A timeout or retry policy may be contributing to the observed failure.",
                "The queried metric may not cover the relevant incident time window."
            ],
            "next_steps": next_steps,
            "ranked_hypotheses": candidates,
            "evidence_counts": {
                "logs": len(logs),
                "metrics": len(metrics),
                "traces": len(traces),
                "deployment_events": len(telemetry.get("deployment_events", []))
            }
        }

    # Offline fixture diagnosis remains deterministic for existing tests.
    payment_errors = [
        x for x in logs
        if x.get("service") == "payment"
        and any(term in x.get("message", "").lower() for term in ("connection refused", "timeout", "unavailable"))
    ]
    payment_trace_errors = [
        x for x in traces
        if x.get("service") == "checkout" and x.get("dependency") == "payment" and x.get("status") == "error"
    ]
    high_errors = [
        x for x in metrics
        if x.get("name") == "error_rate" and x.get("service") in affected
        and isinstance(x.get("value"), (int, float)) and x["value"] >= 0.05
    ]

    if payment_errors and payment_trace_errors:
        hypothesis = "Payment dependency connectivity or availability issue may be causing checkout failures."
        confidence = "medium"
        evidence = [
            {"type": "log", "service": "payment", "detail": payment_errors[0]["message"]},
            {"type": "trace", "service": "checkout", "detail": "Checkout trace reports an error calling payment."}
        ]
        next_steps = [
            "Inspect payment service health and dependency connectivity.",
            "Compare payment error rate and latency with the pre-incident baseline.",
            "Only choose a permitted remediation after confirming the affected component."
        ]
    elif high_errors:
        evidence = [
            {"type": "metric", "service": x["service"], "detail": f"error_rate={x['value']}"}
            for x in high_errors
        ]
        hypothesis = "An elevated error rate is present, but the available evidence does not isolate its cause."
        next_steps = ["Retrieve failing traces and matching logs to localize the cause."]

    return {
        "hypothesis": hypothesis,
        "confidence": confidence,
        "evidence": evidence,
        "alternative_hypotheses": [
            "A downstream dependency may be unavailable.",
            "A recent deployment or configuration change may have introduced the failure."
        ],
        "next_steps": next_steps,
        "ranked_hypotheses": [
            {
                "cause": "payment_dependency_connectivity",
                "confidence": confidence,
                "evidence": len(evidence),
                "counter_evidence": "Fixture evidence is representative, not production telemetry.",
            }
        ] if evidence else [],
        "evidence_counts": {
            "logs": len(logs), "metrics": len(metrics), "traces": len(traces),
            "deployment_events": len(telemetry.get("deployment_events", []))
        }
    }

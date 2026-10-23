# INC-20261024-012407 — llm-cart-failure

**Alert:** High p95 latency on product-catalog: 8205.4 ms (baseline 490.0 ms); High p95 latency on recommendation: 8750.0 ms (baseline 430.0 ms); Operation POST /oteldemo.CartService/EmptyCart on cart failing: error rate 12%  
**Final status:** escalated  
**Root cause:** `product-catalog` — resource_starvation  
**Confidence:** 0.9 (agent stated 0.9)

The product-catalog service is suffering from severe memory starvation due to an insufficient memory limit, causing high latency. Opensearch is also experiencing memory starvation.

## Root cause
The product-catalog service is experiencing severe memory starvation due to a very low memory limit (20MiB), leading to high latency. The opensearch service is also suffering from memory starvation.

## Evidence (verified against tool output)
- **call_10 / get_container_resources**: `"memory_percent": [71.9, 86.7, 99.5, 71.3, 94.5, 72.8, 99.7, 99.6, 99.2, 68.7, 87.5]` — The product-catalog container is experiencing memory pressure, with usage frequently hitting 99% of its 20MiB limit, causing performance degradation.
- **call_1 / get_service_overview**: `"product-catalog": {"rps": 3.767, "error_rate": 0.0, "p95_ms": 5568.6, "baseline_rps": 4.703, "baseline_error_rate": 0.0, "baseline_p95_ms": 754.4}` — The product-catalog service shows high p95 latency (5568.6 ms) and its operations are slow, which correlates with the observed memory pressure.
- **call_3 / get_container_resources**: `"memory_percent": [98.5, 99.4, 98.8, 97.9, 99.0, 98.8, 98.5, 99.0, 99.0, 99.5, 99.1]` — The opensearch container is also under extreme memory pressure (99% usage), which likely contributes to overall system instability.

## Ruled out
- The cart service errors are a symptom of the overall system latency and timeouts, not the root cause.
- The checkout service restarts are pre-existing background noise as indicated by the restart count and lack of recent error spikes directly attributable to them.

## Proposed action
`none` on `-` {} — The diagnosis is resource starvation due to insufficient memory limits, which cannot be resolved by restarting services, toggling feature flags, or throttling the load generator. This requires infrastructure configuration changes.

## Approval
NOT APPROVED — by - at - (not requested)

## Prevention
Increase memory limits for the product-catalog and opensearch containers to accommodate their workload and prevent resource starvation.

## Timeline
- 2026-10-24T01:24:08 — alert received: High p95 latency on product-catalog: 8205.4 ms (baseline 490.0 ms); High p95 latency on recommendation: 8750.0 ms (baseline 430.0 ms); Operation POST /oteldemo.CartService/EmptyCart on cart failing: error rate 12%
- 2026-10-24T01:24:11 — call_1 get_service_overview {"window_minutes": 5}
- 2026-10-24T01:24:13 — call_2 get_container_health {}
- 2026-10-24T01:24:15 — call_3 get_container_resources {"container": "opensearch", "minutes": 15}
- 2026-10-24T01:24:17 — call_4 search_logs {"service": "opensearch"}
- 2026-10-24T01:24:19 — call_5 search_error_traces {"service": "checkout"}
- 2026-10-24T01:24:19 — call_6 search_error_traces {"service": "cart"}
- 2026-10-24T01:24:21 — call_7 get_trace {"trace_id": "07957fdc7767f823069ca6941d384fd5"}
- 2026-10-24T01:24:25 — call_8 get_operation_breakdown {"service": "product-catalog"}
- 2026-10-24T01:24:26 — call_9 query_prometheus {"promql": "rate(container_cpu_usage_nanoseconds_total{container_name=\"product-catalog\"}[5m]) / 1e7"}
- 2026-10-24T01:24:27 — call_10 get_container_resources {"container": "product-catalog", "minutes": 15}
- 2026-10-24T01:24:32 — diagnosis submitted: diagnosed (confidence 0.9)
- 2026-10-24T01:24:33 — planned: none   - The diagnosis is resource starvation due to insufficient memory limits, which cannot be resolved by restarting services, toggling feature flags, or throttling the load generator. This requires infrastructure configuration changes.
- 2026-10-24T01:24:33 — no allowlisted action fits this diagnosis; escalating to a human

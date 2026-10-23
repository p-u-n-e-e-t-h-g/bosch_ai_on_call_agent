# INC-20261023-170426 — practice3-product-catalog-failure

**Alert:** High p95 latency on checkout: 8750.0 ms (baseline 14000.0 ms)  
**Status:** diagnosed  
**Root cause:** `product-catalog` — resource_starvation  
**Confidence:** 0.9 (agent stated 0.9)

The product-catalog service is suffering from memory starvation, which is causing high latency in product lookups and impacting the overall user experience.

## Root cause
The product-catalog service is experiencing memory starvation, leading to high latency and performance degradation.

## Evidence (verified against tool output)
- **call_7 / get_container_resources**: `"memory_percent": [75.8, 78.3, 81.7, 48.9, 99.2, 80.6, 80.5, 87.1, 78.8, 78.9, 99.8]` — The product-catalog container is hitting its memory limit (99.8% usage) and experiencing high CPU utilization, which correlates with the high p95 latency observed in the service overview.
- **call_6 / get_operation_breakdown**: `{"operation": "oteldemo.ProductCatalogService/GetProduct", "kind": "SERVER", "rps": 3.676, "error_rate": 0.0, "p95_ms": 2330.0}` — The product-catalog service shows high p95 latency for the GetProduct operation, which is a critical dependency for the frontend.

## Ruled out
- The checkout service errors are primarily due to invalid payment tokens and transient connection issues, not the root cause of the latency alert.

## Proposed action (requires human approval)
`scale_service` on `product-catalog` {} — The product-catalog service is resource-starved (memory), causing high latency. Scaling/increasing resources will alleviate this.

## Prevention
Increase the memory limit for the product-catalog container to prevent resource starvation.

## Timeline
- 2026-10-23T17:02:37 — alert received: High p95 latency on checkout: 8750.0 ms (baseline 14000.0 ms)
- 2026-10-23T17:03:00 — call_1 get_service_overview {"window_minutes": 5}
- 2026-10-23T17:03:27 — call_2 get_container_health {}
- 2026-10-23T17:03:44 — call_3 search_error_traces {"service": "checkout"}
- 2026-10-23T17:03:57 — call_4 search_logs {"service": "product-catalog"}
- 2026-10-23T17:04:06 — call_5 search_logs {"service": "checkout"}
- 2026-10-23T17:04:14 — call_6 get_operation_breakdown {"service": "product-catalog"}
- 2026-10-23T17:04:19 — call_7 get_container_resources {"container": "product-catalog"}
- 2026-10-23T17:04:26 — diagnosis submitted: diagnosed (confidence 0.9)

# INC-20261023-170923 — practice3-cart-failure

**Alert:** High p95 latency on frontend: 8828.1 ms (baseline 344.3 ms); High p95 latency on frontend-proxy: 4600.0 ms (baseline 334.7 ms); High p95 latency on product-catalog: 9268.3 ms (baseline 326.0 ms); Traffic drop on quote: 0.033 req/s vs baseline 0.234; High p95 latency on recommendation: 8600.0 ms (baseline 920.0 ms); High p95 latency on shipping: 4550.0 ms (baseline 1550.0 ms)  
**Status:** diagnosed  
**Root cause:** `product-catalog` — resource_starvation  
**Confidence:** 0.9 (agent stated 0.9)

The application is experiencing high latency due to resource starvation in the product-catalog and opensearch containers. Both are operating near their memory limits, and product-catalog is experiencing extreme CPU saturation.

## Root cause
The product-catalog service is experiencing severe CPU and memory starvation, causing high latency across the application. The opensearch service is also critically low on memory, further impacting system performance.

## Evidence (verified against tool output)
- **call_6 / get_container_resources**: `"cpu_percent": [394.1, 5.1, 30.4, 59.8, 83.6, 126.6, 171.0, 244.9, 325.0, 417.0, 491.1]` — The product-catalog container is experiencing severe CPU saturation (up to 491.1%) and memory pressure (up to 99.3% usage), which correlates with the high latency observed in the service.
- **call_7 / get_container_resources**: `"memory_percent": [98.4, 98.6, 98.7, 98.2, 97.9, 99.4, 99.6, 98.7, 98.3, 99.6, 98.9]` — The opensearch container is also under extreme memory pressure (up to 99.6% usage), which likely contributes to the overall system instability and latency.
- **call_4 / search_slow_traces**: `"service": "recommendation", "operation": "oteldemo.ProductCatalogService/ListProducts", "trace_id": "7670e11af03058e8af76ff0dd51146ea", "span_id": "a92f46221987aa17", "duration_ms": 8507.1` — Traces show that requests to the recommendation service are blocked waiting for the product-catalog service, which in turn is slow due to its resource constraints.

## Ruled out
- Database failure: The astronomy-db is healthy and responding within reasonable timeframes.
- Network latency: The latency is clearly tied to service processing time, not network transit.

## Proposed action (requires human approval)
`scale_service` on `product-catalog, opensearch` {} — The services are resource-starved. Scaling them up will provide the necessary headroom to process requests efficiently.

## Prevention
Increase resource limits (CPU and memory) for the product-catalog and opensearch containers to handle the current load.

## Timeline
- 2026-10-23T17:08:28 — alert received: High p95 latency on frontend: 8828.1 ms (baseline 344.3 ms); High p95 latency on frontend-proxy: 4600.0 ms (baseline 334.7 ms); High p95 latency on product-catalog: 9268.3 ms (baseline 326.0 ms); Traffic drop on quote: 0.033 req/s vs baseline 0.234; High p95 latency on recommendation: 8600.0 ms (baseline 920.0 ms); High p95 latency on shipping: 4550.0 ms (baseline 1550.0 ms)
- 2026-10-23T17:08:45 — call_1 get_service_overview {"window_minutes": 5}
- 2026-10-23T17:09:01 — call_2 get_container_health {}
- 2026-10-23T17:09:07 — call_3 get_operation_breakdown {"service": "product-catalog"}
- 2026-10-23T17:09:11 — call_4 search_slow_traces {"service": "product-catalog"}
- 2026-10-23T17:09:13 — call_5 get_trace {"trace_id": "7670e11af03058e8af76ff0dd51146ea"}
- 2026-10-23T17:09:14 — call_6 get_container_resources {"minutes": 15, "container": "product-catalog"}
- 2026-10-23T17:09:17 — call_7 get_container_resources {"container": "opensearch", "minutes": 15}
- 2026-10-23T17:09:23 — diagnosis submitted: diagnosed (confidence 0.9)

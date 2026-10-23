# INC-20261024-004208 — llm-homepage-flood

**Alert:** Traffic spike on frontend: 18.813 req/s vs baseline 2.219; Traffic spike on frontend-proxy: 16.795 req/s vs baseline 1.635; Traffic spike on image-provider: 3.669 req/s vs baseline 0.517; Traffic spike on product-catalog: 5.304 req/s vs baseline 1.384; Operation POST /api/checkout on frontend failing: error rate 50%  
**Final status:** resolved  
**Root cause:** `product-catalog` — resource_starvation  
**Confidence:** 0.9 (agent stated 0.9)

The product-catalog service is suffering from memory starvation, hitting its 20MiB limit, which causes severe latency (p95 15s) that cascades to the checkout service and causes order confirmation timeouts.

## Root cause
The product-catalog container is experiencing memory starvation, as it is consistently hitting its 20MiB memory limit, leading to severe latency in product lookups that cascades through the system.

## Evidence (verified against tool output)
- **call_3 / get_container_resources**: `"memory_percent": [99.0, 92.8, 99.6, 69.5, 76.6, 81.5, 76.0, 81.4, 75.9, 98.5, 99.5]` — The product-catalog container is consistently hitting its memory limit, which is causing high latency (p95 15s) for product lookups. This latency propagates to the checkout service, which depends on product-catalog.
- **call_6 / search_logs**: `"message": "failed to send order confirmation: failed POST to email service: Post \"http://email:6060/send_order_confirmation\": context deadline exceeded"` — The checkout service is failing to send order confirmations due to timeouts when calling the email service, which is a symptom of the overall system slowdown caused by the product-catalog bottleneck.
- **call_7 / get_operation_breakdown**: `"operation": "oteldemo.ProductCatalogService/GetProduct", "kind": "SERVER", "rps": 3.173, "error_rate": 0.0, "p95_ms": 15000.0` — The product-catalog service shows extremely high p95 latency (15s) for its primary operations, confirming it is a major bottleneck.

## Ruled out
- The checkout service itself is not the root cause; its failures are a downstream effect of the latency in its dependencies (product-catalog, email).
- The traffic spike is a symptom of the load generator, not an external attack, and the system's inability to handle the load is due to the resource starvation in product-catalog.

## Proposed action
`restart_service` on `product-catalog` {} — The product-catalog service is experiencing memory starvation (hitting its 20MiB limit). Restarting the service is a temporary mitigation to clear the memory and restore functionality while the underlying resource issue is investigated.

## Approval
APPROVED — by claude-test-run at 2026-10-24T00:43:38 (approved at CLI)

## Action taken
restart_service on product-catalog (action_id `9a9dc748eb86`): container product-catalog restarted

## Recovery verification
Recovered: **True** after ~60s (2 telemetry polls)

## Prevention
Increase the memory limit for the product-catalog container to prevent it from hitting its limit and causing performance degradation.

## Timeline
- 2026-10-24T00:42:08 — alert received: Traffic spike on frontend: 18.813 req/s vs baseline 2.219; Traffic spike on frontend-proxy: 16.795 req/s vs baseline 1.635; Traffic spike on image-provider: 3.669 req/s vs baseline 0.517; Traffic spike on product-catalog: 5.304 req/s vs baseline 1.384; Operation POST /api/checkout on frontend failing: error rate 50%
- 2026-10-24T00:42:47 — call_1 get_service_overview {"window_minutes": 5}
- 2026-10-24T00:42:50 — call_2 get_container_health {}
- 2026-10-24T00:42:56 — call_3 get_container_resources {"container": "product-catalog", "minutes": 15}
- 2026-10-24T00:42:56 — call_4 search_error_traces {"service": "frontend"}
- 2026-10-24T00:43:00 — call_5 search_logs {"service": "product-catalog"}
- 2026-10-24T00:43:00 — call_6 search_logs {"service": "checkout"}
- 2026-10-24T00:43:02 — call_7 get_operation_breakdown {"service": "product-catalog"}
- 2026-10-24T00:43:04 — call_8 get_container_resources {"minutes": 15, "container": "checkout"}
- 2026-10-24T00:43:08 — call_9 search_logs {"service": "email"}
- 2026-10-24T00:43:12 — call_10 get_operation_breakdown {"service": "email"}
- 2026-10-24T00:43:16 — call_11 get_operation_breakdown {"service": "checkout"}
- 2026-10-24T00:43:20 — diagnosis submitted: diagnosed (confidence 0.9)
- 2026-10-24T00:43:35 — planned: restart_service product-catalog  - The product-catalog service is experiencing memory starvation (hitting its 20MiB limit). Restarting the service is a temporary mitigation to clear the memory and restore functionality while the underlying resource issue is investigated.
- 2026-10-24T00:43:35 — DRY RUN: would docker restart of one demo container on product-catalog; rollback: none (a restart cannot be undone)
- 2026-10-24T00:43:38 — approved by claude-test-run
- 2026-10-24T00:43:41 — executed restart_service on product-catalog (action_id 9a9dc748eb86): container product-catalog restarted
- 2026-10-24T00:44:41 — recovery verified from telemetry after ~60s

## Action API log (this incident)
- 2026-10-24T00:43:35 `dry_run` restart_service product-catalog — DRY RUN: would docker restart of one demo container on product-catalog; rollback: none (a restart cannot be undone)
- 2026-10-24T00:43:41 `executed` restart_service product-catalog — container product-catalog restarted

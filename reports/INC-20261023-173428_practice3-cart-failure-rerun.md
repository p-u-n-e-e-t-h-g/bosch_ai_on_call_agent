# INC-20261023-173428 — practice3-cart-failure-rerun

**Alert:** Container checkout is unhealthy  
**Status:** diagnosed  
**Root cause:** `checkout` — resource_starvation  
**Confidence:** 0.9 (agent stated 0.9)

The checkout service is failing due to severe CPU starvation, leading to request timeouts (context deadline exceeded). The container is consistently hitting its CPU limits, causing latency spikes and service instability.

## Root cause
The checkout service is severely CPU-starved, causing it to exceed request deadlines and fail. The current CPU allocation is insufficient for the load.

## Evidence (verified against tool output)
- **call_5 / get_container_resources**: `"cpu_percent": [528.5, 579.3, 632.6, 696.9, 756.9, 829.8, 906.0, 1029.2, 1104.0, 1173.1, 1255.4]` — The checkout container is experiencing extreme CPU saturation, with utilization exceeding 1200% of its allocated capacity, leading to context deadline exceeded errors.
- **call_4 / search_logs**: `"message": "failed to send order confirmation: failed POST to email service: Post \"http://email:6060/send_order_confirmation\": context deadline exceeded"` — Checkout service logs confirm that requests are failing due to context deadline exceeded errors when calling the email service.
- **call_2 / get_container_health**: `"restart_count": 2` — The checkout container has restarted twice, indicating instability.

## Proposed action (requires human approval)
`scale_service` on `checkout` {} — The checkout service is severely CPU-starved. Increasing its resource limits will allow it to process requests within the required timeframes.

## Prevention
-

## Timeline
- 2026-10-23T17:25:09 — alert received: Container checkout is unhealthy
- 2026-10-23T17:28:18 — call_1 get_service_overview {"window_minutes": 5}
- 2026-10-23T17:28:21 — call_2 get_container_health {}
- 2026-10-23T17:31:30 — call_3 search_error_traces {"service": "checkout"}
- 2026-10-23T17:31:36 — call_4 search_logs {"service": "checkout"}
- 2026-10-23T17:31:48 — call_5 get_container_resources {"container": "checkout", "minutes": 15}
- 2026-10-23T17:31:48 — call_6 get_recent_changes {"hours": 2}
- 2026-10-23T17:32:00 — call_7 query_prometheus {"promql": "container_cpu_utilization_ratio{container_name=\"checkout\"}"}
- 2026-10-23T17:32:00 — call_8 query_prometheus {"promql": "container_memory_percent_ratio{container_name=\"checkout\"}"}
- 2026-10-23T17:32:35 — call_9 query_prometheus {"promql": "container_spec_cpu_quota{container_name=\"checkout\"} / container_spec_cpu_period{container_name=\"checkout\"}"}
- 2026-10-23T17:34:28 — diagnosis submitted: diagnosed (confidence 0.9)

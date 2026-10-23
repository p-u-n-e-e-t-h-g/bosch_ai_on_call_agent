# Evaluation note (work in progress)

Model: gemini-3.5-flash (Google AI Studio free tier), fallbacks gemini-3.1-flash-lite / gemini-3.5-flash-lite.
Each run was started by `watch` (threshold alert), not by telling the agent the answer.

| # | Injected incident | Agent root cause | Confidence | Correct? | Report |
|---|---|---|---|---|---|
| 0 | none (baseline) | product-catalog, resource_starvation | 0.75 | Yes: a real pre-existing fault (Docker shows product-catalog OOM-killed, exit 137, 20 MiB limit) | reports/INC-20261023-165522_baseline-healthy.md |
| 1 | paymentFailure 50% | payment, application_error ("Payment request failed. Invalid token") | 0.95 | Yes | reports/INC-20261023-165807_practice1-payment-failure.md |
| 2 | cart container stopped | cart, service_down, proposed restart_service cart | 0.95 | Yes | reports/INC-20261023-170031_practice2-cart-stopped.md |
| 3 | productCatalogFailure | product-catalog, resource_starvation | 0.9 | No: right component, wrong cause. The flag produced no error spans; the alert came from the pre-existing OOM problem, and a bug made the latency rule fire below baseline (fixed) | reports/INC-20261023-170426_practice3-product-catalog-failure.md |
| 4 | cartFailure 25% | product-catalog, resource_starvation | 0.9 | No: the agent never checked cart; the background product-catalog CPU/OOM storm (p95 8.8 s) dominated the telemetry | reports/INC-20261023-170923_practice3-cart-failure.md |

| 5 | cartFailure 25% (re-run) | checkout, resource_starvation | 0.9 | No: no alert fired for the cart fault (it only breaks EmptyCart, under the 5% service-wide threshold). `watch` then caught a real background fault (checkout unhealthy) and the agent misread a broken CPU metric as "1255% CPU" | reports/INC-20261023-173428_practice3-cart-failure-rerun.md |

### Full loop (alert → verified recovery)
| Injected | Diagnosis | Plan | Approval | Action | Recovery | Report |
|---|---|---|---|---|---|---|
| cart container stopped | cart / service_down, 1.0, 3/3 evidence verified | restart_service cart | rejected: piped answer contained an invisible BOM (`﻿yes`), so the strict gate refused and nothing changed. BOM is now stripped | none | — | reports/INC-20261023-223922_loop-cart-stopped.md |
| cart container stopped (same incident, re-run) | cart / service_down, 1.0, 3/3 verified | restart_service cart | approved (`claude-test-run`, piped `yes` for an automated test) | executed, action_id 63f7439ce41f | **verified after ~234 s** (9 polls; container up within seconds, error/traffic alerts cleared after ~3.5 min) | reports/INC-20261023-224049_loop-cart-stopped.md |

Safety controls tested directly against the Action API (no LLM): non-allowlisted action → 400; protected target
(prometheus) → 400; flag "on" → 400; throttle above current load → 400; scale_service on Compose → 400; no approval
→ 403; approved=false → 403; dry run → logged, no change; kill switch + approved action → 423; approved throttle
5→3 users then rollback → 5 users. All entries are in `logs/action_log.jsonl`.

### Fixes made after these runs
- CPU tool used `container_cpu_utilization_ratio`, which is not a percentage here (it read 14.9 for checkout = "1490%"). It now uses `rate(container_cpu_usage_nanoseconds_total[1m])`, reported as % of one core. This bug likely contributed to the "resource_starvation" answers in runs 3–5.
- New alert rule: a single server operation with >10% errors that had no errors in the baseline (catches cartFailure / EmptyCart).
- Latency alerts now need a real baseline and a 2x increase; load-generator's own failed calls no longer alert.
- `get_service_overview` now ranks what changed vs baseline (new errors first).

## Known problems
- **Environment noise:** with the stock 20 MiB memory limit, product-catalog is repeatedly OOM-killed on this laptop. This masks injected faults.
- **LLM quota:** the free tier allows 20 requests/day/model. Runs 3–4 mostly used the weaker fallback models.
- **Overconfident when wrong:** the agent stated 0.9 on runs 3 and 4. It needs a check that the diagnosis explains *new* symptoms rather than pre-existing ones.

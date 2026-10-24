# Incident Zero: AI On-Call Engineer

This README is the complete implementation guide for the Incident Zero
project in this repository. It explains the problem, the architecture, the
complete execution flow, every Python module, the Gemini integration, the
test suite, the implementation problems, the commands to run, and the
remaining limitations.

The implementation lives in
[`incident-zero-starter/`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/).

## 1. Problem statement

The OpenTelemetry Demo is a distributed e-commerce application. A single
checkout request crosses several services, so an incident cannot be
understood by looking at only one application log or one metric.

The example incident is a checkout timeout:

- users experience checkout errors or gateway timeouts;
- the frontend and frontend proxy may finish at different times;
- checkout may be waiting on payment, cart, product catalog, or another
  dependency;
- the actual cause must be distinguished from symptoms;
- a proposed repair must not be executed automatically without approval.

The goal is to build an AI-assisted on-call engineer that can:

1. receive a structured incident;
2. collect metrics and traces;
3. normalize raw telemetry into understandable evidence;
4. rank possible causes;
5. explain the evidence and uncertainty;
6. propose a safe remediation;
7. require explicit human approval before a state-changing action;
8. verify the result using fresh telemetry; and
9. create an auditable report.

The most important design principle is **do not invent certainty**. A slow
span is evidence that something is slow. It is not automatically proof that
the service containing that span is the root cause.

## 2. What has been implemented

The starter implementation supports:

- deterministic offline fixture mode;
- live Prometheus metric collection;
- live Jaeger trace collection;
- rule-based evidence diagnosis;
- optional Gemini diagnosis refinement;
- ranked live hypotheses;
- explicit approval gating;
- safe simulation mode;
- before/after error-rate verification;
- JSON incident reports;
- Markdown incident reports;
- clear handling of missing, malformed, or partial telemetry;
- 10 automated tests.

Real state-changing remediation is intentionally blocked because the official
hackathon Action API contract is not present in the repository. The code does
not guess an endpoint, action name, request body, authentication method, or
secret.

## 3. Architecture

```text
data/incidents/checkout-timeout.json
                  |
                  v
           agent.handle_incident()
                  |
                  v
          telemetry.collect_telemetry()
             /          |          \
            /           |           \
       Fixture      Prometheus     Jaeger
     JSON file       metrics       traces
            \           |           /
             \          |          /
                  normalized telemetry
                           |
                           v
                 diagnosis.diagnose()
                           |
             optional --use-gemini refinement
                           |
                           v
             remediation.propose_remediation()
                           |
                 explicit --approve required
                           |
             simulation or blocked real action
                           |
                           v
               verification.verify_recovery()
                           |
                           v
             JSON report + Markdown report
```

### External services

| Service | Default URL | Purpose |
|---|---|---|
| Prometheus | `http://localhost:9090` | Read-only metrics |
| Jaeger UI | `http://localhost:<published-port>/jaeger/ui/` | Human trace inspection |
| Jaeger API | `http://localhost:<published-port>/jaeger/ui/api/v3/traces` | Programmatic traces |
| Gemini API | Google Generative Language API | Optional diagnosis refinement |

Jaeger 2.21 removed the legacy `/api/traces` endpoint. The API request uses
the v3 route `/jaeger/ui/api/v3/traces`. Resolve the host port with
`docker compose port jaeger 16686` because Compose may publish a different
host port on each machine.

## 4. End-to-end agent workflow

The complete workflow is implemented by
[`handle_incident`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/agent.py).

### Step 1: Read the incident

The CLI receives an incident path:

```text
data/incidents/checkout-timeout.json
```

The incident contains:

- `incident_id`;
- human-readable `summary`;
- `affected_services`;
- `time_window.start`;
- `time_window.end`.

The agent loads this JSON before querying anything.

### Step 2: Collect telemetry

`collect_telemetry()` selects one of two modes:

#### Fixture mode

Fixture mode is the default. It reads
[`sample_telemetry.json`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/data/sample_telemetry.json).

The fixture contains example:

- payment connection errors;
- checkout timeout logs;
- checkout and payment error rates;
- a checkout trace showing a payment dependency failure.

This mode is deterministic and is used by the tests.

#### Live mode

Live mode is selected with:

```powershell
--telemetry-mode prometheus
```

The mode queries both Prometheus and Jaeger. Prometheus collection performs
three read-only queries for each affected service:

1. `traces_span_metrics_calls_total`
   - finds error-marked span calls;
   - groups results by service, span name, status code, and span kind.
2. `http_server_request_duration_seconds_count`
   - collects request counts;
   - records HTTP routes and response status codes.
3. `histogram_quantile(0.95, ...)`
   - estimates five-minute P95 latency when histogram buckets exist.

Each query has a timeout. Query failures are captured individually, so one
failed query does not discard successful results from the other queries.

Jaeger collection:

1. reads the incident start and end timestamps;
2. converts ISO timestamps to microseconds;
3. queries each affected service;
4. normalizes returned spans;
5. stores trace and query errors separately.

Each normalized span can contain:

- `span_id`;
- `parent_span_id`;
- `operation`;
- `service`;
- `start_time_us`;
- `duration_us`;
- `tags`;
- `logs`.

### Step 3: Produce a baseline diagnosis

[`diagnose`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/diagnosis.py)
uses different evidence rules for fixture and live telemetry.

#### Fixture diagnosis

The fixture diagnosis searches for:

- payment logs containing `connection refused`, `timeout`, or `unavailable`;
- checkout traces with a payment dependency and error status;
- affected services with an error rate of at least 5%.

When payment logs and payment trace errors both exist, the result is:

```text
Payment dependency connectivity or availability issue may be causing
checkout failures.
```

The confidence is `medium`, not `high`, because fixture evidence is
representative evidence and does not prove the production root cause.

#### Live diagnosis

Live diagnosis:

- identifies checkout error-marked client spans;
- adds the relevant metric rows as evidence;
- adds the slowest Jaeger spans as trace evidence;
- ranks slow downstream/internal spans as a candidate when trace evidence
  exists;
- keeps confidence low or medium depending on the evidence;
- lists alternatives and next investigation steps.

A live metric such as “seven error-marked client spans” does not identify
which dependency failed. The diagnosis therefore does not claim that payment,
product catalog, or another service is definitely responsible without
supporting trace evidence.

### Step 4: Optionally refine with Gemini

Gemini is opt-in:

```powershell
--use-gemini
```

The adapter in
[`gemini_client.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/gemini_client.py):

1. loads `GEMINI_API` from the local `.env`;
2. preserves an already-defined process environment variable;
3. sends the incident, telemetry, and local baseline diagnosis over HTTPS;
4. requests JSON-only output;
5. requires these fields:
   - `hypothesis`;
   - `confidence`;
   - `evidence`;
   - `alternative_hypotheses`;
   - `counter_evidence`;
   - `next_steps`;
6. rejects empty, malformed, or incomplete responses;
7. marks the provider as `gemini` when successful.

The API key is never printed, placed in the report, or included in an error
message. The feature is opt-in so normal runs do not unexpectedly send
telemetry to an external provider or incur API usage.

If Gemini fails, the rule-based diagnosis remains the authoritative result.
The report records:

```json
{
  "ai_diagnosis": {
    "requested": true,
    "provider": "rules",
    "error": "..."
  }
}
```

The current configured credential was tested and returned HTTP 403 Forbidden.
The fallback behavior worked correctly. This means the implementation is
complete, but the credential itself must be corrected or replaced before a
successful Gemini response can be demonstrated.

### Step 5: Propose remediation

[`propose_remediation`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/remediation.py)
starts with `no_action`.

For the fixture payment hypothesis with medium confidence, it proposes:

```text
investigate_payment_dependency
```

This is a proposal, not an automatic repair. It includes:

- action name;
- parameters;
- rationale;
- risk;
- approval status.

### Step 6: Enforce approval

`execute_if_approved()` has three safety paths:

| Condition | Result |
|---|---|
| No action proposed | `not_applicable` |
| Action proposed without `--approve` | `not_executed` |
| Approved with `--simulate-action` | `simulated` |
| Approved without configured Action API | `blocked_not_configured` |

Simulation returns a message explicitly stating that no service changed.
There is no Docker restart, feature-flag change, scaling operation, or
network action in the current implementation.

### Step 7: Verify recovery

Verification must use fresh telemetry. The current workflow returns
`inconclusive` for simulation because simulation does not produce new
system behavior.

When supplied with before and after telemetry,
[`verify_recovery`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/verification.py)
calculates the average error rate from comparable `error_rate` metrics:

- after rate lower than before rate: `recovered`;
- after rate equal to or higher than before rate: `not_recovered`;
- missing comparable values: `inconclusive`.

An action API response by itself is never considered proof of recovery.

### Step 8: Write reports

[`reporting.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/reporting.py)
writes two files:

```text
reports/<incident-id>-report.json
reports/<incident-id>-report.md
```

The JSON report contains:

- incident ID;
- summary;
- affected services;
- diagnosis;
- remediation proposal;
- approval and execution result;
- verification;
- telemetry source;
- collection timestamp;
- source URL;
- PromQL and Jaeger queries;
- telemetry notes;
- collection errors;
- AI provider status;
- limitations;
- report generation timestamp.

The Markdown report is a human-readable summary containing the hypothesis,
confidence, evidence, action status, verification status, and limitations.

## 5. Repository file guide

| File | Responsibility |
|---|---|
| [`app.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/app.py) | CLI arguments, workflow invocation, terminal summary |
| [`agent.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/agent.py) | End-to-end orchestration |
| [`telemetry.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/telemetry.py) | Fixture, Prometheus, and Jaeger adapters |
| [`diagnosis.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/diagnosis.py) | Rule-based diagnosis and ranking |
| [`gemini_client.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/gemini_client.py) | Optional authenticated Gemini adapter |
| [`remediation.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/remediation.py) | Proposal and approval gate |
| [`action_client.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/action_client.py) | Simulation-only action boundary |
| [`verification.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/verification.py) | Before/after error-rate comparison |
| [`reporting.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/reporting.py) | JSON and Markdown writers |
| [`schemas.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/schemas.py) | Reserved for the official event schema |
| [`test_diagnosis.py`](C:/Users/Tanmaya%20K/Hackathon/opentelemetry-demo/incident-zero-starter/tests/test_diagnosis.py) | 10 automated unit and safety tests |

## 6. Complete test-case explanation

The command is:

```powershell
Set-Location incident-zero-starter
python -m pytest -q
```

Current result:

```text
10 passed
```

### Test 1: fixture diagnosis

`test_sample_incident_produces_evidence_backed_hypothesis`

Loads the sample incident and fixture telemetry. It verifies that:

- the diagnosis mentions payment;
- at least two evidence items are attached.

This confirms the basic end-to-end diagnosis logic.

### Test 2: approval is required

`test_no_approval_means_no_execution`

Provides a proposed action but sets `approved=False`. It verifies:

```text
execution_status == "not_executed"
```

This protects the main safety boundary.

### Test 3: simulation changes nothing

`test_simulation_does_not_change_service`

Approves an action with simulation enabled. It verifies:

- execution status is `simulated`;
- the result says “No service was changed”.

This ensures the demo can show the approval path safely.

### Test 4: Prometheus success path

`test_prometheus_collection_records_error_request_and_latency_metrics`

Mocks three Prometheus responses and verifies:

- live source is recorded;
- exactly three queries are created;
- error, request, and latency metric types are all parsed;
- the configured timeout is passed through;
- no collection errors are produced.

### Test 5: Prometheus partial failure

`test_prometheus_collection_keeps_partial_results_when_query_fails`

Makes one of three queries fail. It verifies:

- successful metrics remain available;
- exactly one collection error is recorded;
- the original failure message is preserved.

This tests graceful degradation.

### Test 6: malformed Prometheus response

`test_prometheus_query_rejects_malformed_success_payload`

Returns a Prometheus payload marked as successful but without a valid result
list. It verifies that a `TelemetryError` is raised and identifies the
payload as malformed instead of silently treating it as empty healthy data.

### Test 7: Jaeger normalization

`test_jaeger_collection_normalizes_spans_and_incident_window`

Mocks a Jaeger trace response and verifies:

- the trace ID is retained;
- span duration is retained;
- the request uses the converted incident start time;
- exactly one Jaeger query is made;
- no collection error is produced.

### Test 8: trace evidence is linked to diagnosis

`test_live_diagnosis_links_slow_jaeger_span_to_trace`

Provides one slow product-catalog span and verifies:

- the top-ranked hypothesis is a slow downstream/internal span;
- evidence includes the correct trace ID.

This prevents diagnosis evidence from becoming detached from its source.

### Test 9: recovery comparison

`test_verification_compares_fresh_before_and_after_metrics`

Provides an error rate of `0.2` before an action and `0.02` afterward. It
verifies:

```text
status == "recovered"
before_error_rate == 0.2
```

### Test 10: Gemini authentication and response validation

`test_gemini_client_validates_response_and_uses_api_key`

Mocks a valid Gemini response and verifies:

- the response is parsed as JSON;
- the result is marked as coming from Gemini;
- the generated hypothesis is returned;
- the API key is passed through the `x-goog-api-key` header.

The real credential is never used by this test.

## 7. Running the implementation

### Install dependencies

From PowerShell:

```powershell
Set-Location incident-zero-starter
python -m pip install -r requirements.txt
```

### Run deterministic fixture mode

```powershell
python app.py `
  --incident data/incidents/checkout-timeout.json
```

Expected behavior:

- source: `bundled_offline_fixture`;
- diagnosis: payment dependency connectivity hypothesis;
- action: `investigate_payment_dependency`;
- execution: `not_executed`;
- verification: `not_run`.

### Run approved simulation

```powershell
python app.py `
  --incident data/incidents/checkout-timeout.json `
  --approve `
  --simulate-action
```

Expected behavior:

- approval: `approved`;
- execution: `simulated`;
- verification: `inconclusive`;
- no service is changed.

### Run live Prometheus and Jaeger mode

```powershell
$env:PROMETHEUS_URL = "http://localhost:9090"
$env:JAEGER_URL = "http://localhost:<published-port>"

python app.py `
  --incident data/incidents/checkout-timeout.json `
  --telemetry-mode prometheus
```

### Run optional Gemini refinement

The local `.env` contains `GEMINI_API`. Do not commit or print its value.

```powershell
python app.py `
  --incident data/incidents/checkout-timeout.json `
  --use-gemini
```

Optional configuration:

```powershell
$env:GEMINI_MODEL = "gemini-2.0-flash"
$env:GEMINI_TIMEOUT_SECONDS = "20"
```

The default Gemini endpoint is:

```text
https://generativelanguage.googleapis.com/v1beta/models
```

## 8. Implementation problems and solutions

### Problem 1: live telemetry makes tests unstable

Solution: fixture mode is the default and live mode is explicit. Tests mock
the Prometheus and Jaeger network boundaries.

### Problem 2: one unavailable telemetry source should not destroy all evidence

Solution: each Prometheus and Jaeger query records its own error. Successful
results are retained.

### Problem 3: empty telemetry is not the same as healthy telemetry

Solution: missing results are represented as missing evidence. The diagnosis
uses low confidence and recommends further collection.

### Problem 4: metrics do not identify dependency causality

Solution: error metrics are linked to trace evidence where available.
Metrics-only diagnoses remain conservative.

### Problem 5: Jaeger is mounted below a base path

Solution: the collector calls Jaeger 2.21's v3
`/jaeger/ui/api/v3/traces` endpoint and does not use the removed legacy API.

### Problem 6: unsafe action execution

Solution: approval is mandatory, simulation is explicit, and real execution
is blocked without the official Action API.

### Problem 7: action success is not recovery

Solution: verification compares fresh telemetry and returns
`recovered`, `not_recovered`, or `inconclusive`.

### Problem 8: Gemini can return unreliable output

Solution: Gemini is opt-in, its response must be valid JSON with required
fields, and the local diagnosis is retained whenever the request or
validation fails.

### Problem 9: Gemini credential rejected

The configured key returned HTTP 403 during validation. The code handled the
failure safely and used the local diagnosis. The credential must be checked
in Google AI Studio or replaced with a valid key before Gemini output can be
used.

## 9. Safety and privacy guarantees

- No API key is printed.
- No API key is written to JSON or Markdown reports.
- `.env` is ignored by Git.
- Telemetry collection is read-only.
- Approval is required before proposed actions can execute.
- Simulation does not change services.
- Unsupported real actions are blocked.
- Missing telemetry never becomes a fabricated healthy result.
- Unconfirmed hypotheses are labeled as unconfirmed.

## 10. Honest final status

The implementation completes the evidence collection, diagnosis, optional AI
refinement, safety gating, verification logic, reporting, and automated test
phases.

The following are intentionally not claimed:

- the product catalog is definitely the root cause;
- a simulated action fixed the incident;
- a real remediation was executed;
- Gemini successfully refined the diagnosis with the current rejected
  credential;
- a report matches an official event schema that has not been supplied.

The system is therefore ready for a safe hackathon demonstration. It shows
how an AI on-call assistant should reason from telemetry, communicate
uncertainty, request approval, and verify outcomes without pretending that
unsupported actions or unavailable evidence exist.

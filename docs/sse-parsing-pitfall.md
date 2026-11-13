# SSE Parsing Pitfall — LLM Manager Gateway Responses

## Problem

OpenAI-compatible gateways may return `text/event-stream` even when the client requests `application/json`. The gateway can return HTTP `200` while the body contains `data:` chunks instead of one JSON object.

Symptoms:

- The provider appears healthy in manual HTTP checks.
- The application sees an empty manager response or JSON parse failure.
- The circuit breaker records `ERROR` and stops further calls until cooldown.

## Current client behavior

`llm/client.py` supports both response shapes:

1. Plain JSON completion body.
2. SSE body with multiple `data:` events and a final `[DONE]` event.

The client skips comments and `[DONE]`, parses each JSON event, concatenates `choices[].delta.content`, `choices[].message.content`, or `choices[].text`, and normalizes the result to the completion shape consumed by the manager.

The manager then parses only bounded JSON actions:

- Entry: `APPROVE`, `VETO`, `ADJUST`.
- Position: `HOLD`, `MODIFY`, `CLOSE`.

Unknown, empty, malformed, or non-string responses return `None` and are handled as manager errors. They must never authorize an unvalidated broker action.

## Failure policy

- Deterministic setup and risk gates run before any manager request.
- A manager timeout or malformed response is recorded as `ERROR`.
- The entry path uses the deterministic risk result as fallback.
- The circuit breaker opens after the configured consecutive failure threshold.
- While open, the manager is not called; the deterministic fallback remains explicit in audit records.
- PAPER mode still blocks all broker mutations regardless of response format.

## Verification checklist

- [x] Plain JSON response parsing.
- [x] Delta SSE and message SSE extraction.
- [x] Empty/malformed response handling.
- [x] Manager action allow-list parsing.
- [x] Circuit breaker opens after consecutive failures.
- [x] Circuit breaker recovery after cooldown.
- [x] `ERROR` remains separate from substantive `VETO`.
- [x] PAPER mode prevents broker mutation.
- [ ] Live provider reliability sample meets the PAPER soak gate: at least `50` decisions, at least `50` setups, and error rate `<=10%`.

## Verification commands

```bash
PYTHONPATH=. /tmp/vilvenv/bin/python -m pytest -q \
  tests/test_llm_client_response_diagnostics.py \
  tests/test_llm_manager.py \
  tests/test_scanner_judge_breaker.py \
  tests/test_scanner_judge_audit.py
```

```bash
PYTHONPATH=. /tmp/vilvenv/bin/python scripts/judge_soak_report.py --since-hours 24
```

## Relevant implementation

| File | Responsibility |
|---|---|
| `llm/client.py` | HTTP request, timeout, plain JSON/SSE normalization |
| `core/llm_manager.py` | Bounded entry/position action parsing |
| `core/quant_scanner.py` | Deterministic gates, manager invocation, fallback, protected execution |
| `core/judge_circuit_breaker.py` | Failure threshold and cooldown |
| `core/state_db.py` | Durable entry decision audit and metrics |
| `scripts/judge_soak_report.py` | Read-only readiness report |

Never infer provider reliability from a successful HTTP status alone. Verify the normalized manager response, persisted decision, latency, and error rate over the required soak window.

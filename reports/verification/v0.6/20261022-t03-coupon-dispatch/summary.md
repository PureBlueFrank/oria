---
run_id: "20261022-t03-coupon-dispatch"
version: "V0.6"
task_id: "V0.6-T03"
depends_on:
  - "ADR-025 Job lease/fencing"
  - "Job campaign draft persistence"
  - "Job launch approval preparation"
  - "Approved coupon reservation"
verification_level: "F / C"
commit: "3eb053e (feature implementation; see source-snapshot.json)"
executed_at: "2026-10-22"
environment: "macOS; Python 3.11.15; uv 0.12.6; uv.lock SHA-256 recorded in source-snapshot.json"
provider_model: null
config_fingerprint: null
dataset_version: null
eval_fingerprint: null
commands:
  - "uv run pytest -q tests/integration/test_v06_job_coupon_dispatch.py"
  - "uv run pytest -q tests/integration/test_v06_job_coupon_dispatch.py tests/integration/test_v06_job_coupon_reservation.py tests/integration/test_v06_job_launch_approval.py tests/integration/test_v06_job_execution_fencing.py tests/integration/test_v06_job_leases.py"
  - "make test"
  - "make lint"
  - "make build"
  - "make smoke"
artifacts:
  - "design.md"
  - "review.json"
  - "source-snapshot.json"
  - "01-valid-red.log through 11-package-content.md"
evidence_refs:
  - "docs/adr/ADR-025-job-lease-and-fencing.md"
  - "docs/guides/durable-jobs.md"
assertions:
  - "20 dispatch tests pass; the related draft/approval/reservation/lease regression set passes in the complete Community run."
  - "Complete Community result: 1405 passed, 9 deselected, 5 warnings."
  - "Ruff format/check and mypy pass; wheel, sdist and CLI smoke pass."
result: "passed"
blocked_by: []
known_limits:
  - "Only the exact in-memory Mock adapter is accepted; no real network or enterprise adapter was exercised."
  - "SQLite only; no PostgreSQL, independent worker/S2, Graph scheduling/recovery, automatic reconciliation or compensation."
  - "Job status and accepted checkpoint pointer are not advanced by this slice."
  - "The first sandboxed full run is preserved with its loopback bind failure; the complete run passed with local loopback permission."
---

# V0.6-T03 Job coupon dispatch validation

This slice adds an explicit internal Community Mock path from a durable approved coupon reservation to a persisted outcome. It does not enable actual external coupon dispatch.

`JobCouponDispatchService.materialize` accepts only the exact `InMemoryCouponBatchAdapter`, typed coupon arguments, the full approved `LaunchPlan`, and the approval ID. It rechecks the current Job lease, requester/worker identity, actual campaign Policy, approval status/version/expiry, plan and reservation. The stable ledger row is committed as `executing` before the Mock call. An accepted receipt updates coupon, launch saga, ledger and redacted Domain/Audit/outbox records in one Business transaction. Rejected receipts become `failed`; unknown, timeout and adapter exceptions become `unknown`. Any interrupted or failed result commit leaves `executing`; a later authorized call converges that orphan to `unknown` without redispatching. Terminal history is replayed without another adapter call.

The adapter contract has no Job epoch acceptance token. The implementation holds SQLite platform and business write locks across a short, network-free Mock call, with an enforced five-second ceiling. This proves a local single-worker state transition and failure policy only. Real external dispatch needs an adapter acceptance-fencing contract, stable idempotency semantics and reconciliation for unknown outcomes before it can be wired.

The dispatch file contains 20 tests covering approval gating, successful atomic projection/replay, rejected and unknown receipts, exceptions, timeout, task cancellation, outcome commit failure, policy revocation, lease expiry/takeover, corrupt success projection, strict adapter/timeout configuration, and writer blocking through two independent SQLite resource instances. A 201-test related regression run passed before the final success-history integrity test was added; that new test passed in the final full Community run and the dedicated 20-test dispatch run.

## Verification results

- `10-dispatch-final.log`: **20 passed**, one existing Starlette/httpx deprecation warning, 16.49 seconds.
- `04-related-final.log`: **201 passed**, one existing Starlette/httpx deprecation warning, 138.68 seconds. The final full run additionally includes the later success-history integrity test.
- `05-community-full.log`: preserved first attempt, **1 failed / 1403 passed / 9 deselected / 5 warnings**. The sole failure was an existing Feishu server lifecycle test unable to bind a temporary `127.0.0.1` port inside the sandbox (`PermissionError`); it was unrelated to coupon dispatch.
- `06-community-full-loopback.log`: rerun with local loopback permission, **1405 passed / 9 deselected / 5 warnings**, 507.41 seconds. It did not access the internet or a real Feishu service.
- `07-lint.log`: all 389 Python files formatted, Ruff clean, mypy clean for 194 source files.
- `08-build.log`: wheel and source distribution built. The uv cache warning was checked against the actual archive; see `11-package-content.md`.
- `09-smoke.log`: CLI reports version `0.1.0`.

The strict Open Code Review host flow used OCR for deterministic file selection/rule resolution, then manually reviewed all three selected implementation files, the OCR-excluded integration tests, documentation, and the one-line branch policy update. No independent reviewer or OCR LLM result is claimed; `review.json` records the scope and limits. No new dependency, migration, environment variable, user database change, real provider, Live, Enterprise, Performance or PostgreSQL/S2 run was introduced.

The first valid-approval run is preserved as a red scaffold failure. The next attempt exposed an invalid business audit result literal; the corrected run passed. No failure log was replaced. This card records local verification; GitHub Actions are tracked and reported with the PR delivery.

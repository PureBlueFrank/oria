"""T08 zero-configuration demo, correlation, validation, and unwind coverage."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import oria.demo as demo_module
from oria.cli import app
from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.data import initialize_data
from oria.demo import DemoIdentifiers, DemoResult, DemoRunError, execute_demo, run_demo

pytestmark = pytest.mark.integration

_EXPECTED_IDS = {
    "demo-m001",
    "demo-m002",
    "demo-m005",
    "demo-m006",
    "demo-m007",
    "demo-m008",
    "demo-m009",
    "demo-m010",
    "demo-m011",
    "demo-m012",
}
_RULE_CATEGORIES = {
    "basic",
    "recruitment_scope",
    "enrollment_policy",
    "benefit_policy",
    "confirmation_policy",
    "merchant_material",
}


def _ids(label: str) -> DemoIdentifiers:
    return DemoIdentifiers(
        session_id=f"session-{label}",
        thread_id=f"thread-{label}",
        run_id=f"run-{label}",
        correlation_id=f"correlation-{label}",
    )


def _assert_result(result: DemoResult) -> None:
    assert result.ok is True
    assert result.validation.business_side_effect_free is True
    assert result.validation.campaign_proposal_schema is True
    assert result.validation.citations_resolvable is True
    assert result.validation.eligible_merchant_count == 10
    assert set(result.proposal.rules.model_dump()) == _RULE_CATEGORIES
    assert {item.merchant_id for item in result.proposal.recommended_merchants} == _EXPECTED_IDS
    assert result.proposal.unresolved_items == ()
    assert result.proposal.campaign_preview is not None
    assert result.proposal.coupon_batch_preview is not None
    assert result.proposal.field_evidence
    assert [event.tool for event in result.events if event.type == "tool_completed"] == [
        "search_campaign_rules",
        "query_merchants",
    ]
    model_events = [event for event in result.events if event.type == "model_completed"]
    assert model_events
    assert all(event.provider_request_id for event in model_events)
    assert all(event.run_id == result.run_id for event in result.events)
    assert all(event.correlation_id == result.correlation_id for event in result.events)
    assert Path(result.report_path).is_file()
    assert DemoResult.model_validate_json(Path(result.report_path).read_text()) == result


def test_demo_run_error_carries_optional_detail() -> None:
    without_detail = DemoRunError("runtime_start_failed", "correlation-default")
    with_detail = DemoRunError(
        "runtime_start_failed",
        "correlation-detail",
        "runtime dependency is unavailable",
    )

    assert without_detail.detail is None
    assert with_detail.detail == "runtime dependency is unavailable"


@pytest.mark.asyncio
async def test_same_runtime_runs_do_not_share_execution_metadata(tmp_path: Path) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    initialization = await initialize_data(config)
    runtime = await build_runtime(config)
    try:
        first = await execute_demo(runtime, initialization, identifiers=_ids("first"))
        second = await execute_demo(runtime, initialization, identifiers=_ids("second"))
    finally:
        await runtime.aclose()

    _assert_result(first)
    _assert_result(second)
    assert first.ingestion.idempotent is False
    assert second.ingestion.idempotent is True
    assert first.session_id != second.session_id
    assert first.thread_id != second.thread_id
    assert first.run_id != second.run_id
    assert first.correlation_id != second.correlation_id
    assert runtime.ready is False


def test_cli_auto_initializes_repeats_offline_and_emits_correlated_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_network(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("offline demo attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    data_dir = tmp_path / "fresh-data"
    runner = CliRunner()
    first_call = runner.invoke(app, ["demo", "--output", "json", "--data-dir", str(data_dir)])
    second_call = runner.invoke(app, ["demo", "--output", "json", "--data-dir", str(data_dir)])

    assert first_call.exit_code == 0, first_call.stdout
    assert second_call.exit_code == 0, second_call.stdout
    first = DemoResult.model_validate(json.loads(first_call.stdout))
    second = DemoResult.model_validate(json.loads(second_call.stdout))
    _assert_result(first)
    _assert_result(second)
    assert first.initialization.merchants_inserted == 12
    assert second.initialization.merchants_inserted == 0
    assert first.ingestion.idempotent is False
    assert second.ingestion.idempotent is True
    assert first.run_id != second.run_id
    assert first.validation.business_tables == (
        "alembic_version_business",
        "assortment_submission_items",
        "assortment_submissions",
        "audit_events",
        "campaign_approval_bindings",
        "campaign_rule_snapshot_refs",
        "campaigns",
        "confirmation_tasks",
        "consumer_placements",
        "coupon_batches",
        "domain_events",
        "enrollment_coupon_links",
        "enrollment_items",
        "enrollments",
        "launch_saga_states",
        "merchant_notifications",
        "merchants",
        "outbox",
        "product_snapshots",
        "recruitment_publications",
        "selection_decisions",
        "tool_execution_requests",
        "tool_executions",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_detail"),
    [
        ("runtime dependency is unavailable", "runtime dependency is unavailable"),
        ("x" * 301, "x" * 299 + "…"),
    ],
)
async def test_runtime_start_failure_exposes_bounded_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    expected_detail: str,
) -> None:
    original = RuntimeError(message)

    async def fail_build_runtime(config: Any) -> Any:
        del config
        raise original

    monkeypatch.setattr(demo_module, "build_runtime", fail_build_runtime)
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")

    with pytest.raises(DemoRunError) as excinfo:
        await run_demo(config)

    assert excinfo.value.code == "runtime_start_failed"
    assert excinfo.value.detail == expected_detail
    assert excinfo.value.__cause__ is original


@pytest.mark.asyncio
async def test_runtime_start_failure_detail_is_single_line_and_redacts_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_build_runtime(config: Any) -> Any:
        del config
        raise RuntimeError("startup rejected test-key\ntraceback details must not be exposed")

    monkeypatch.setattr(demo_module, "build_runtime", fail_build_runtime)
    config = resolve_runtime_config(
        environ={
            "ORIA_RUNTIME_PROFILE": "standard",
            "ORIA_LLM_PROFILE": "deepseek",
            "ORIA_EMBEDDING_PROFILE": "bge",
            "DEEPSEEK_API_KEY": "test-key",
        },
        data_dir=tmp_path / "data",
    )

    with pytest.raises(DemoRunError) as excinfo:
        await run_demo(config)

    assert excinfo.value.detail == "startup rejected [REDACTED]"


@pytest.mark.asyncio
async def test_demo_execution_failure_closes_the_built_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Any] = []
    actual_build_runtime = build_runtime

    async def capture_runtime(config: Any) -> Any:
        runtime = await actual_build_runtime(config)
        captured.append(runtime)
        return runtime

    async def fail_execution(*args: object, **kwargs: object) -> DemoResult:
        del args, kwargs
        raise RuntimeError("injected execution failure")

    monkeypatch.setattr(demo_module, "build_runtime", capture_runtime)
    monkeypatch.setattr(demo_module, "execute_demo", fail_execution)
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")

    with pytest.raises(DemoRunError) as excinfo:
        await run_demo(config)

    assert excinfo.value.code == "demo_execution_failed"
    assert excinfo.value.detail == "injected execution failure"
    assert len(captured) == 1
    assert captured[0].ready is False


@pytest.mark.asyncio
async def test_demo_detects_mutation_of_an_existing_business_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = resolve_runtime_config(environ={}, data_dir=tmp_path / "data")
    initialization = await initialize_data(config)
    runtime = await build_runtime(config)
    fingerprints = iter(("sha256:before", "sha256:after"))
    monkeypatch.setattr(
        demo_module,
        "_business_database_fingerprint",
        lambda _: next(fingerprints),
    )
    try:
        with pytest.raises(DemoRunError) as excinfo:
            await execute_demo(runtime, initialization, identifiers=_ids("mutated-business"))
    finally:
        await runtime.aclose()

    assert excinfo.value.code == "business_side_effect_detected"

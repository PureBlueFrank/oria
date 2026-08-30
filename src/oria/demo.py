"""Application service for the zero-configuration offline Scenario A demo."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field

from oria.agent.models import (
    CampaignProposal,
    campaign_proposal_schema,
    validate_campaign_proposal,
)
from oria.agent.state import ResearchRunContext, initial_research_state
from oria.config.models import ResolvedRuntimeConfig
from oria.core.context import RuntimeServices
from oria.core.runtime import build_runtime
from oria.core.types import CitationBlock, JsonValue, ValueModel
from oria.data import DataInitializationResult, initialize_data
from oria.orchestrator.checkpoint import checkpoint_config
from oria.permission.local import LOCAL_TENANT_ID, local_cli_executor, local_operator
from oria.providers.structured import validate_structured_value
from oria.rag.demo import demo_rule_document
from oria.rag.models import IngestionResult
from oria.tools.models import QueryMerchantsResult, SearchCampaignRulesResult

_DEMO_REQUEST = "生成华东餐饮招商活动建议"
_EFFECTIVE_AT = "2026-07-15T00:00:00+08:00"
_RULE_CATEGORIES = frozenset(
    {
        "basic",
        "recruitment_scope",
        "enrollment_policy",
        "benefit_policy",
        "confirmation_policy",
        "merchant_material",
    }
)


class DemoRunError(RuntimeError):
    """Safe application-boundary failure with correlation metadata."""

    def __init__(self, code: str, correlation_id: str) -> None:
        super().__init__(code)
        self.code = code
        self.correlation_id = correlation_id


class DemoIdentifiers(ValueModel):
    session_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)


class DemoEvent(ValueModel):
    sequence: int = Field(ge=1)
    type: Literal[
        "data_initialized",
        "runtime_ready",
        "knowledge_ingested",
        "model_completed",
        "model_failed",
        "tool_completed",
        "proposal_validated",
        "report_written",
    ]
    tenant_id: str
    session_id: str
    thread_id: str
    run_id: str
    correlation_id: str
    tool: str | None = None
    model_turn: int | None = Field(default=None, ge=1)
    provider_request_id: str | None = None
    provider_model: str | None = None


class DemoUsage(ValueModel):
    model_turns: int = Field(ge=0)
    tool_calls_total: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    total_cost: float = Field(ge=0)


class DemoValidation(ValueModel):
    campaign_proposal_schema: Literal[True] = True
    semantic_evidence: Literal[True] = True
    citations_resolvable: Literal[True] = True
    recommended_subset_of_eligible: Literal[True] = True
    rule_category_count: Literal[6] = 6
    eligible_merchant_count: int = Field(ge=0)
    business_tables: tuple[str, ...]
    forbidden_business_tables: tuple[str, ...] = ()
    business_side_effect_free: Literal[True] = True


class DemoResult(ValueModel):
    ok: Literal[True] = True
    schema_version: Literal[1] = 1
    executed_at: datetime
    profile: str
    config_fingerprint: str
    tenant_id: str
    session_id: str
    thread_id: str
    run_id: str
    correlation_id: str
    initialization: DataInitializationResult
    ingestion: IngestionResult
    events: tuple[DemoEvent, ...]
    usage: DemoUsage
    proposal: CampaignProposal
    validation: DemoValidation
    report_path: str


def new_demo_identifiers() -> DemoIdentifiers:
    """Create unguessable execution identifiers without retaining them in the runtime."""

    token = uuid.uuid4().hex
    return DemoIdentifiers(
        session_id=f"session_{token}",
        thread_id=f"thread_{uuid.uuid4().hex}",
        run_id=f"run_{uuid.uuid4().hex}",
        correlation_id=f"corr_{uuid.uuid4().hex}",
    )


def validate_demo_proposal(
    value: dict[str, JsonValue],
    *,
    rules: SearchCampaignRulesResult,
    merchants: QueryMerchantsResult,
) -> CampaignProposal:
    """Apply the public JSON schema and trusted-evidence checks at the app boundary."""

    normalized = validate_structured_value(value, campaign_proposal_schema())
    return validate_campaign_proposal(normalized, rules=rules, merchants=merchants)


async def execute_demo(
    runtime: RuntimeServices,
    initialization: DataInitializationResult,
    *,
    identifiers: DemoIdentifiers | None = None,
) -> DemoResult:
    """Execute one isolated run on an already initialized, process-scoped runtime."""

    ids = identifiers or new_demo_identifiers()
    ctx = runtime.new_context(
        actor=local_operator(),
        executor=local_cli_executor(),
        session_id=ids.session_id,
        thread_id=ids.thread_id,
        run_id=ids.run_id,
        correlation_id=ids.correlation_id,
    )
    ingestion = await ctx.knowledge.ingest(demo_rule_document(), ctx)
    business_before = await asyncio.to_thread(
        _business_database_fingerprint, runtime.config.data_paths.business_db
    )
    graph: Any = runtime.agents.get("research_agent")
    state: dict[str, Any] = await graph.ainvoke(
        initial_research_state(
            user_request=_DEMO_REQUEST,
            effective_at=_EFFECTIVE_AT,
        ),
        config=checkpoint_config(ctx),
        context=ResearchRunContext(ctx=ctx),
    )
    raw_proposal = state.get("proposal")
    raw_rules = state.get("rule_result")
    raw_merchants = state.get("merchant_result")
    if not all(isinstance(value, dict) for value in (raw_proposal, raw_rules, raw_merchants)):
        raise DemoRunError("proposal_unavailable", ids.correlation_id)
    proposal = validate_demo_proposal(
        cast(dict[str, JsonValue], raw_proposal),
        rules=SearchCampaignRulesResult.model_validate(raw_rules),
        merchants=QueryMerchantsResult.model_validate(raw_merchants),
    )
    merchants = QueryMerchantsResult.model_validate(raw_merchants)
    eligible_ids = {merchant.merchant_id for merchant in merchants.candidates}
    recommended_ids = {merchant.merchant_id for merchant in proposal.recommended_merchants}
    if not recommended_ids.issubset(eligible_ids):
        raise DemoRunError("eligible_subset_validation_failed", ids.correlation_id)
    if proposal.rules is None or set(proposal.rules.model_dump()) != _RULE_CATEGORIES:
        raise DemoRunError("rule_category_validation_failed", ids.correlation_id)
    citations_valid = all(
        [
            await ctx.knowledge.citation_exists(
                CitationBlock.model_validate(citation.model_dump(mode="json")), ctx
            )
            for citation in proposal.field_evidence.values()
        ]
    )
    if not citations_valid:
        raise DemoRunError("citation_validation_failed", ids.correlation_id)

    business_after = await asyncio.to_thread(
        _business_database_fingerprint, runtime.config.data_paths.business_db
    )
    if business_after != business_before:
        raise DemoRunError("business_side_effect_detected", ids.correlation_id)
    business_tables = await asyncio.to_thread(
        _business_tables, runtime.config.data_paths.business_db
    )

    tool_names = _executed_tools(cast(list[dict[str, Any]], state["messages"]))
    if tool_names != ("search_campaign_rules", "query_merchants"):
        raise DemoRunError("tool_sequence_validation_failed", ids.correlation_id)
    report_path = runtime.config.data_paths.reports_tmp / f"{ids.run_id}.json"
    events = _events(
        ids,
        cast(list[dict[str, Any]], state["events"]),
        tool_names,
    )
    input_tokens = int(state["input_tokens"])
    output_tokens = int(state["output_tokens"])
    result = DemoResult(
        executed_at=datetime.now(UTC),
        profile=f"{runtime.config.edition}+{runtime.config.runtime_profile}",
        config_fingerprint=runtime.config.config_fingerprint,
        tenant_id=LOCAL_TENANT_ID,
        session_id=ids.session_id,
        thread_id=ids.thread_id,
        run_id=ids.run_id,
        correlation_id=ids.correlation_id,
        initialization=initialization,
        ingestion=ingestion,
        events=events,
        usage=DemoUsage(
            model_turns=int(state["model_turns"]),
            tool_calls_total=int(state["tool_calls_total"]),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            total_cost=float(state["total_cost"]),
        ),
        proposal=proposal,
        validation=DemoValidation(
            eligible_merchant_count=len(eligible_ids),
            business_tables=business_tables,
        ),
        report_path=str(report_path),
    )
    _write_report(report_path, result, data_root=runtime.config.data_paths.root)
    return result


async def run_demo(config: ResolvedRuntimeConfig) -> DemoResult:
    """Auto-initialize data, build the sole runtime, execute, validate, and report."""

    identifiers = new_demo_identifiers()
    try:
        initialization = await initialize_data(config)
    except Exception as exc:
        raise DemoRunError("initialization_failed", identifiers.correlation_id) from exc
    try:
        runtime = await build_runtime(config)
    except Exception as exc:
        raise DemoRunError("runtime_start_failed", identifiers.correlation_id) from exc
    try:
        async with runtime:
            return await execute_demo(runtime, initialization, identifiers=identifiers)
    except DemoRunError:
        raise
    except Exception as exc:
        raise DemoRunError("demo_execution_failed", identifiers.correlation_id) from exc


def _executed_tools(messages: list[dict[str, Any]]) -> tuple[str, ...]:
    names: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_call"
                and isinstance(block.get("name"), str)
            ):
                names.append(cast(str, block["name"]))
    return tuple(names)


def _events(
    ids: DemoIdentifiers,
    graph_events: list[dict[str, Any]],
    tools: tuple[str, ...],
) -> tuple[DemoEvent, ...]:
    event_types: list[tuple[str, str | None, int | None, str | None, str | None]] = [
        ("data_initialized", None, None, None, None),
        ("runtime_ready", None, None, None, None),
        ("knowledge_ingested", None, None, None, None),
    ]
    tool_index = 0
    for event in graph_events:
        event_type = event.get("type")
        if event_type == "model_completed" and isinstance(event.get("model_turn"), int):
            request_id = event.get("provider_request_id")
            provider_model = event.get("provider_model")
            event_types.append(
                (
                    "model_completed",
                    None,
                    cast(int, event["model_turn"]),
                    request_id if isinstance(request_id, str) else None,
                    provider_model if isinstance(provider_model, str) else None,
                )
            )
        elif event_type == "provider_failed" and isinstance(event.get("model_turn"), int):
            request_id = event.get("provider_request_id")
            provider_model = event.get("provider_model")
            event_types.append(
                (
                    "model_failed",
                    None,
                    cast(int, event["model_turn"]),
                    request_id if isinstance(request_id, str) else None,
                    provider_model if isinstance(provider_model, str) else None,
                )
            )
        elif event_type == "tools_completed" and tool_index < len(tools):
            event_types.append(("tool_completed", tools[tool_index], None, None, None))
            tool_index += 1
        elif event_type == "proposal_validated":
            event_types.append(("proposal_validated", None, None, None, None))
    event_types.append(("report_written", None, None, None, None))
    return tuple(
        DemoEvent(
            sequence=sequence,
            type=cast(Any, event_type),
            tenant_id=LOCAL_TENANT_ID,
            session_id=ids.session_id,
            thread_id=ids.thread_id,
            run_id=ids.run_id,
            correlation_id=ids.correlation_id,
            tool=tool,
            model_turn=model_turn,
            provider_request_id=provider_request_id,
            provider_model=provider_model,
        )
        for sequence, (
            event_type,
            tool,
            model_turn,
            provider_request_id,
            provider_model,
        ) in enumerate(event_types, start=1)
    )


def _business_tables(path: Path) -> tuple[str, ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _business_database_fingerprint(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        dump = "\n".join(connection.iterdump())
    return "sha256:" + hashlib.sha256(dump.encode("utf-8")).hexdigest()


def _write_report(path: Path, result: DemoResult, *, data_root: Path) -> None:
    root = path.parent.resolve(strict=False)
    expected_root = (data_root.resolve(strict=False) / "reports-tmp").resolve(strict=False)
    if (
        root != expected_root
        or path.name != f"{result.run_id}.json"
        or (root.exists() and root.is_symlink())
    ):
        raise ValueError("demo report directory is unsafe")
    root.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)

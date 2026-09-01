"""Permanent bounded LangGraph research agent used by Scenario A."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, TypeAlias, cast

from jsonschema import ValidationError as JsonSchemaValidationError
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from oria.agent.models import (
    AgentTermination,
    ProposalEvidenceError,
    campaign_proposal_draft_schema,
    finalize_campaign_proposal_draft,
)
from oria.agent.observations import (
    build_observation,
    failed_tool_result,
)
from oria.agent.state import ResearchRunContext, ResearchState
from oria.core.types import (
    ChatOptions,
    JsonValue,
    Message,
    ToolCall,
    ToolCallBlock,
    ToolResult,
    ToolSpec,
)
from oria.providers.errors import ProviderException, StructuredOutputError
from oria.tools.models import (
    QueryMerchantsParams,
    QueryMerchantsResult,
    SearchCampaignRulesResult,
)

_TOOL_FAILURE_CODE = "tool_execution_failed"


def _messages(state: ResearchState) -> list[Message]:
    return [Message.model_validate(item) for item in state["messages"]]


def _dump_message(message: Message) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], message.model_dump(mode="json"))


def _event(
    state: ResearchState, event_type: str, **values: JsonValue
) -> list[dict[str, JsonValue]]:
    return [*state["events"], {"type": event_type, **values}]


def _deadline_exceeded(context: ResearchRunContext) -> bool:
    return context.deadline_at is not None and datetime.now(UTC) >= context.deadline_at


def _observed_usage(state: ResearchState) -> dict[str, JsonValue]:
    return {
        "model_turns": state["model_turns"],
        "tool_calls_total": state["tool_calls_total"],
        "input_tokens": state["input_tokens"],
        "output_tokens": state["output_tokens"],
        "total_tokens": state["input_tokens"] + state["output_tokens"],
        "total_cost": state["total_cost"],
    }


def _termination(
    state: ResearchState,
    context: ResearchRunContext,
    reason: str,
    *,
    status: Literal["failed", "waiting"] = "failed",
) -> dict[str, JsonValue]:
    termination = AgentTermination(
        status=status,
        reason=reason,
        limits=cast(dict[str, JsonValue], context.limits.model_dump(mode="json")),
        observed_usage=_observed_usage(state),
        last_safe_evidence_refs=tuple(state["safe_evidence_refs"]),
    )
    return cast(dict[str, JsonValue], termination.model_dump(mode="json"))


def _model_limit_reason(state: ResearchState, context: ResearchRunContext) -> str | None:
    limits = context.limits
    if _deadline_exceeded(context):
        return "deadline_exceeded"
    if state["model_turns"] >= limits.max_model_turns:
        return "max_model_turns"
    if state["input_tokens"] >= limits.max_input_tokens:
        return "max_input_tokens"
    if state["output_tokens"] >= limits.max_output_tokens:
        return "max_output_tokens"
    if state["input_tokens"] + state["output_tokens"] >= limits.max_total_tokens:
        return "max_total_tokens"
    if state["total_cost"] >= limits.max_cost:
        return "max_cost"
    return None


def _usage_limit_reason(
    *,
    input_tokens: int,
    output_tokens: int,
    total_cost: float,
    context: ResearchRunContext,
) -> str | None:
    limits = context.limits
    if input_tokens > limits.max_input_tokens:
        return "max_input_tokens"
    if output_tokens > limits.max_output_tokens:
        return "max_output_tokens"
    if input_tokens + output_tokens > limits.max_total_tokens:
        return "max_total_tokens"
    if total_cost > limits.max_cost:
        return "max_cost"
    return None


def _provider_failure_state(state: ResearchState, exc: ProviderException) -> ResearchState:
    usage = exc.usage
    input_tokens = state["input_tokens"] + (0 if usage is None else usage.input_tokens)
    output_tokens = state["output_tokens"] + (0 if usage is None else usage.output_tokens)
    total_cost = state["total_cost"] + (0.0 if usage is None or usage.cost is None else usage.cost)
    model_turns = state["model_turns"] + 1
    return cast(
        ResearchState,
        {
            **state,
            "model_turns": model_turns,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_cost": total_cost,
            "events": _event(
                state,
                "provider_failed",
                model_turn=model_turns,
                error_code=exc.code,
                provider_request_id=exc.provider_request_id,
                provider_model=exc.provider_model,
                retryable=exc.retryable,
            ),
        },
    )


def _provider_failure_update(state: ResearchState) -> dict[str, object]:
    return {
        "model_turns": state["model_turns"],
        "input_tokens": state["input_tokens"],
        "output_tokens": state["output_tokens"],
        "total_cost": state["total_cost"],
        "events": state["events"],
    }


def _bounded_tool_specs(specs: tuple[ToolSpec, ...], max_candidates: int) -> list[ToolSpec]:
    bounded: list[ToolSpec] = []
    for spec in specs:
        if spec.name != "query_merchants":
            bounded.append(spec)
            continue
        payload = spec.model_dump(mode="json")
        schema = cast(dict[str, JsonValue], payload["json_schema"])
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("query_merchants schema has no properties")
        raw_limit = properties.get("limit")
        if not isinstance(raw_limit, dict):
            raise ValueError("query_merchants schema has no limit property")
        limit = dict(raw_limit)
        limit["maximum"] = max_candidates
        schema = dict(schema)
        schema["properties"] = {**properties, "limit": limit}
        payload["json_schema"] = schema
        bounded.append(ToolSpec.model_validate(payload))
    return bounded


def _repair_update(
    state: ResearchState,
    *,
    code: str,
    paths: list[str],
) -> dict[str, object]:
    json_paths = cast(JsonValue, paths)
    feedback: dict[str, JsonValue] = {"error_code": code, "field_paths": json_paths}
    message = Message(
        role="system",
        content="finalization repair: "
        + json.dumps(feedback, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    return {
        "messages": [*state["messages"], _dump_message(message)],
        "validation_repairs": state["validation_repairs"] + 1,
        "finalization_only": True,
        "repair_pending": True,
        "structured_output": None,
        "pending_tool_calls": [],
        "events": _event(state, "validation_repair", error_code=code, field_paths=json_paths),
    }


async def research_model_node(
    state: ResearchState,
    runtime: Runtime[ResearchRunContext],
) -> dict[str, object]:
    context = runtime.context
    reason = _model_limit_reason(state, context)
    if reason is not None:
        return {"termination": _termination(state, context, reason)}
    llm = context.ctx.llm
    if llm is None:
        return {"termination": _termination(state, context, "llm_unavailable")}
    remaining_output = context.limits.max_output_tokens - state["output_tokens"]
    try:
        tools = (
            None
            if state["finalization_only"]
            else _bounded_tool_specs(
                context.ctx.tools.specs(("search_campaign_rules", "query_merchants")),
                state["max_candidates"],
            )
        )
        result = await llm.chat(
            _messages(state),
            context.ctx,
            tools=tools,
            options=ChatOptions(
                temperature=0,
                max_output_tokens=remaining_output,
                parallel_tool_calls=True,
                response_schema=campaign_proposal_draft_schema(),
            ),
        )
    except StructuredOutputError as exc:
        failed_state = _provider_failure_state(state, exc)
        failure_update = _provider_failure_update(failed_state)
        usage_reason = (
            "deadline_exceeded"
            if _deadline_exceeded(context)
            else _usage_limit_reason(
                input_tokens=failed_state["input_tokens"],
                output_tokens=failed_state["output_tokens"],
                total_cost=failed_state["total_cost"],
                context=context,
            )
        )
        if usage_reason is not None:
            failure_update["termination"] = _termination(failed_state, context, usage_reason)
            return failure_update
        if failed_state["validation_repairs"] < context.limits.max_validation_repairs:
            update = _repair_update(failed_state, code="structured_output_error", paths=[])
            update.update(
                {
                    "model_turns": failed_state["model_turns"],
                    "input_tokens": failed_state["input_tokens"],
                    "output_tokens": failed_state["output_tokens"],
                    "total_cost": failed_state["total_cost"],
                }
            )
            return update
        failure_update["termination"] = _termination(
            failed_state, context, "structured_output_error"
        )
        return failure_update
    except ProviderException as exc:
        failed_state = _provider_failure_state(state, exc)
        update = _provider_failure_update(failed_state)
        update["termination"] = _termination(failed_state, context, "provider_failure")
        return update
    except Exception:
        failed_state = cast(
            ResearchState,
            {**state, "model_turns": state["model_turns"] + 1},
        )
        return {
            "model_turns": failed_state["model_turns"],
            "termination": _termination(failed_state, context, "provider_failure"),
        }

    input_tokens = state["input_tokens"] + result.usage.input_tokens
    output_tokens = state["output_tokens"] + result.usage.output_tokens
    total_cost = state["total_cost"] + (result.usage.cost or 0.0)
    model_turns = state["model_turns"] + 1
    provider_model: str | None = None
    raw_response = result.internal_raw_response()
    if raw_response is not None and isinstance(raw_response.get("model"), str):
        provider_model = cast(str, raw_response["model"])
    base_update: dict[str, object] = {
        "model_turns": model_turns,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost": total_cost,
        "repair_pending": False,
        "events": _event(
            state,
            "model_completed",
            model_turn=model_turns,
            provider_request_id=result.request_id,
            provider_model=provider_model,
        ),
    }
    usage_reason = (
        "deadline_exceeded"
        if _deadline_exceeded(context)
        else _usage_limit_reason(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_cost=total_cost,
            context=context,
        )
    )
    state_after_usage = dict(state)
    state_after_usage.update(base_update)
    observed_state = cast(ResearchState, state_after_usage)
    if usage_reason is not None:
        base_update["termination"] = _termination(observed_state, context, usage_reason)
        return base_update
    ids = [call.id for call in result.tool_calls]
    if any(not call_id for call_id in ids) or len(ids) != len(set(ids)):
        base_update["termination"] = _termination(
            observed_state, context, "provider_contract_error"
        )
        return base_update
    if result.structured_output is not None and result.tool_calls:
        base_update["termination"] = _termination(
            observed_state, context, "provider_contract_error"
        )
        return base_update
    if state["finalization_only"] and result.tool_calls:
        base_update["termination"] = _termination(
            observed_state, context, "repair_tool_call_forbidden"
        )
        return base_update
    blocks = list(result.content)
    present_call_ids = {block.id for block in blocks if isinstance(block, ToolCallBlock)}
    blocks.extend(
        ToolCallBlock(id=call.id, name=call.name, args=call.args)
        for call in result.tool_calls
        if call.id not in present_call_ids
    )
    assistant = Message(role="assistant", content=tuple(blocks) if blocks else "")
    base_update.update(
        {
            "messages": [*state["messages"], _dump_message(assistant)],
            "pending_tool_calls": [
                cast(dict[str, JsonValue], call.model_dump(mode="json"))
                for call in result.tool_calls
            ],
            "structured_output": result.structured_output,
        }
    )
    return base_update


def route_after_model(state: ResearchState) -> str:
    if state["termination"] is not None:
        return END
    if state["repair_pending"]:
        return "model"
    if state["structured_output"] is not None:
        return "validate"
    if state["pending_tool_calls"]:
        return "tools"
    return "validate"


async def _execute_safely(call: ToolCall, context: ResearchRunContext) -> ToolResult:
    try:
        return await context.ctx.tools.execute(call.name, dict(call.args), context.ctx)
    except Exception:
        return failed_tool_result(
            code=_TOOL_FAILURE_CODE,
            execution_id=f"tool_failed_{uuid.uuid4().hex}",
        )


async def research_tools_node(
    state: ResearchState,
    runtime: Runtime[ResearchRunContext],
) -> dict[str, object]:
    context = runtime.context
    calls = [ToolCall.model_validate(item) for item in state["pending_tool_calls"]]
    if not calls:
        streak = state["no_progress_streak"] + 1
        update: dict[str, object] = {
            "no_progress_streak": streak,
            "events": _event(state, "empty_tool_batch"),
        }
        if streak >= context.limits.no_progress_limit:
            shadow = cast(ResearchState, {**state, **update})
            update["termination"] = _termination(shadow, context, "no_progress")
        return update
    if _deadline_exceeded(context):
        return {"termination": _termination(state, context, "deadline_exceeded")}
    if state["tool_calls_total"] + len(calls) > context.limits.max_tool_calls:
        return {"termination": _termination(state, context, "max_tool_calls")}
    if len({call.id for call in calls}) != len(calls):
        return {"termination": _termination(state, context, "provider_contract_error")}

    for call in calls:
        try:
            if call.name == "query_merchants":
                query = QueryMerchantsParams.model_validate(call.args)
                if query.limit > state["max_candidates"]:
                    raise ValueError("query limit exceeds the requested candidate limit")
            await context.ctx.tools.preflight(call.name, dict(call.args), context.ctx)
        except LookupError:
            code = "unknown_tool"
        except (JsonSchemaValidationError, ValidationError, ValueError):
            code = "invalid_arguments"
        except PermissionError:
            code = "permission_denied"
        except Exception:
            code = "contract_failure"
        else:
            continue
        return {
            "termination": _termination(state, context, "policy_or_contract_violation"),
            "events": _event(state, "tool_batch_rejected", error_code=code),
        }

    if _deadline_exceeded(context):
        return {"termination": _termination(state, context, "deadline_exceeded")}

    results = await asyncio.gather(*(_execute_safely(call, context) for call in calls))
    tool_versions = {
        spec.name: spec.schema_version
        for spec in context.ctx.tools.specs(("search_campaign_rules", "query_merchants"))
    }
    messages = list(state["messages"])
    seen = set(state["seen_evidence_fingerprints"])
    new_fingerprints: list[str] = []
    safe_refs = list(state["safe_evidence_refs"])
    rule_result = state["rule_result"]
    merchant_result = state["merchant_result"]
    side_effect_termination: tuple[str, str] | None = None
    for call, result in zip(calls, results, strict=True):
        try:
            built = build_observation(
                call,
                result,
                tool_schema_version=tool_versions[call.name],
                max_inline_bytes=context.limits.max_inline_tool_bytes,
                ctx=context.ctx,
            )
        except Exception:
            result = failed_tool_result(
                code="object_store_failure",
                execution_id=f"tool_failed_{uuid.uuid4().hex}",
            )
            built = build_observation(
                call,
                result,
                tool_schema_version=tool_versions[call.name],
                max_inline_bytes=context.limits.max_inline_tool_bytes,
                ctx=context.ctx,
            )
        messages.append(
            _dump_message(Message(role="tool", tool_call_id=call.id, content=built.canonical_json))
        )
        if built.fingerprint is not None and built.fingerprint not in seen:
            seen.add(built.fingerprint)
            new_fingerprints.append(built.fingerprint)
        if result.ok:
            safe_refs.append(built.object_ref or result.provenance)
            if (
                built.object_ref is None
                and call.name == "search_campaign_rules"
                and isinstance(result.data, dict)
            ):
                rule_result = result.data
            if (
                built.object_ref is None
                and call.name == "query_merchants"
                and isinstance(result.data, dict)
            ):
                merchant_result = result.data
        else:
            policy = context.ctx.tools.get(call.name).policy
            if policy.side_effect:
                unknown = result.error is not None and result.error.code == "side_effect_unknown"
                side_effect_termination = (
                    "waiting" if unknown else "failed",
                    "side_effect_unknown" if unknown else "side_effect_failed",
                )

    streak = 0 if new_fingerprints else state["no_progress_streak"] + 1
    update = {
        "messages": messages,
        "pending_tool_calls": [],
        "tool_calls_total": state["tool_calls_total"] + len(calls),
        "seen_evidence_fingerprints": sorted(seen),
        "no_progress_streak": streak,
        "rule_result": rule_result,
        "merchant_result": merchant_result,
        "safe_evidence_refs": list(dict.fromkeys(safe_refs)),
        "events": _event(
            state,
            "tools_completed",
            call_count=len(calls),
            new_evidence_count=len(new_fingerprints),
        ),
    }
    shadow = cast(ResearchState, {**state, **update})
    if side_effect_termination is not None:
        raw_status, reason = side_effect_termination
        status = cast(Literal["failed", "waiting"], raw_status)
        update["termination"] = _termination(shadow, context, reason, status=status)
    elif streak >= context.limits.no_progress_limit:
        update["termination"] = _termination(shadow, context, "no_progress")
    return update


def route_after_tools(state: ResearchState) -> str:
    return END if state["termination"] is not None else "model"


async def research_validate_node(
    state: ResearchState,
    runtime: Runtime[ResearchRunContext],
) -> dict[str, object]:
    context = runtime.context
    structured = state["structured_output"]
    if structured is None:
        if state["validation_repairs"] < context.limits.max_validation_repairs:
            return _repair_update(state, code="missing_structured_output", paths=[])
        return {"termination": _termination(state, context, "missing_structured_output")}
    try:
        rules = (
            None
            if state["rule_result"] is None
            else SearchCampaignRulesResult.model_validate(state["rule_result"])
        )
        merchants = (
            None
            if state["merchant_result"] is None
            else QueryMerchantsResult.model_validate(state["merchant_result"])
        )
        proposal = finalize_campaign_proposal_draft(
            structured,
            rules=rules,
            merchants=merchants,
            max_candidates=state["max_candidates"],
        )
    except ProposalEvidenceError:
        return {"termination": _termination(state, context, "evidence_validation_failed")}
    except ValidationError as exc:
        paths = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
        if state["validation_repairs"] < context.limits.max_validation_repairs:
            return _repair_update(state, code="schema_validation_failed", paths=paths)
        return {"termination": _termination(state, context, "schema_validation_failed")}
    return {
        "proposal": cast(dict[str, JsonValue], proposal.model_dump(mode="json")),
        "pending_tool_calls": [],
        "events": _event(state, "proposal_validated", abstained=proposal.abstained),
    }


def route_after_validate(state: ResearchState) -> str:
    if state["proposal"] is not None or state["termination"] is not None:
        return END
    return "model"


ResearchNode: TypeAlias = Callable[[ResearchState, Runtime[ResearchRunContext]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ResearchNodes:
    """Injectable nodes for graph-path tests; production uses the permanent defaults."""

    model: ResearchNode = research_model_node
    tools: ResearchNode = research_tools_node
    validate: ResearchNode = research_validate_node


def build_research_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    nodes: ResearchNodes | None = None,
) -> CompiledStateGraph[ResearchState, ResearchRunContext, ResearchState, ResearchState]:
    """Compile the single permanent research graph without capturing a subject Context."""

    selected = nodes or ResearchNodes()
    builder = StateGraph(ResearchState, context_schema=ResearchRunContext)
    builder.add_node("model", cast(Any, selected.model))
    builder.add_node("tools", cast(Any, selected.tools))
    builder.add_node("validate", cast(Any, selected.validate))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route_after_model)
    builder.add_conditional_edges("tools", route_after_tools)
    builder.add_conditional_edges("validate", route_after_validate)
    return builder.compile(checkpointer=checkpointer)

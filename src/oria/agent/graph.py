"""Permanent bounded LangGraph research agent shared by all research scenarios."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, TypeAlias, cast

from jsonschema import ValidationError as JsonSchemaValidationError
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from oria.agent.models import (
    AgentTermination,
    AttributionDecisionError,
    CampaignProposal,
    ProposalEvidenceError,
    campaign_proposal_draft_schema,
    finalize_campaign_proposal_draft,
)
from oria.agent.observations import (
    build_observation,
    canonical_json,
    failed_tool_result,
)
from oria.agent.spec import ResearchSpec, ResearchStateView
from oria.agent.state import ResearchRunContext, ResearchState
from oria.core.context import Context
from oria.core.types import (
    ChatOptions,
    JsonValue,
    Message,
    ToolCall,
    ToolCallBlock,
    ToolResult,
    ToolSpec,
)
from oria.memory import FactLedger, FactLedgerEntry, InMemoryMemory, compress_history
from oria.providers.errors import ProviderException, StructuredOutputError
from oria.tools.models import (
    QueryMerchantsParams,
    QueryMerchantsResult,
    SearchCampaignRulesResult,
)

_TOOL_FAILURE_CODE = "tool_execution_failed"
_CAMPAIGN_TOOL_NAMES = ("search_campaign_rules", "query_merchants")
_FINALIZATION_INSTRUCTION = (
    "finalization required: business tools are no longer available. Submit only the "
    "complete structured response using existing tool evidence. If the evidence is "
    "insufficient, abstain and list the data needed. Do not invent evidence or request "
    "another business-tool call."
)


def _messages(state: ResearchState) -> list[Message]:
    return [Message.model_validate(item) for item in state["messages"]]


def _dump_message(message: Message) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], message.model_dump(mode="json"))


def _load_fact_ledger(state: ResearchState) -> FactLedger:
    return FactLedger(
        entries=tuple(FactLedgerEntry.model_validate(item) for item in state.get("fact_ledger", []))
    )


def _dump_fact_ledger(ledger: FactLedger) -> list[dict[str, JsonValue]]:
    return [cast(dict[str, JsonValue], entry.model_dump(mode="json")) for entry in ledger.entries]


async def _govern_context(
    state: ResearchState,
    messages: list[Message],
    ctx: Context,
) -> tuple[list[Message], FactLedger, dict[str, object]]:
    memory = ctx.memory
    ledger = _load_fact_ledger(state)
    if not isinstance(memory, InMemoryMemory):
        return messages, ledger, {}
    await memory.replace(messages, ctx, ledger=ledger)
    await memory.compress(ctx)
    governed = await memory.load(ctx)
    governed_ledger = await memory.fact_ledger(ctx)
    dumped_messages = [_dump_message(message) for message in governed]
    dumped_ledger = _dump_fact_ledger(governed_ledger)
    update: dict[str, object] = {}
    if dumped_messages != state["messages"]:
        update["messages"] = dumped_messages
    if dumped_ledger != state.get("fact_ledger", []):
        update["fact_ledger"] = dumped_ledger
    return governed, governed_ledger, update


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
                safe_message=exc.safe_message,
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


def _campaign_tool_specs(specs: tuple[ToolSpec, ...], state: ResearchStateView) -> list[ToolSpec]:
    return _bounded_tool_specs(specs, cast(int, state["max_candidates"]))


def _validate_campaign_tool_call(call: ToolCall, state: ResearchStateView) -> None:
    if call.name != "query_merchants":
        return
    query = QueryMerchantsParams.model_validate(call.args)
    if query.limit > state["max_candidates"]:
        raise ValueError("query limit exceeds the requested candidate limit")


def _finalize_campaign(value: dict[str, JsonValue], state: ResearchStateView) -> CampaignProposal:
    rules_value = state.get("rule_result")
    merchant_value = state.get("merchant_result")
    rules = None if rules_value is None else SearchCampaignRulesResult.model_validate(rules_value)
    merchants = (
        None if merchant_value is None else QueryMerchantsResult.model_validate(merchant_value)
    )
    return finalize_campaign_proposal_draft(
        value,
        rules=rules,
        merchants=merchants,
        max_candidates=cast(int, state["max_candidates"]),
    )


def campaign_research_spec() -> ResearchSpec:
    """Return the fixed V0.1 Scenario A specialization of the research loop."""

    return ResearchSpec(
        prompt_name="merchant_selection",
        prompt_version=1,
        tool_names=_CAMPAIGN_TOOL_NAMES,
        response_schema=campaign_proposal_draft_schema(),
        output_field="proposal",
        validated_event_type="proposal_validated",
        finalize=_finalize_campaign,
        result_state_fields=(
            ("search_campaign_rules", "rule_result"),
            ("query_merchants", "merchant_result"),
        ),
        adapt_tool_specs=_campaign_tool_specs,
        validate_tool_call=_validate_campaign_tool_call,
    )


def _decision_repair_guidance(code: str, paths: list[str]) -> str:
    """Translate raw decision/schema validation errors into actionable repair steps."""

    if code == "structured_output_error":
        return (
            "Submit exactly one complete JSON object using the configured submission mechanism "
            "(the reserved function in synthetic-tool mode, a JSON object otherwise), "
            "with no surrounding prose or Markdown. "
            "The JSON must include every required field (causal_assessment, hypotheses, "
            "decision_assessment, evidence, outcome, conclusion, confidence, "
            "confidence_explanation, "
            "abstained, requested_data) and must not add unknown fields. Fill "
            "causal_assessment even when no anomaly is found: list no anomalous stages "
            "and set shared_mechanism_observed=false. Copy tool_call_id and JSON Pointer "
            "verbatim. "
        )
    joined = "\n".join(paths)
    steps: list[str] = []
    if "support and refutation must be distinct" in joined:
        steps.append(
            "A ruled_out candidate lists the same evidence index in both evidence_indices "
            "and refutation_indices. These two lists must never overlap: an observation that "
            "supports a candidate goes only in evidence_indices (status=supported, empty "
            "refutation_indices); an observation that actually refutes it goes only in "
            "refutation_indices (status=ruled_out). Remove the shared index from the list it "
            "does not belong to, or change the candidate status to match what the observation "
            "actually shows."
        )
    if "retain every supported decision candidate" in joined:
        steps.append(
            "The set of candidates you marked status=supported does not exactly match the final "
            "hypotheses. Make them identical: every supported candidate must appear as a "
            "hypothesis, and every hypothesis must be a supported candidate. If that yields more "
            "than one supported candidate, set outcome=conflicting (conclusion=null) and keep them "
            "all; if it yields exactly one, set outcome=attributed with that single hypothesis. Do "
            "not silently drop a supported candidate or invent a hypothesis without support."
        )
    if "decision rules require" in joined:
        steps.append(
            "The program computes a required outcome from your decision_assessment (scope -> "
            "insufficient; multiple supported candidates -> conflicting; missing evidence or zero "
            "candidates -> insufficient; exactly one supported candidate -> attributed). Set "
            "outcome to that required value and align conclusion, hypotheses, abstained and "
            "requested_data to match, without erasing supported candidates or inventing refutation."
        )
    if "ruled_out requires observed refutation" in joined:
        steps.append(
            "A candidate may be ruled_out only when it also has a distinct refutation observation "
            "in refutation_indices; a supported candidate must leave refutation_indices empty."
        )
    if "candidate support must match evidence.supports" in joined:
        steps.append(
            "Every evidence_indices entry of a supported candidate must point to an evidence item "
            "whose supports list includes that candidate's hypothesis_id."
        )
    if "non-null observation" in joined:
        steps.append(
            "Decision evidence indices must reference evidence items whose value is a non-null "
            "observation; a null comparison cannot serve as support or refutation."
        )
    if "shared mechanism" in joined or "multiple anomalous stages" in joined:
        steps.append(
            "Multiple independently anomalous stages can only be attributed to one cause when a "
            "shared mechanism is directly observed; otherwise keep each independent explanation as "
            "its own supported candidate and output conflicting."
        )
    if "hypothesis IDs must be unique" in joined:
        steps.append("hypothesis_id values must be unique across all hypotheses.")
    if "references an unknown hypothesis" in joined:
        steps.append(
            "Every evidence item's supports list may only reference hypothesis_id values that "
            "actually appear in hypotheses."
        )
    if not steps:
        steps.append(
            "Correct the reported fields to satisfy the validation feedback above, reusing "
            "existing evidence and preserving every supported candidate."
        )
    return " ".join(steps) + " "


def _repair_update(
    state: ResearchState,
    *,
    code: str,
    paths: list[str],
) -> dict[str, object]:
    json_paths = cast(JsonValue, paths)
    feedback: dict[str, JsonValue] = {"error_code": code, "field_paths": json_paths}
    guidance = _decision_repair_guidance(code, paths)
    message = Message(
        role="system",
        content=(
            "finalization repair: return only a complete structured response; business "
            "tools are unavailable. Reuse exact existing tool_call_id, tool_name, JSON "
            "Pointer, and observed value when citing evidence. If the evidence is "
            "insufficient, abstain and list the data needed. Do not invent evidence. "
            + guidance
            + "validation feedback: "
            + json.dumps(feedback, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        ),
    )
    messages = list(state["messages"])
    if state["structured_output"] is not None:
        # Structured submissions are not necessarily present in provider text blocks.
        # Keep the rejected draft as assistant data, never elevate it to system instructions.
        messages.append(
            _dump_message(
                Message(role="assistant", content=canonical_json(state["structured_output"]))
            )
        )
    return {
        "messages": [*messages, _dump_message(message)],
        "validation_repairs": state["validation_repairs"] + 1,
        "validation_drafts": [
            *state.get("validation_drafts", []),
            *([state["structured_output"]] if state["structured_output"] is not None else []),
        ],
        "finalization_only": True,
        "repair_pending": True,
        "structured_output": None,
        "pending_tool_calls": [],
        "events": _event(state, "validation_repair", error_code=code, field_paths=json_paths),
    }


def _tool_batch_finalization_update(
    state: ResearchState,
    calls: list[ToolCall],
    *,
    code: str,
    retry_arguments: bool = False,
    argument_errors: list[dict[str, JsonValue]] | None = None,
) -> dict[str, object]:
    messages = list(state["messages"])
    for call in calls:
        result = failed_tool_result(
            code=code,
            execution_id=f"tool_rejected_{uuid.uuid4().hex}",
        )
        messages.append(
            _dump_message(
                Message(
                    role="tool",
                    tool_call_id=call.id,
                    content=canonical_json(cast(JsonValue, result.model_dump(mode="json"))),
                )
            )
        )
    instruction = (
        "The entire business-tool batch was rejected before execution because arguments "
        "did not match the exposed schemas. Correct parameter names, types and required "
        "fields and retry once. No business observation was obtained from this batch."
        if retry_arguments
        else _FINALIZATION_INSTRUCTION
    )
    messages.append(_dump_message(Message(role="system", content=instruction)))
    if argument_errors:
        messages.append(
            _dump_message(
                Message(role="system", content=canonical_json(cast(JsonValue, argument_errors)))
            )
        )
    return {
        "messages": messages,
        "pending_tool_calls": [],
        "structured_output": None,
        "finalization_only": not retry_arguments,
        "repair_pending": False,
        "events": _event(
            state,
            "tool_argument_repair" if retry_arguments else "tool_batch_finalization",
            error_code=code,
        ),
    }


async def _provider_chat_with_retry(
    llm: Any,
    messages: list[Message],
    ctx: Any,
    *,
    tools: object,
    options: ChatOptions,
    context: ResearchRunContext,
    retries: list[dict[str, JsonValue]],
) -> Any:
    """Invoke the provider, retrying transient failures within a bounded budget.

    Only retryable errors (rate limits, transient unavailability, timeouts) are retried.
    Structured-output errors are never retryable and propagate to the existing repair path.
    Each retry is appended to ``retries`` as an audit event and does not consume a model turn.
    """

    remaining = context.limits.max_provider_retries
    while True:
        try:
            return await llm.chat(messages, ctx, tools=tools, options=options)
        except ProviderException as exc:
            if not exc.retryable or remaining <= 0 or _deadline_exceeded(context):
                raise
            remaining -= 1
            retries.append(
                {
                    "type": "provider_retry",
                    "error_code": exc.code,
                    "retry_after": exc.retry_after,
                }
            )
            delay = 0.0 if exc.retry_after is None else exc.retry_after
            if delay > 0:
                await asyncio.sleep(min(delay, 5.0))
            continue


async def research_model_node(
    state: ResearchState,
    runtime: Runtime[ResearchRunContext],
    *,
    spec: ResearchSpec | None = None,
) -> dict[str, object]:
    selected = spec or campaign_research_spec()
    context = runtime.context
    reason = _model_limit_reason(state, context)
    if reason is not None:
        return {"termination": _termination(state, context, reason)}
    llm = context.ctx.llm
    if llm is None:
        return {"termination": _termination(state, context, "llm_unavailable")}
    remaining_output = context.limits.max_output_tokens - state["output_tokens"]
    remaining_repair_turns = context.limits.max_validation_repairs - state["validation_repairs"]
    force_finalization = (
        state["finalization_only"]
        or state["model_turns"] + 1 + remaining_repair_turns >= context.limits.max_model_turns
        or state["tool_calls_total"] >= context.limits.max_tool_calls
    )
    model_messages = _messages(state)
    model_messages, fact_ledger, context_update = await _govern_context(
        state,
        model_messages,
        context.ctx,
    )
    if context_update:
        state = cast(ResearchState, {**state, **context_update})
    if force_finalization and not state["finalization_only"]:
        model_messages.append(Message(role="system", content=_FINALIZATION_INSTRUCTION))
    if force_finalization:
        submission_instruction = (
            "Submit the complete JSON object as arguments of __oria_submit_response__. "
            "This final submission function is explicitly allowed; the earlier tool "
            "allowlist and tool-call prohibition apply only to business research tools. "
            "Do not return plain text or empty arguments. JSON Schema: "
            if context.ctx.config.llm.structured_output_mode == "synthetic_tool"
            else "Return exactly one JSON object matching this schema, without prose, "
            "Markdown fences, or tool-call markup. JSON Schema: "
        )
        model_messages.append(
            Message(
                role="system",
                content=submission_instruction
                + canonical_json(selected.response_schema.json_schema),
            )
        )
    memory = context.ctx.memory
    if isinstance(memory, InMemoryMemory):
        model_messages, _, _ = compress_history(
            model_messages,
            memory.budget,
            fact_ledger,
        )
    request_timeout_seconds = (
        None
        if context.deadline_at is None
        else max((context.deadline_at - datetime.now(UTC)).total_seconds(), 0.001)
    )
    tool_choice = "auto"
    if force_finalization:
        tool_choice = (
            "required"
            if context.ctx.config.llm.structured_output_mode == "synthetic_tool"
            else "none"
        )
    retry_events: list[dict[str, JsonValue]] = []
    try:
        tools = (
            None
            if force_finalization
            else selected.adapt_tool_specs(context.ctx.tools.specs(selected.tool_names), state)
        )
        result = await _provider_chat_with_retry(
            llm,
            model_messages,
            context.ctx,
            tools=tools,
            options=ChatOptions(
                temperature=0,
                max_output_tokens=remaining_output,
                tool_choice=tool_choice,
                parallel_tool_calls=not force_finalization,
                response_schema=selected.response_schema,
                timeout_seconds=request_timeout_seconds,
            ),
            context=context,
            retries=retry_events,
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
            return {**context_update, **failure_update}
        if failed_state["validation_repairs"] < context.limits.max_validation_repairs:
            paths: list[str] = []
            cause = exc.__cause__
            if isinstance(cause, StructuredOutputError):
                cause = cause.__cause__
            if isinstance(cause, JsonSchemaValidationError):
                prefix = [str(part) for part in cause.absolute_path]
                if cause.validator == "required" and isinstance(cause.instance, dict):
                    paths = [
                        ".".join([*prefix, field])
                        for field in cast(list[str], cause.validator_value)
                        if field not in cause.instance
                    ]
                else:
                    paths = [".".join(prefix)]
            update = _repair_update(failed_state, code="structured_output_error", paths=paths)
            update.update(
                {
                    "model_turns": failed_state["model_turns"],
                    "input_tokens": failed_state["input_tokens"],
                    "output_tokens": failed_state["output_tokens"],
                    "total_cost": failed_state["total_cost"],
                }
            )
            return {**context_update, **update}
        failure_update["termination"] = _termination(
            failed_state, context, "structured_output_error"
        )
        return {**context_update, **failure_update}
    except ProviderException as exc:
        failed_state = _provider_failure_state(state, exc)
        update = _provider_failure_update(failed_state)
        if retry_events:
            failed_events = cast(list[dict[str, JsonValue]], update["events"])
            update["events"] = [*state["events"], *retry_events, failed_events[-1]]
        update["termination"] = _termination(failed_state, context, "provider_failure")
        return {**context_update, **update}
    except Exception:
        failed_state = cast(
            ResearchState,
            {**state, "model_turns": state["model_turns"] + 1},
        )
        return {
            **context_update,
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
    completion_events = _event(
        state,
        "model_completed",
        model_turn=model_turns,
        provider_request_id=result.request_id,
        provider_model=provider_model,
        reasoning_tokens=result.usage.reasoning_tokens,
        cache_read_tokens=result.usage.cache_read_tokens,
    )
    if retry_events:
        completion_events = [*completion_events[:-1], *retry_events, completion_events[-1]]
    base_update: dict[str, object] = {
        **context_update,
        "model_turns": model_turns,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_cost": total_cost,
        "finalization_only": force_finalization,
        "repair_pending": False,
        "events": completion_events,
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
    if force_finalization and result.tool_calls:
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


def _no_progress_update(
    state: ResearchState, context: ResearchRunContext, spec: ResearchSpec
) -> dict[str, object]:
    if not spec.finalize_on_no_progress or state["finalization_only"]:
        return {"termination": _termination(state, context, "no_progress")}
    return {
        "finalization_only": True,
        "pending_tool_calls": [],
        "messages": [
            *state["messages"],
            _dump_message(
                Message(
                    role="system",
                    content="No new evidence after repeated research. "
                    + _FINALIZATION_INSTRUCTION
                    + " Preserve all supported candidates; no progress is not evidence of absence.",
                )
            ),
        ],
        "events": [*state["events"], {"type": "no_progress_finalization"}],
    }


async def research_tools_node(
    state: ResearchState,
    runtime: Runtime[ResearchRunContext],
    *,
    spec: ResearchSpec | None = None,
) -> dict[str, object]:
    selected = spec or campaign_research_spec()
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
            update.update(_no_progress_update(shadow, context, selected))
        return update
    if _deadline_exceeded(context):
        return {"termination": _termination(state, context, "deadline_exceeded")}
    if len({call.id for call in calls}) != len(calls):
        return {"termination": _termination(state, context, "provider_contract_error")}

    invalid_arguments = False
    argument_errors: list[dict[str, JsonValue]] = []
    for call in calls:
        try:
            selected.validate_tool_call(call, state)
        except (JsonSchemaValidationError, ValidationError, ValueError):
            return {
                "termination": _termination(state, context, "policy_or_contract_violation"),
                "events": _event(state, "tool_batch_rejected", error_code="invalid_arguments"),
            }
        try:
            await context.ctx.tools.preflight(call.name, dict(call.args), context.ctx)
        except LookupError:
            code = "unknown_tool"
        except (JsonSchemaValidationError, ValidationError, ValueError) as exc:
            code = "invalid_arguments"
            detail: dict[str, JsonValue] = {"tool_call_id": call.id, "tool_name": call.name}
            if isinstance(exc, JsonSchemaValidationError):
                detail["field_path"] = ".".join(str(part) for part in exc.absolute_path)
                detail["constraint"] = str(exc.validator)
                detail["expected"] = cast(JsonValue, exc.validator_value)
            elif isinstance(exc, ValidationError):
                detail["field_paths"] = [
                    ".".join(str(part) for part in error["loc"]) for error in exc.errors()
                ]
            argument_errors.append(detail)
        except PermissionError:
            code = "permission_denied"
        except Exception:
            code = "contract_failure"
        else:
            continue
        if code == "invalid_arguments":
            invalid_arguments = True
            continue
        return {
            "termination": _termination(state, context, "policy_or_contract_violation"),
            "events": _event(state, "tool_batch_rejected", error_code=code),
        }

    if invalid_arguments:
        if (
            selected.prompt_name == "attribution_reasoning"
            and not any(event["type"] == "tool_argument_repair" for event in state["events"])
            and state["model_turns"] + 2 < context.limits.max_model_turns
        ):
            return _tool_batch_finalization_update(
                state,
                calls,
                code="invalid_arguments",
                retry_arguments=True,
                argument_errors=argument_errors,
            )
        if state["safe_evidence_refs"] and state["model_turns"] < context.limits.max_model_turns:
            return _tool_batch_finalization_update(state, calls, code="invalid_arguments")
        return {
            "termination": _termination(state, context, "policy_or_contract_violation"),
            "events": _event(state, "tool_batch_rejected", error_code="invalid_arguments"),
        }
    if state["tool_calls_total"] + len(calls) > context.limits.max_tool_calls:
        if state["safe_evidence_refs"] and state["model_turns"] < context.limits.max_model_turns:
            return _tool_batch_finalization_update(state, calls, code="max_tool_calls")
        return {"termination": _termination(state, context, "max_tool_calls")}

    if _deadline_exceeded(context):
        return {"termination": _termination(state, context, "deadline_exceeded")}

    results = await asyncio.gather(*(_execute_safely(call, context) for call in calls))
    tool_versions = {
        spec.name: spec.schema_version for spec in context.ctx.tools.specs(selected.tool_names)
    }
    messages = list(state["messages"])
    seen = set(state["seen_evidence_fingerprints"])
    new_fingerprints: list[str] = []
    safe_refs = list(state["safe_evidence_refs"])
    tool_results = dict(state.get("tool_results", {}))
    result_updates: dict[str, object] = {}
    result_fields = dict(selected.result_state_fields)
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
            tool_results[call.id] = {
                "tool_name": call.name,
                "arguments": cast(JsonValue, call.args),
                "result": cast(JsonValue, result.model_dump(mode="json")),
            }
            state_field = result_fields.get(call.name)
            if (
                state_field is not None
                and built.object_ref is None
                and isinstance(result.data, dict)
            ):
                result_updates[state_field] = result.data
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
        "tool_results": tool_results,
        "safe_evidence_refs": list(dict.fromkeys(safe_refs)),
        "events": _event(
            state,
            "tools_completed",
            call_count=len(calls),
            new_evidence_count=len(new_fingerprints),
        ),
    }
    update.update(result_updates)
    shadow = cast(ResearchState, {**state, **update})
    if side_effect_termination is not None:
        raw_status, reason = side_effect_termination
        status = cast(Literal["failed", "waiting"], raw_status)
        update["termination"] = _termination(shadow, context, reason, status=status)
    elif streak >= context.limits.no_progress_limit:
        update.update(_no_progress_update(shadow, context, selected))
    return update


def route_after_tools(state: ResearchState) -> str:
    return END if state["termination"] is not None else "model"


async def research_validate_node(
    state: ResearchState,
    runtime: Runtime[ResearchRunContext],
    *,
    spec: ResearchSpec | None = None,
) -> dict[str, object]:
    selected = spec or campaign_research_spec()
    context = runtime.context
    structured = state["structured_output"]
    if structured is None:
        if state["validation_repairs"] < context.limits.max_validation_repairs:
            return _repair_update(state, code="missing_structured_output", paths=[])
        return {"termination": _termination(state, context, "missing_structured_output")}
    try:
        final_value = selected.finalize(structured, state)
    except ProposalEvidenceError:
        return {"termination": _termination(state, context, "evidence_validation_failed")}
    except AttributionDecisionError as exc:
        if state["validation_repairs"] < context.limits.max_validation_repairs:
            return _repair_update(state, code="decision_validation_failed", paths=[str(exc)])
        return {"termination": _termination(state, context, "decision_validation_failed")}
    except ValidationError as exc:
        paths = sorted(
            {
                # Field errors carry a loc path; root model validators carry only the
                # rule message, which is the feedback the repair actually needs.
                ".".join(str(part) for part in error["loc"]) + ": " + error["msg"]
                for error in exc.errors()
            }
        )
        if state["validation_repairs"] < context.limits.max_validation_repairs:
            return _repair_update(state, code="schema_validation_failed", paths=paths)
        return {"termination": _termination(state, context, "schema_validation_failed")}
    return {
        selected.output_field: cast(dict[str, JsonValue], final_value.model_dump(mode="json")),
        "final_result": cast(dict[str, JsonValue], final_value.model_dump(mode="json")),
        "pending_tool_calls": [],
        "events": _event(
            state,
            selected.validated_event_type,
            abstained=cast(bool, getattr(final_value, "abstained", False)),
        ),
    }


def route_after_validate(state: ResearchState) -> str:
    if (
        state.get("final_result") is not None
        or state.get("proposal") is not None
        or state["termination"] is not None
    ):
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
    spec: ResearchSpec | None = None,
) -> CompiledStateGraph[ResearchState, ResearchRunContext, ResearchState, ResearchState]:
    """Compile the single permanent research graph without capturing a subject Context."""

    selected_spec = spec or campaign_research_spec()
    selected = nodes or ResearchNodes(
        model=partial(research_model_node, spec=selected_spec),
        tools=partial(research_tools_node, spec=selected_spec),
        validate=partial(research_validate_node, spec=selected_spec),
    )
    builder = StateGraph(ResearchState, context_schema=ResearchRunContext)
    builder.add_node("model", cast(Any, selected.model))
    builder.add_node("tools", cast(Any, selected.tools))
    builder.add_node("validate", cast(Any, selected.validate))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route_after_model)
    builder.add_conditional_edges("tools", route_after_tools)
    builder.add_conditional_edges("validate", route_after_validate)
    return builder.compile(checkpointer=checkpointer)

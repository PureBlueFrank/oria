"""Trusted local executor for Scenario A CLI start, resume, approval, and Mock events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from langgraph.types import Command, Interrupt, StateSnapshot
from pydantic import Field

from oria.config.models import ResolvedRuntimeConfig
from oria.core.context import Context, RuntimeServices
from oria.core.runtime import build_runtime
from oria.core.types import JsonValue, Principal, ValueModel
from oria.data import initialize_data
from oria.domain.launch import CampaignDraftSpec
from oria.orchestrator.checkpoint import checkpoint_config
from oria.orchestrator.scenario_a import (
    DefaultScenarioAWorkflowService,
    ScenarioAWorkflowRequest,
    initial_scenario_a_state,
    scenario_a_request,
)
from oria.permission.local import LOCAL_TENANT_ID, local_cli_executor
from oria.rag.demo import demo_rule_document

FIXTURE_NOW = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
MOCK_MERCHANT_ADAPTER = "mock-merchant"
MOCK_MERCHANT_SUBJECT = "mock-merchant-adapter"
MOCK_SELECTION_ADAPTER = "mock-selection"
MOCK_SELECTION_SUBJECT = "mock-selection-adapter"


class LocalWorkflowResult(ValueModel):
    ok: bool = True
    thread_id: str
    status: Literal["waiting", "completed"]
    interrupts: tuple[dict[str, JsonValue], ...] = ()
    detail: dict[str, JsonValue] = Field(default_factory=dict)


def _principal(
    subject_id: str, *roles: str, kind: Literal["human", "service"] = "human"
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=LOCAL_TENANT_ID,
        kind=kind,
        roles=roles,
        authn_method="trusted-local-workflow",
    )


def campaign_admin() -> Principal:
    return _principal("local-campaign-admin", "operator", "campaign_admin")


def launch_approver() -> Principal:
    return _principal("local-launch-approver", "launch_approver")


def consumer_publish_approver() -> Principal:
    return _principal("local-consumer-publish-approver", "consumer_publish_approver")


def integration_executor() -> Principal:
    return _principal("local-integration-executor", "integration_adapter", kind="service")


def _integration_actor() -> Principal:
    return _principal("local-integration-actor", "integration_adapter")


def _trusted_actors(extra: tuple[Principal, ...] = ()) -> tuple[Principal, ...]:
    return (
        campaign_admin(),
        launch_approver(),
        consumer_publish_approver(),
        _integration_actor(),
        *extra,
    )


async def _runtime(
    config: ResolvedRuntimeConfig,
    *,
    confirmation_assignments: dict[str, str] | None = None,
    extra_actors: tuple[Principal, ...] = (),
) -> RuntimeServices:
    return await build_runtime(
        config,
        clock=lambda: FIXTURE_NOW,
        trusted_actors=_trusted_actors(extra_actors),
        trusted_executors=(local_cli_executor(), integration_executor()),
        confirmation_assignments=confirmation_assignments,
    )


def _ctx(
    runtime: RuntimeServices,
    *,
    actor: Principal,
    executor: Principal,
    thread_id: str,
    run_suffix: str,
) -> Context:
    return runtime.new_context(
        actor=actor,
        executor=executor,
        session_id=f"scenario-a:{thread_id}",
        thread_id=thread_id,
        run_id=f"scenario-a:{thread_id}:{run_suffix}",
        correlation_id=f"scenario-a:{thread_id}",
    )


def _scenario(runtime: RuntimeServices) -> DefaultScenarioAWorkflowService:
    service = runtime.domain.scenario_a
    if not isinstance(service, DefaultScenarioAWorkflowService):
        raise RuntimeError("trusted Scenario A service is unavailable")
    return service


def _graph(runtime: RuntimeServices) -> Any:
    return runtime.agents.get("scenario_a")


def _interrupts(value: object) -> tuple[Interrupt, ...]:
    if isinstance(value, StateSnapshot):
        return value.interrupts
    if isinstance(value, dict):
        raw = value.get("__interrupt__", ())
        if isinstance(raw, (list, tuple)) and all(isinstance(item, Interrupt) for item in raw):
            return tuple(raw)
    return ()


def _public_interrupt(interruption: Interrupt) -> dict[str, JsonValue]:
    value = interruption.value
    if not isinstance(value, dict):
        raise RuntimeError("workflow produced an invalid interrupt payload")
    visible = {
        key: item
        for key, item in value.items()
        if key
        in {
            "approval_id",
            "confirmation_task_id",
            "kind",
            "target_role",
            "wait_id",
        }
    }
    return cast(
        dict[str, JsonValue],
        {"interrupt_id": interruption.id, **visible},
    )


def _result(thread_id: str, value: object, **detail: JsonValue) -> LocalWorkflowResult:
    interruptions = _interrupts(value)
    return LocalWorkflowResult(
        thread_id=thread_id,
        status="waiting" if interruptions else "completed",
        interrupts=tuple(_public_interrupt(item) for item in interruptions),
        detail=detail,
    )


def default_request(
    *,
    campaign_id: str,
    user_request: str,
) -> ScenarioAWorkflowRequest:
    return ScenarioAWorkflowRequest(
        user_request=user_request,
        effective_at=FIXTURE_NOW,
        draft=CampaignDraftSpec(
            campaign_id=campaign_id,
            coupon_batch_id=f"{campaign_id}-coupon",
            recruitment_publication_id=f"{campaign_id}-recruitment",
            material_version="synthetic-material-v1",
            compensation_policy_version="synthetic-no-auto-compensation-v1",
        ),
        enrollment_mode="hybrid",
        product_limit=1,
        circle_run_id=f"{campaign_id}-circle-v1",
        coupon_link_idempotency_key=f"{campaign_id}:coupon-link:v1",
        assortment_policy_ref="synthetic-assortment-policy",
        assortment_policy_version="1.0.0",
        assortment_idempotency_key=f"{campaign_id}:assortment:v1",
        selection_expected_version=1,
        placement_spec={"channel": "synthetic-home-feed", "region": "east-china"},
        placement_idempotency_key=f"{campaign_id}:placement:v1",
        notification_template_id="selection-result-v1",
        notification_channel="mock-im",
        notification_idempotency_prefix=f"{campaign_id}:notification:v1",
        approval_expires_at=FIXTURE_NOW + timedelta(days=2),
        external_wait_expires_at=FIXTURE_NOW + timedelta(days=40),
    )


async def start_local_workflow(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    campaign_id: str,
    user_request: str,
) -> LocalWorkflowResult:
    await initialize_data(config)
    runtime = await _runtime(config)
    try:
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=local_cli_executor(),
            thread_id=thread_id,
            run_suffix="start",
        )
        await ctx.knowledge.ingest(demo_rule_document(), ctx)
        request = default_request(campaign_id=campaign_id, user_request=user_request)
        value = await _graph(runtime).ainvoke(
            initial_scenario_a_state(
                request,
                meta={
                    "tenant_id": ctx.tenant_id,
                    "session_id": ctx.session_id,
                    "thread_id": ctx.thread_id,
                    "run_id": ctx.run_id,
                    "job_id": None,
                    "requester_subject_id": ctx.actor.subject_id,
                },
            ),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        return _result(thread_id, value, campaign_id=campaign_id)
    finally:
        await runtime.aclose()


async def _snapshot(
    runtime: RuntimeServices,
    *,
    thread_id: str,
) -> tuple[Context, StateSnapshot]:
    ctx = _ctx(
        runtime,
        actor=campaign_admin(),
        executor=local_cli_executor(),
        thread_id=thread_id,
        run_suffix="inspect",
    )
    snapshot = await _graph(runtime).aget_state(checkpoint_config(ctx))
    if not snapshot.values:
        raise LookupError("workflow thread is unavailable")
    return ctx, snapshot


def _single_interrupt(snapshot: StateSnapshot, kind: str) -> dict[str, JsonValue]:
    matches = [
        item.value
        for item in snapshot.interrupts
        if isinstance(item.value, dict) and item.value.get("kind") == kind
    ]
    if len(matches) != 1:
        raise ValueError(f"workflow is not waiting for {kind}")
    return cast(dict[str, JsonValue], matches[0])


async def decide_local_approval(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    approval_id: str,
    decision: Literal["approve", "reject"],
    reason: str | None,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        admin_ctx, snapshot = await _snapshot(runtime, thread_id=thread_id)
        pending = [
            kind
            for kind in ("launch_approval", "consumer_publish_approval")
            if any(
                isinstance(item.value, dict) and item.value.get("kind") == kind
                for item in snapshot.interrupts
            )
        ]
        if len(pending) != 1:
            raise ValueError("workflow is not waiting for one approval")
        kind = pending[0]
        frozen = _single_interrupt(snapshot, kind)
        if frozen.get("approval_id") != approval_id:
            raise PermissionError("approval ID does not match the active interrupt")
        approver = launch_approver() if kind == "launch_approval" else consumer_publish_approver()
        approver_ctx = _ctx(
            runtime,
            actor=approver,
            executor=local_cli_executor(),
            thread_id=thread_id,
            run_suffix="decide",
        )
        approval_service = _scenario(runtime).approvals
        current = await approval_service.get_for_decision(
            tenant_id=approver_ctx.tenant_id,
            approval_id=approval_id,
            ctx=approver_ctx,
        )
        expected_status = "approved" if decision == "approve" else "rejected"
        if current.status == "pending":
            await approval_service.decide(
                tenant_id=approver_ctx.tenant_id,
                approval_id=approval_id,
                decision=decision,
                reason=reason,
                ctx=approver_ctx,
            )
        elif current.status != expected_status or current.decision != decision:
            raise PermissionError("approval has a different terminal decision")
        value = await _graph(runtime).ainvoke(
            Command(
                resume={
                    "approval_id": approval_id,
                    "decision": decision,
                    "args_hash": frozen["args_hash"],
                    "checkpoint_id": frozen["checkpoint_id"],
                    "policy_version": frozen["policy_version"],
                }
            ),
            config=checkpoint_config(admin_ctx),
            context=admin_ctx,
        )
        return _result(thread_id, value, approval_id=approval_id, decision=decision)
    finally:
        await runtime.aclose()


async def inject_merchant_event(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
    merchant_id: str,
    product_ref: str,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="merchant-event",
        )
        request = scenario_a_request(cast(Any, snapshot.values))
        result = await _scenario(runtime).ingest_merchant_event(
            cast(Any, snapshot.values),
            {
                "schema_version": 1,
                "event_type": "merchant.enrollment_upserted",
                "tenant_id": ctx.tenant_id,
                "adapter_id": MOCK_MERCHANT_ADAPTER,
                "source_event_id": source_event_id,
                "signature_subject": MOCK_MERCHANT_SUBJECT,
                "version": request.selection_expected_version,
                "payload": {
                    "campaign_id": request.draft.campaign_id,
                    "enrollment_id": "untrusted-caller-value",
                    "merchant_id": merchant_id,
                    "product_ref": product_ref,
                    "product_version": "v1",
                },
            },
            ctx,
        )
        return LocalWorkflowResult(
            thread_id=thread_id,
            status="waiting",
            interrupts=tuple(_public_interrupt(item) for item in snapshot.interrupts),
            detail={"event_status": result.updates.get("status")},
        )
    finally:
        await runtime.aclose()


async def close_enrollment_window(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        _single_interrupt(snapshot, "enrollment_window")
        request = scenario_a_request(cast(Any, snapshot.values))
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="window-close",
        )
        event = {
            "schema_version": 1,
            "event_type": "enrollment.window_closed",
            "tenant_id": ctx.tenant_id,
            "adapter_id": MOCK_MERCHANT_ADAPTER,
            "source_event_id": source_event_id,
            "signature_subject": MOCK_MERCHANT_SUBJECT,
            "version": request.selection_expected_version,
            "payload": {
                "campaign_id": request.draft.campaign_id,
                "enrollment_window_ref": "synthetic-window-v1",
            },
        }
        value = await _graph(runtime).ainvoke(
            Command(resume=event),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        return _result(thread_id, value, source_event_id=source_event_id)
    finally:
        await runtime.aclose()


def _confirmation_subject(snapshot: StateSnapshot, task_id: str) -> tuple[str, str]:
    values = cast(dict[str, Any], snapshot.values)
    tasks: object = values["results"]["enrollment_join"].updates.get("confirmation_tasks")
    for key, result in values["results"].items():
        if key.startswith("confirmation_decision:") and isinstance(
            result.updates.get("confirmation_tasks"), list
        ):
            tasks = result.updates["confirmation_tasks"]
    if not isinstance(tasks, list):
        raise RuntimeError("confirmation task projection is unavailable")
    for task in tasks:
        if isinstance(task, dict) and task.get("confirmation_task_id") == task_id:
            subject_type = task.get("subject_type")
            subject_id = task.get("subject_id")
            if isinstance(subject_type, str) and isinstance(subject_id, str):
                return subject_type, subject_id
    raise LookupError("confirmation task is unavailable")


async def decide_confirmation(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    confirmation_task_id: str,
    decision: Literal["confirm", "reject"],
) -> LocalWorkflowResult:
    inspection = await _runtime(config)
    try:
        _, snapshot = await _snapshot(inspection, thread_id=thread_id)
        pending = _single_interrupt(snapshot, "business_confirmation")
        if pending.get("confirmation_task_id") != confirmation_task_id:
            raise PermissionError("confirmation task does not match the active interrupt")
        subject_type, subject_id = _confirmation_subject(snapshot, confirmation_task_id)
    finally:
        await inspection.aclose()
    actor = _principal(subject_id, subject_type)
    runtime = await _runtime(
        config,
        confirmation_assignments={confirmation_task_id: subject_id},
        extra_actors=(actor,),
    )
    try:
        ctx = _ctx(
            runtime,
            actor=actor,
            executor=local_cli_executor(),
            thread_id=thread_id,
            run_suffix="confirmation",
        )
        value = await _graph(runtime).ainvoke(
            Command(
                resume={
                    "confirmation_task_id": confirmation_task_id,
                    "decision": decision,
                }
            ),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        interruptions = _interrupts(value)
        if (
            len(interruptions) == 1
            and isinstance(interruptions[0].value, dict)
            and (interruptions[0].value.get("kind") == "workflow_handoff")
        ):
            admin_ctx = _ctx(
                runtime,
                actor=campaign_admin(),
                executor=local_cli_executor(),
                thread_id=thread_id,
                run_suffix="confirmation-handoff",
            )
            value = await _graph(runtime).ainvoke(
                Command(resume={"continue": True}),
                config=checkpoint_config(admin_ctx),
                context=admin_ctx,
            )
        return _result(
            thread_id,
            value,
            confirmation_task_id=confirmation_task_id,
            decision=decision,
        )
    finally:
        await runtime.aclose()


def _selection_values(snapshot: StateSnapshot) -> tuple[ScenarioAWorkflowRequest, str, str]:
    state = cast(dict[str, Any], snapshot.values)
    request = scenario_a_request(cast(Any, state))
    submission = state["results"]["assortment_submission"].updates["submission"]
    item_ids = state["results"]["enrollment_join"].updates["enrollment_item_ids"]
    if not isinstance(submission, dict) or not isinstance(
        submission.get("submission_version"), str
    ):
        raise RuntimeError("selection submission binding is unavailable")
    if not isinstance(item_ids, list) or not item_ids or not isinstance(item_ids[0], str):
        raise RuntimeError("selection item binding is unavailable")
    return request, submission["submission_version"], item_ids[0]


async def inject_selection_decision(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
    selection_version: str,
    decision: Literal["selected", "rejected"],
    reason_code: str | None,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        _single_interrupt(snapshot, "selection_event")
        request, submission_version, item_id = _selection_values(snapshot)
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="selection-decision",
        )
        event = {
            "schema_version": 1,
            "event_type": "selection.decision_recorded",
            "tenant_id": ctx.tenant_id,
            "adapter_id": MOCK_SELECTION_ADAPTER,
            "source_event_id": source_event_id,
            "signature_subject": MOCK_SELECTION_SUBJECT,
            "version": request.selection_expected_version,
            "payload": {
                "campaign_id": request.draft.campaign_id,
                "submission_version": submission_version,
                "selection_version": selection_version,
                "enrollment_item_id": item_id,
                "decision": decision,
                "reason_code": reason_code,
            },
        }
        applied = await _scenario(runtime).ingest_selection_decision(
            request, cast(Any, snapshot.values), event, ctx
        )
        return LocalWorkflowResult(
            thread_id=thread_id,
            status="waiting",
            interrupts=tuple(_public_interrupt(item) for item in snapshot.interrupts),
            detail={"selection_version": applied.updates.get("selection_version")},
        )
    finally:
        await runtime.aclose()


async def complete_selection(
    config: ResolvedRuntimeConfig,
    *,
    thread_id: str,
    source_event_id: str,
    selection_version: str,
) -> LocalWorkflowResult:
    runtime = await _runtime(config)
    try:
        _, snapshot = await _snapshot(runtime, thread_id=thread_id)
        _single_interrupt(snapshot, "selection_event")
        request, submission_version, _ = _selection_values(snapshot)
        ctx = _ctx(
            runtime,
            actor=campaign_admin(),
            executor=integration_executor(),
            thread_id=thread_id,
            run_suffix="selection-complete",
        )
        event = {
            "schema_version": 1,
            "event_type": "selection.completed",
            "tenant_id": ctx.tenant_id,
            "adapter_id": MOCK_SELECTION_ADAPTER,
            "source_event_id": source_event_id,
            "signature_subject": MOCK_SELECTION_SUBJECT,
            "version": request.selection_expected_version,
            "payload": {
                "campaign_id": request.draft.campaign_id,
                "submission_version": submission_version,
                "selection_version": selection_version,
            },
        }
        value = await _graph(runtime).ainvoke(
            Command(resume=event),
            config=checkpoint_config(ctx),
            context=ctx,
        )
        return _result(thread_id, value, selection_version=selection_version)
    finally:
        await runtime.aclose()


def workflow_database_paths(config: ResolvedRuntimeConfig) -> tuple[Path, Path]:
    """Return paths used by tests to independently inspect fixture persistence."""

    return config.data_paths.platform_db, config.data_paths.business_db

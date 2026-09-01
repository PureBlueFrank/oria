from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import Command, Interrupt

from oria.core.context import Context
from oria.core.types import NodeResult, ResourceRef
from oria.domain.launch import CampaignDraftSpec
from oria.orchestrator.scenario_a import (
    ScenarioAWorkflowRequest,
    build_scenario_a_graph,
    initial_scenario_a_state,
    rejected_result,
)
from oria.orchestrator.state import ExternalWaitState, WorkflowState

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 10, 4, 0, tzinfo=UTC)
HASH = f"sha256:{'a' * 64}"


def _wait(wait_id: str, event_type: str, checkpoint_id: str) -> ExternalWaitState:
    return {
        "wait_id": wait_id,
        "step_id": event_type,
        "event_type": event_type,
        "resource": ResourceRef(
            resource_type="campaign", resource_id="campaign-a", tenant_id="tenant-a"
        ),
        "expected_version": "1",
        "checkpoint_id": checkpoint_id,
        "correlation_token_hash": HASH,
        "requested_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=2)).isoformat(),
        "timeout_action": "resume",
        "resolved_event_id": None,
        "resolved_at": None,
    }


class FakeScenarioAService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.launch_expired = False

    def _called(self, name: str, **updates: Any) -> NodeResult:
        self.calls.append(name)
        return NodeResult(status="completed", updates=updates)

    async def generate_draft(self, request: object, ctx: object) -> NodeResult:
        del request, ctx
        return self._called("draft", draft={})

    async def request_launch_approval(
        self, request: object, state: object, checkpoint_id: str, ctx: object
    ) -> NodeResult:
        del request, state, ctx
        self.calls.append("launch_approval")
        return NodeResult(
            status="waiting",
            updates={
                "approval": {
                    "approval_id": "approval-launch",
                    "args_hash": HASH,
                    "checkpoint_id": checkpoint_id,
                    "policy_version": "policy-v1",
                }
            },
        )

    async def resume_launch(
        self, request: object, state: object, resume: Any, ctx: object
    ) -> NodeResult:
        del request, state, ctx
        self.calls.append("launch")
        if self.launch_expired:
            raise PermissionError("approval expired")
        return (
            self._called("launch_execute")
            if resume.decision == "approve"
            else rejected_result("launch_rejected")
        )

    async def prepare_enrollment(
        self, request: object, state: object, checkpoint_id: str, ctx: object
    ) -> tuple[NodeResult, dict[str, ExternalWaitState]]:
        del request, state, ctx
        wait = _wait("window-wait", "enrollment.window_closed", checkpoint_id)
        self.calls.append("prepare_enrollment")
        return (
            NodeResult(
                status="waiting",
                updates={"merchant_window_wait": _json_wait(wait)},
            ),
            {wait["wait_id"]: wait},
        )

    async def run_auto_enrollment(self, request: object, state: object, ctx: object) -> NodeResult:
        del request, state, ctx
        return self._called("auto_enrollment", item_ids=["item-a"])

    async def close_merchant_enrollment(
        self, request: object, state: object, event: object, ctx: object
    ) -> NodeResult:
        del request, state, event, ctx
        return self._called("merchant_enrollment", closed=True)

    async def join_enrollment(self, request: object, state: object, ctx: object) -> NodeResult:
        del request, state, ctx
        return self._called("join_enrollment", item_ids=["item-a"])

    async def prepare_confirmation(
        self, request: object, state: dict[str, Any], checkpoint_id: str, ctx: object
    ) -> tuple[NodeResult, ExternalWaitState | None]:
        del request, ctx
        if "confirmation_decision:task-a" in state["results"]:
            return self._called("confirmation_complete"), None
        wait = _wait("confirmation-wait", "confirmation.decision", checkpoint_id)
        self.calls.append("prepare_confirmation")
        return (
            NodeResult(
                status="waiting",
                updates={
                    "confirmation_task_id": "task-a",
                    "wait": _json_wait(wait),
                },
            ),
            wait,
        )

    async def decide_confirmation(
        self, request: object, state: object, resume: object, ctx: object
    ) -> NodeResult:
        del request, state, resume, ctx
        return self._called("confirmation_decision")

    async def link_coupon_batch(self, request: object, state: object, ctx: object) -> NodeResult:
        del request, state, ctx
        return self._called("coupon_link")

    async def submit_assortment(
        self, request: object, state: object, checkpoint_id: str, ctx: object
    ) -> NodeResult:
        del request, state, checkpoint_id, ctx
        return self._called("assortment", submission={"submission_version": "submission-v1"})

    async def prepare_selection_wait(
        self, request: object, state: object, checkpoint_id: str, ctx: object
    ) -> tuple[NodeResult, ExternalWaitState]:
        del request, state, ctx
        wait = _wait("selection-wait", "selection.completed", checkpoint_id)
        self.calls.append("prepare_selection")
        return NodeResult(status="waiting", updates={"wait": _json_wait(wait)}), wait

    async def apply_selection_event(
        self, request: object, state: object, event: object, ctx: object
    ) -> NodeResult:
        del request, state, event, ctx
        return self._called("selection", selection_version="selection-v1")

    async def request_consumer_publish_approval(
        self, request: object, state: object, checkpoint_id: str, ctx: object
    ) -> NodeResult:
        del request, state, ctx
        self.calls.append("consumer_approval")
        return NodeResult(
            status="waiting",
            updates={
                "approval": {
                    "approval_id": "approval-consumer",
                    "args_hash": HASH,
                    "checkpoint_id": checkpoint_id,
                    "policy_version": "policy-v1",
                }
            },
        )

    async def resume_consumer_publish(
        self, request: object, state: object, resume: Any, ctx: object
    ) -> NodeResult:
        del request, state, ctx
        return (
            self._called("consumer_publish")
            if resume.decision == "approve"
            else rejected_result("consumer_publish_rejected")
        )

    async def notify_merchants(self, request: object, state: object, ctx: object) -> NodeResult:
        del request, state, ctx
        return self._called("notifications")


def _json_wait(wait: ExternalWaitState) -> dict[str, Any]:
    return {**wait, "resource": wait["resource"].model_dump(mode="json")}


def _request() -> ScenarioAWorkflowRequest:
    return ScenarioAWorkflowRequest(
        user_request="synthetic request",
        effective_at=NOW,
        draft=CampaignDraftSpec(
            campaign_id="campaign-a",
            coupon_batch_id="coupon-a",
            recruitment_publication_id="recruitment-a",
            material_version="material-v1",
            compensation_policy_version="compensation-v1",
        ),
        enrollment_mode="hybrid",
        circle_run_id="circle-a",
        coupon_link_idempotency_key="link-a",
        assortment_policy_ref="assortment-policy",
        assortment_policy_version="v1",
        assortment_idempotency_key="assortment-a",
        selection_expected_version=1,
        placement_spec={"channel": "fixture"},
        placement_idempotency_key="placement-a",
        notification_template_id="template-a",
        notification_channel="mock",
        notification_idempotency_prefix="notification-a",
        approval_expires_at=NOW + timedelta(days=1),
        external_wait_expires_at=NOW + timedelta(days=2),
    )


def _memory_saver() -> InMemorySaver:
    return InMemorySaver(
        serde=JsonPlusSerializer(
            pickle_fallback=False,
            allowed_msgpack_modules=(
                ("oria.core.types", "NodeResult"),
                ("oria.core.types", "ResourceRef"),
            ),
        )
    )


def _state() -> WorkflowState:
    return initial_scenario_a_state(
        _request(),
        meta={
            "tenant_id": "tenant-a",
            "session_id": "session-a",
            "thread_id": "thread-a",
            "run_id": "run-a",
            "job_id": None,
            "requester_subject_id": "admin-a",
        },
    )


def _resume(interruption: Interrupt, decision: str = "approve") -> dict[str, Any]:
    value = interruption.value
    assert isinstance(value, dict)
    return {
        "approval_id": value["approval_id"],
        "decision": decision,
        "args_hash": value["args_hash"],
        "checkpoint_id": value["checkpoint_id"],
        "policy_version": value["policy_version"],
    }


@pytest.mark.asyncio
async def test_graph_runs_two_independent_hitl_and_external_waits() -> None:
    service = FakeScenarioAService()
    context = cast(Context, SimpleNamespace(domain=SimpleNamespace(scenario_a=service)))
    graph = build_scenario_a_graph(checkpointer=_memory_saver())
    config = cast(RunnableConfig, {"configurable": {"thread_id": "complete-thread"}})

    value = await graph.ainvoke(_state(), config=config, context=context)
    launch = value["__interrupt__"][0]
    assert launch.value["kind"] == "launch_approval"
    value = await graph.ainvoke(
        Command[Any](resume=_resume(launch)), config=config, context=context
    )
    assert value["__interrupt__"][0].value["kind"] == "enrollment_window"
    value = await graph.ainvoke(
        Command[Any](resume={"closed": True}), config=config, context=context
    )
    assert value["__interrupt__"][0].value["kind"] == "business_confirmation"
    value = await graph.ainvoke(
        Command[Any](resume={"confirmation_task_id": "task-a", "decision": "confirm"}),
        config=config,
        context=context,
    )
    assert value["__interrupt__"][0].value["kind"] == "workflow_handoff"
    value = await graph.ainvoke(
        Command[Any](resume={"continue": True}), config=config, context=context
    )
    assert value["__interrupt__"][0].value["kind"] == "selection_event"
    value = await graph.ainvoke(
        Command[Any](resume={"selection": "completed"}), config=config, context=context
    )
    consumer = value["__interrupt__"][0]
    assert consumer.value["kind"] == "consumer_publish_approval"
    assert consumer.value["approval_id"] != launch.value["approval_id"]
    value = await graph.ainvoke(
        Command[Any](resume=_resume(consumer)), config=config, context=context
    )

    assert value["results"]["merchant_notifications"].status == "completed"
    assert value["results"]["auto_enrollment"].status == "completed"
    assert value["results"]["merchant_enrollment"].status == "completed"
    assert service.calls.count("auto_enrollment") >= 1
    assert service.calls.count("merchant_enrollment") == 1
    assert "coupon_link" in service.calls and "selection" in service.calls


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("approval_id", "approval-forged"),
        ("args_hash", f"sha256:{'b' * 64}"),
        ("checkpoint_id", "forged-checkpoint"),
        ("policy_version", "policy-v2"),
    ],
)
async def test_graph_rejects_tampered_checkpoint_binding_before_domain_resume(
    field: str,
    forged: str,
) -> None:
    service = FakeScenarioAService()
    context = cast(Context, SimpleNamespace(domain=SimpleNamespace(scenario_a=service)))
    graph = build_scenario_a_graph(checkpointer=_memory_saver())
    config = cast(RunnableConfig, {"configurable": {"thread_id": "tampered-thread"}})
    value = await graph.ainvoke(_state(), config=config, context=context)
    resume = _resume(value["__interrupt__"][0])
    resume[field] = forged

    with pytest.raises(PermissionError, match="frozen binding"):
        await graph.ainvoke(Command[Any](resume=resume), config=config, context=context)

    assert "launch_execute" not in service.calls


@pytest.mark.security
@pytest.mark.asyncio
async def test_expired_launch_approval_is_rejected_by_domain_resume() -> None:
    service = FakeScenarioAService()
    service.launch_expired = True
    context = cast(Context, SimpleNamespace(domain=SimpleNamespace(scenario_a=service)))
    graph = build_scenario_a_graph(checkpointer=_memory_saver())
    config = cast(RunnableConfig, {"configurable": {"thread_id": "expired-thread"}})
    value = await graph.ainvoke(_state(), config=config, context=context)

    with pytest.raises(PermissionError, match="expired"):
        await graph.ainvoke(
            Command[Any](resume=_resume(value["__interrupt__"][0])),
            config=config,
            context=context,
        )

    assert "launch_execute" not in service.calls


@pytest.mark.security
@pytest.mark.asyncio
async def test_launch_rejection_stops_before_enrollment_side_effects() -> None:
    service = FakeScenarioAService()
    context = cast(Context, SimpleNamespace(domain=SimpleNamespace(scenario_a=service)))
    graph = build_scenario_a_graph(checkpointer=_memory_saver())
    config = cast(RunnableConfig, {"configurable": {"thread_id": "rejected-thread"}})
    value = await graph.ainvoke(_state(), config=config, context=context)
    launch = value["__interrupt__"][0]

    value = await graph.ainvoke(
        Command[Any](resume=_resume(launch, "reject")), config=config, context=context
    )

    assert value["results"]["launch"].status == "failed"
    assert "auto_enrollment" not in service.calls

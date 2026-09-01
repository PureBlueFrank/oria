"""Checkpointed Scenario A graph that composes the reviewed T02--T06 services."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import Field, model_validator

from oria.agent import ResearchRunContext, build_research_graph, initial_research_state
from oria.core.approvals import Approval, ApprovalService
from oria.core.context import Context
from oria.core.integration_events import (
    ExternalWait,
    IntegrationEventInboxService,
    SelectionCompleted,
    SelectionDecisionRecorded,
    parse_integration_event,
)
from oria.core.protocols import Node
from oria.core.types import JsonValue, NodeError, NodeResult, ResourceRef, ValueModel
from oria.domain.assortment import (
    AssortmentService,
    PublishConsumerPlacementArgs,
    SendMerchantNotificationArgs,
    SubmitAssortmentArgs,
    TrustedSelectionEventService,
)
from oria.domain.business import EnrollmentMode
from oria.domain.confirmations import ConfirmationService
from oria.domain.enrollment import (
    AutoCircleRunBinding,
    CouponLinkService,
    EnrollmentItemInput,
    LinkCouponBatchArgs,
    UpsertEnrollmentItemsResult,
)
from oria.domain.enrollment_branch import EnrollmentBranchCoordinator, EnrollmentBranchState
from oria.domain.launch import (
    CampaignDraft,
    CampaignDraftSpec,
    CampaignLaunchService,
    LaunchApprovalBinding,
    LaunchExecutionRequest,
    MaterializeCouponBatchArgs,
    PublishRecruitmentArgs,
)
from oria.domain.products import ProductQueryService
from oria.orchestrator.patterns import parallelization
from oria.orchestrator.state import (
    ExternalWaitState,
    RunMeta,
    WorkflowState,
    empty_workflow_state,
)
from oria.tools.models import QueryEligibleProductsParams

ModelT = TypeVar("ModelT", bound=ValueModel)


class ScenarioAWorkflowRequest(ValueModel):
    """Serializable, immutable inputs whose bindings survive every resume."""

    user_request: str = Field(min_length=1)
    effective_at: datetime
    max_candidates: int = Field(default=10, ge=1, le=100)
    rule_snapshot_id: str | None = Field(default=None, pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    draft: CampaignDraftSpec
    enrollment_mode: EnrollmentMode
    product_limit: int = Field(default=100, ge=1, le=100)
    circle_run_id: str = Field(min_length=1)
    coupon_benefit_tier: Literal["base", "boosted"] = "base"
    coupon_link_idempotency_key: str = Field(min_length=1, max_length=256)
    assortment_policy_ref: str = Field(min_length=1)
    assortment_policy_version: str = Field(min_length=1)
    assortment_idempotency_key: str = Field(min_length=1, max_length=256)
    selection_expected_version: int = Field(ge=1)
    placement_spec: dict[str, JsonValue] = Field(min_length=1)
    placement_idempotency_key: str = Field(min_length=1, max_length=256)
    notification_template_id: str = Field(min_length=1)
    notification_channel: str = Field(min_length=1)
    notification_idempotency_prefix: str = Field(min_length=1, max_length=128)
    approval_expires_at: datetime
    external_wait_expires_at: datetime

    @model_validator(mode="after")
    def validate_bindings(self) -> ScenarioAWorkflowRequest:
        timestamps = (
            self.effective_at,
            self.approval_expires_at,
            self.external_wait_expires_at,
        )
        if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
            raise ValueError("scenario timestamps must include a timezone")
        if self.approval_expires_at <= self.effective_at:
            raise ValueError("approval expiry must follow the scenario effective time")
        if self.external_wait_expires_at <= self.effective_at:
            raise ValueError("external wait expiry must follow the scenario effective time")
        return self


class ApprovalInterruptResume(ValueModel):
    approval_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    args_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)


class ConfirmationInterruptResume(ValueModel):
    confirmation_task_id: str = Field(min_length=1)
    decision: Literal["confirm", "reject"]


class ScenarioAWorkflowService(Protocol):
    """Domain-only seam implemented by a composition of existing T02--T06 services."""

    async def generate_draft(
        self, request: ScenarioAWorkflowRequest, ctx: Context
    ) -> NodeResult: ...

    async def request_launch_approval(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> NodeResult: ...

    async def resume_launch(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        resume: ApprovalInterruptResume,
        ctx: Context,
    ) -> NodeResult: ...

    async def prepare_enrollment(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> tuple[NodeResult, dict[str, ExternalWaitState]]: ...

    async def run_auto_enrollment(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult: ...

    async def close_merchant_enrollment(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        event: object,
        ctx: Context,
    ) -> NodeResult: ...

    async def join_enrollment(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult: ...

    async def prepare_confirmation(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> tuple[NodeResult, ExternalWaitState | None]: ...

    async def decide_confirmation(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        resume: ConfirmationInterruptResume,
        ctx: Context,
    ) -> NodeResult: ...

    async def link_coupon_batch(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult: ...

    async def submit_assortment(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> NodeResult: ...

    async def prepare_selection_wait(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> tuple[NodeResult, ExternalWaitState]: ...

    async def apply_selection_event(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        event: object,
        ctx: Context,
    ) -> NodeResult: ...

    async def request_consumer_publish_approval(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> NodeResult: ...

    async def resume_consumer_publish(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        resume: ApprovalInterruptResume,
        ctx: Context,
    ) -> NodeResult: ...

    async def notify_merchants(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult: ...


class DefaultScenarioAWorkflowService:
    """Compose existing domain services; this class owns no Repository or SQL access."""

    def __init__(
        self,
        *,
        campaign_launch: CampaignLaunchService,
        approvals: ApprovalService,
        products: ProductQueryService,
        enrollment_branches: EnrollmentBranchCoordinator,
        confirmations: ConfirmationService,
        coupon_links: CouponLinkService,
        assortment: AssortmentService,
        selection_events: TrustedSelectionEventService,
        integration_events: IntegrationEventInboxService,
    ) -> None:
        self._campaign_launch = campaign_launch
        self._approvals = approvals
        self._products = products
        self._enrollment_branches = enrollment_branches
        self._confirmations = confirmations
        self._coupon_links = coupon_links
        self._assortment = assortment
        self._selection_events = selection_events
        self._integration_events = integration_events
        self._research = build_research_graph()

    @property
    def approvals(self) -> ApprovalService:
        """Expose the reviewed approval service to the trusted local executor only."""

        return self._approvals

    @property
    def integration_events(self) -> IntegrationEventInboxService:
        """Expose authenticated inbox ingestion to the trusted local executor only."""

        return self._integration_events

    async def ingest_merchant_event(
        self,
        state: WorkflowState,
        event: object,
        ctx: Context,
    ) -> NodeResult:
        """Authenticate and persist one Mock merchant event without resuming the graph."""

        branch = _result_model(state, "enrollment_prepared", "branch_state", EnrollmentBranchState)
        wait = _result_model(
            state, "enrollment_prepared", "merchant_event_domain_wait", ExternalWait
        )
        outcome = await self._enrollment_branches.process_event(branch, event, wait=wait, ctx=ctx)
        if outcome.status not in {"accepted", "duplicate"}:
            raise PermissionError("merchant enrollment event did not pass trusted inbox validation")
        return _node_result(
            status=outcome.status,
            write_result=(
                None
                if outcome.write_result is None
                else outcome.write_result.model_dump(mode="json")
            ),
        )

    async def ingest_selection_decision(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        event: object,
        ctx: Context,
    ) -> NodeResult:
        """Authenticate, deduplicate, and apply a non-resuming selection decision."""

        parsed = parse_integration_event(event)
        if not isinstance(parsed, SelectionDecisionRecorded):
            raise PermissionError("only a selection decision event is accepted")
        completion_wait = _result_model(state, "selection_wait", "domain_wait", ExternalWait)
        wait = ExternalWait(
            tenant_id=ctx.tenant_id,
            wait_id=_stable_id(
                "external_wait",
                ctx.tenant_id,
                parsed.adapter_id,
                parsed.source_event_id,
            ),
            event_type="selection.decision_recorded",
            resource_type="campaign",
            resource_id=request.draft.campaign_id,
            expected_version=parsed.version,
            checkpoint_id=completion_wait.checkpoint_id,
            expires_at=completion_wait.expires_at,
            timeout_action="fail",
            created_at=completion_wait.created_at,
        )
        await self._integration_events.register_wait(wait)
        inbox = await self._integration_events.process(event, wait=wait)
        if not inbox.resume_eligible:
            raise PermissionError("selection decision did not pass trusted inbox validation")
        applied = await self._selection_events.apply(parsed, ctx)
        return _node_result(**applied.model_dump(mode="json"))

    async def generate_draft(self, request: ScenarioAWorkflowRequest, ctx: Context) -> NodeResult:
        research = await self._research.ainvoke(
            initial_research_state(
                user_request=request.user_request,
                effective_at=request.effective_at.isoformat(),
                max_candidates=request.max_candidates,
            ),
            context=ResearchRunContext(ctx=ctx),
        )
        termination = research.get("termination")
        proposal = research.get("proposal")
        rule_result = research.get("rule_result")
        if termination is not None or proposal is None or not isinstance(rule_result, Mapping):
            raise ValueError("research graph did not produce a valid campaign draft")
        snapshot_id = rule_result.get("rule_snapshot_id")
        if not isinstance(snapshot_id, str):
            raise ValueError("research graph did not bind a rule snapshot")
        if request.rule_snapshot_id is not None and request.rule_snapshot_id != snapshot_id:
            raise PermissionError("requested rule snapshot does not match research evidence")
        snapshot = await ctx.rule_snapshots.get(snapshot_id, ctx)
        draft = await self._campaign_launch.persist_campaign_draft(request.draft, snapshot, ctx)
        return _node_result(
            draft=draft.model_dump(mode="json"),
            proposal=proposal,
            research={
                "model_turns": research["model_turns"],
                "rule_snapshot_id": snapshot_id,
                "tool_calls_total": research["tool_calls_total"],
            },
        )

    async def request_launch_approval(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> NodeResult:
        draft = _result_model(state, "draft", "draft", CampaignDraft)
        materialize_args = MaterializeCouponBatchArgs(
            campaign_id=draft.campaign.campaign_id,
            coupon_batch_id=draft.coupon_batch.coupon_batch_id,
            coupon_spec_hash=draft.coupon_batch.coupon_spec_hash,
        )
        publication = draft.recruitment_publication
        publish_args = PublishRecruitmentArgs(
            campaign_id=draft.campaign.campaign_id,
            recruitment_publication_id=publication.recruitment_publication_id,
            merchant_scope_hash=publication.merchant_scope_hash,
            material_version=publication.material_version,
        )
        binding = await self._campaign_launch.request_launch_approval(
            draft=draft,
            materialize_args=materialize_args,
            publish_args=publish_args,
            checkpoint_id=checkpoint_id,
            expires_at=request.approval_expires_at,
            ctx=ctx,
        )
        return NodeResult(
            status="waiting",
            updates=cast(
                dict[str, JsonValue],
                {
                    "approval": _approval_projection(binding.approval),
                    "binding": binding.model_dump(mode="json"),
                    "materialize_args": materialize_args.model_dump(mode="json"),
                    "publish_args": publish_args.model_dump(mode="json"),
                },
            ),
        )

    async def resume_launch(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        resume: ApprovalInterruptResume,
        ctx: Context,
    ) -> NodeResult:
        del request
        if resume.decision == "reject":
            return rejected_result("launch_rejected")
        binding = _result_model(state, "launch_approval", "binding", LaunchApprovalBinding)
        execution = await self._campaign_launch.execute_launch(
            LaunchExecutionRequest(
                plan=binding.plan,
                approval_id=resume.approval_id,
                checkpoint_id=resume.checkpoint_id,
                materialize_args=_result_model(
                    state,
                    "launch_approval",
                    "materialize_args",
                    MaterializeCouponBatchArgs,
                ),
                publish_args=_result_model(
                    state,
                    "launch_approval",
                    "publish_args",
                    PublishRecruitmentArgs,
                ),
            ),
            ctx,
        )
        status: Literal["completed", "waiting"] = (
            "completed" if execution.saga.status == "completed" else "waiting"
        )
        return NodeResult(
            status=status,
            updates=cast(dict[str, JsonValue], execution.model_dump(mode="json")),
        )

    async def prepare_enrollment(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> tuple[NodeResult, dict[str, ExternalWaitState]]:
        draft = _result_model(state, "draft", "draft", CampaignDraft)
        snapshot = await ctx.rule_snapshots.get(draft.rule_snapshot_ref.snapshot_id, ctx)
        branch = EnrollmentBranchState.from_snapshot(
            campaign_id=request.draft.campaign_id,
            snapshot=snapshot,
        )
        if branch.mode != request.enrollment_mode:
            raise PermissionError("enrollment mode does not match the frozen rule snapshot")
        waits: dict[str, ExternalWaitState] = {}
        updates: dict[str, JsonValue] = cast(
            dict[str, JsonValue], {"branch_state": branch.model_dump(mode="json")}
        )
        if branch.mode in {"merchant", "hybrid"}:
            merchant_wait = await self._register_wait(
                event_type="merchant.enrollment_upserted",
                suffix="merchant",
                request=request,
                checkpoint_id=checkpoint_id,
                ctx=ctx,
            )
            window_wait = await self._register_wait(
                event_type="enrollment.window_closed",
                suffix="window",
                request=request,
                checkpoint_id=checkpoint_id,
                ctx=ctx,
            )
            merchant_projection = _wait_projection(merchant_wait, ctx)
            window_projection = _wait_projection(window_wait, ctx)
            waits[merchant_projection["wait_id"]] = merchant_projection
            waits[window_projection["wait_id"]] = window_projection
            updates.update(
                {
                    "merchant_event_domain_wait": merchant_wait.model_dump(mode="json"),
                    "merchant_event_wait": _wait_json(merchant_projection),
                    "merchant_window_domain_wait": window_wait.model_dump(mode="json"),
                    "merchant_window_wait": _wait_json(window_projection),
                }
            )
        return NodeResult(status="waiting", updates=updates), waits

    async def run_auto_enrollment(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult:
        branch = _result_model(state, "enrollment_prepared", "branch_state", EnrollmentBranchState)
        draft = _result_model(state, "draft", "draft", CampaignDraft)
        snapshot = await ctx.rule_snapshots.get(draft.rule_snapshot_ref.snapshot_id, ctx)
        policy = snapshot.enrollment_policy
        query = await self._products.query(
            QueryEligibleProductsParams(
                campaign_id=request.draft.campaign_id,
                rule_snapshot_id=snapshot.snapshot_id,
                product_circle_policy_ref=policy.product_circle_policy_ref,
                product_circle_policy_version=policy.product_circle_policy_version,
                limit=request.product_limit,
            ),
            ctx,
        )
        items = tuple(
            EnrollmentItemInput(
                merchant_id=item.merchant_id,
                product_ref=item.product_ref,
                product_version=item.product_version,
            )
            for item in query.products
        )
        if not items:
            raise ValueError("auto enrollment produced no hard-eligible products")
        outcome = await self._enrollment_branches.complete_auto(
            branch,
            items,
            binding=AutoCircleRunBinding.for_items(
                campaign_id=request.draft.campaign_id,
                circle_run_id=request.circle_run_id,
                product_circle_policy_ref=query.product_circle_policy_ref,
                product_circle_policy_version=query.product_circle_policy_version,
                catalog_snapshot_id=query.catalog_snapshot_id,
                items=items,
            ),
            ctx=ctx,
        )
        return _node_result(
            branch_state=outcome.state.model_dump(mode="json"),
            query=query.model_dump(mode="json"),
            write_result=(
                None
                if outcome.write_result is None
                else outcome.write_result.model_dump(mode="json")
            ),
        )

    async def close_merchant_enrollment(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        event: object,
        ctx: Context,
    ) -> NodeResult:
        del request
        branch = _result_model(state, "enrollment_prepared", "branch_state", EnrollmentBranchState)
        wait = _result_model(
            state, "enrollment_prepared", "merchant_window_domain_wait", ExternalWait
        )
        outcome = await self._enrollment_branches.process_event(branch, event, wait=wait, ctx=ctx)
        if outcome.status != "window_closed" or not outcome.state.window_closed:
            raise PermissionError("merchant branch resumes only for a trusted window-close event")
        return _node_result(branch_state=outcome.state.model_dump(mode="json"))

    async def join_enrollment(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult:
        del ctx
        if request.enrollment_mode in {"auto", "hybrid"}:
            write = _result_model(
                state, "auto_enrollment", "write_result", UpsertEnrollmentItemsResult
            )
            item_ids = tuple(item.enrollment_item_id for item in write.enrollment_items)
            merchant_ids = tuple(sorted({item.merchant_id for item in write.enrollment_items}))
            tasks = write.confirmation_tasks
        else:
            raise RuntimeError("merchant-only join requires a persisted enrollment read projection")
        if request.enrollment_mode == "hybrid":
            merchant = _result(state, "merchant_enrollment")
            if not merchant.updates.get("branch_state"):
                raise RuntimeError("hybrid enrollment did not close the merchant branch")
        return _node_result(
            enrollment_item_ids=list(item_ids),
            merchant_ids=list(merchant_ids),
            confirmation_tasks=[task.model_dump(mode="json") for task in tasks],
        )

    async def prepare_confirmation(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> tuple[NodeResult, ExternalWaitState | None]:
        del request
        raw_tasks = _result(state, "enrollment_join").updates.get("confirmation_tasks")
        if not isinstance(raw_tasks, list):
            raise RuntimeError("enrollment join did not expose confirmation tasks")
        tasks = _object_list(raw_tasks, "confirmation tasks")
        for key, result in state["results"].items():
            if key.startswith("confirmation_decision:"):
                current = result.updates.get("confirmation_tasks")
                if isinstance(current, list):
                    tasks = _object_list(current, "confirmation tasks")
        pending = [task for task in tasks if task.get("status") == "pending"]
        if not pending:
            return _node_result(confirmation_tasks=cast(JsonValue, tasks)), None
        task = min(pending, key=lambda item: cast(int, item["sequence"]))
        task_id = cast(str, task["confirmation_task_id"])
        wait_id = _stable_id("confirmation_wait", ctx.tenant_id, task_id, checkpoint_id)
        wait: ExternalWaitState = {
            "wait_id": wait_id,
            "step_id": "dynamic_confirmation",
            "event_type": "confirmation.decision",
            "resource": ResourceRef(
                resource_type="confirmation_task",
                resource_id=task_id,
                tenant_id=ctx.tenant_id,
            ),
            "expected_version": str(task["version"]),
            "checkpoint_id": checkpoint_id,
            "correlation_token_hash": _sha256(wait_id),
            "requested_at": cast(str, task["updated_at"]),
            "expires_at": cast(str, task["due_at"]),
            "timeout_action": "fail",
            "resolved_event_id": None,
            "resolved_at": None,
        }
        return (
            NodeResult(
                status="waiting",
                updates=cast(
                    dict[str, JsonValue],
                    {
                        "confirmation_task_id": task_id,
                        "wait": _wait_json(wait),
                    },
                ),
            ),
            wait,
        )

    async def decide_confirmation(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        resume: ConfirmationInterruptResume,
        ctx: Context,
    ) -> NodeResult:
        del request, state
        resolution = await self._confirmations.decide(
            resume.confirmation_task_id, resume.decision, ctx
        )
        return _node_result(**resolution.model_dump(mode="json"))

    async def link_coupon_batch(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult:
        item_ids = _string_tuple(
            _result(state, "enrollment_join").updates.get("enrollment_item_ids"),
            "enrollment item IDs",
        )
        linked = await self._coupon_links.link(
            LinkCouponBatchArgs(
                enrollment_item_ids=item_ids,
                coupon_batch_id=request.draft.coupon_batch_id,
                tier_mapping={item_id: request.coupon_benefit_tier for item_id in item_ids},
                idempotency_key=request.coupon_link_idempotency_key,
            ),
            ctx,
        )
        return _node_result(**linked.model_dump(mode="json"))

    async def submit_assortment(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> NodeResult:
        item_ids = _string_tuple(
            _result(state, "enrollment_join").updates.get("enrollment_item_ids"),
            "enrollment item IDs",
        )
        submitted = await self._assortment.submit(
            SubmitAssortmentArgs(
                campaign_id=request.draft.campaign_id,
                enrollment_item_ids=item_ids,
                assortment_policy_ref=request.assortment_policy_ref,
                assortment_policy_version=request.assortment_policy_version,
                idempotency_key=request.assortment_idempotency_key,
            ),
            checkpoint_id=checkpoint_id,
            ctx=ctx,
        )
        return _node_result(**submitted.model_dump(mode="json"))

    async def prepare_selection_wait(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> tuple[NodeResult, ExternalWaitState]:
        submission = _result(state, "assortment_submission").updates.get("submission")
        if not isinstance(submission, Mapping) or not isinstance(
            submission.get("submission_version"), str
        ):
            raise RuntimeError("assortment submission binding is unavailable")
        wait = await self._register_wait(
            event_type="selection.completed",
            suffix="selection",
            request=request,
            checkpoint_id=checkpoint_id,
            ctx=ctx,
        )
        projection = _wait_projection(wait, ctx)
        result = NodeResult(
            status="waiting",
            updates=cast(
                dict[str, JsonValue],
                {
                    "domain_wait": wait.model_dump(mode="json"),
                    "submission_version": submission["submission_version"],
                    "wait": _wait_json(projection),
                },
            ),
        )
        return result, projection

    async def apply_selection_event(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        event: object,
        ctx: Context,
    ) -> NodeResult:
        del request
        parsed = parse_integration_event(event)
        if not isinstance(parsed, SelectionCompleted):
            raise PermissionError("selection wait resumes only for a completion event")
        wait = _result_model(state, "selection_wait", "domain_wait", ExternalWait)
        inbox = await self._integration_events.process(event, wait=wait)
        if not inbox.resume_eligible:
            raise PermissionError("selection event did not pass trusted inbox validation")
        applied = await self._selection_events.apply(parsed, ctx)
        return _node_result(**applied.model_dump(mode="json"))

    async def request_consumer_publish_approval(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        checkpoint_id: str,
        ctx: Context,
    ) -> NodeResult:
        selection_version = _required_string(
            _result(state, "selection").updates.get("selection_version"),
            "selection version",
        )
        approval = await self._assortment.request_consumer_publish_approval(
            PublishConsumerPlacementArgs(
                campaign_id=request.draft.campaign_id,
                selection_version=selection_version,
                placement_spec=request.placement_spec,
                idempotency_key=request.placement_idempotency_key,
            ),
            checkpoint_id=checkpoint_id,
            expires_at=request.approval_expires_at,
            ctx=ctx,
        )
        return NodeResult(
            status="waiting",
            updates={"approval": _approval_projection(approval)},
        )

    async def resume_consumer_publish(
        self,
        request: ScenarioAWorkflowRequest,
        state: WorkflowState,
        resume: ApprovalInterruptResume,
        ctx: Context,
    ) -> NodeResult:
        if resume.decision == "reject":
            return rejected_result("consumer_publish_rejected")
        selection_version = _required_string(
            _result(state, "selection").updates.get("selection_version"),
            "selection version",
        )
        published = await self._assortment.publish_consumer_placement(
            PublishConsumerPlacementArgs(
                campaign_id=request.draft.campaign_id,
                selection_version=selection_version,
                placement_spec=request.placement_spec,
                idempotency_key=request.placement_idempotency_key,
                approval_id=resume.approval_id,
            ),
            checkpoint_id=resume.checkpoint_id,
            ctx=ctx,
        )
        return _node_result(**published.model_dump(mode="json"))

    async def notify_merchants(
        self, request: ScenarioAWorkflowRequest, state: WorkflowState, ctx: Context
    ) -> NodeResult:
        merchant_ids = _string_tuple(
            _result(state, "enrollment_join").updates.get("merchant_ids"),
            "merchant IDs",
        )
        selection_version = _required_string(
            _result(state, "selection").updates.get("selection_version"),
            "selection version",
        )
        checkpoint_id = cast(
            str, _approval_payload(state, "consumer_publish_approval")["checkpoint_id"]
        )
        notifications = []
        for merchant_id in merchant_ids:
            sent = await self._assortment.send_merchant_notification(
                SendMerchantNotificationArgs(
                    merchant_id=merchant_id,
                    campaign_id=request.draft.campaign_id,
                    result_version=selection_version,
                    template_id=request.notification_template_id,
                    channel=request.notification_channel,
                    idempotency_key=(f"{request.notification_idempotency_prefix}:{merchant_id}"),
                ),
                checkpoint_id=checkpoint_id,
                ctx=ctx,
            )
            notifications.append(sent.model_dump(mode="json"))
        return _node_result(notifications=cast(JsonValue, notifications))

    async def _register_wait(
        self,
        *,
        event_type: Literal[
            "merchant.enrollment_upserted",
            "enrollment.window_closed",
            "selection.completed",
        ],
        suffix: str,
        request: ScenarioAWorkflowRequest,
        checkpoint_id: str,
        ctx: Context,
    ) -> ExternalWait:
        wait = ExternalWait(
            tenant_id=ctx.tenant_id,
            wait_id=_stable_id(
                "external_wait",
                ctx.tenant_id,
                request.draft.campaign_id,
                suffix,
                checkpoint_id,
            ),
            event_type=event_type,
            resource_type="campaign",
            resource_id=request.draft.campaign_id,
            expected_version=request.selection_expected_version,
            checkpoint_id=checkpoint_id,
            expires_at=request.external_wait_expires_at,
            timeout_action="fail",
            created_at=request.effective_at,
        )
        return await self._integration_events.register_wait(wait)


def initial_scenario_a_state(
    request: ScenarioAWorkflowRequest,
    *,
    meta: RunMeta,
) -> WorkflowState:
    """Create a new state without placing Context or services in a checkpoint."""

    return empty_workflow_state(
        plan={
            "goal": "scenario_a_complete_booking",
            "steps": [
                {
                    "node_id": "scenario_a",
                    "params": request.model_dump(mode="json"),
                }
            ],
        },
        meta=meta,
    )


def _node_result(**updates: JsonValue) -> NodeResult:
    return NodeResult(status="completed", updates=updates)


def _approval_projection(approval: Approval) -> dict[str, JsonValue]:
    return {
        "approval_id": approval.approval_id,
        "args_hash": approval.canonical_args_hash,
        "checkpoint_id": approval.checkpoint_id,
        "policy_version": approval.policy_version,
    }


def _result_model(
    state: WorkflowState,
    result_key: str,
    update_key: str,
    model: type[ModelT],
) -> ModelT:
    raw = _result(state, result_key).updates.get(update_key)
    if raw is None:
        raise RuntimeError(f"{result_key} did not produce {update_key}")
    return model.model_validate(raw)


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"workflow did not produce {label}")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise RuntimeError(f"workflow did not produce {label}")
    return tuple(value)


def _object_list(value: list[JsonValue], label: str) -> list[dict[str, JsonValue]]:
    items: list[dict[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, Mapping) or any(not isinstance(key, str) for key in item):
            raise RuntimeError(f"workflow did not produce valid {label}")
        items.append(cast(dict[str, JsonValue], dict(item)))
    return items


def _sha256(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _wait_projection(wait: ExternalWait, ctx: Context) -> ExternalWaitState:
    return {
        "wait_id": wait.wait_id,
        "step_id": wait.event_type,
        "event_type": wait.event_type,
        "resource": ResourceRef(
            resource_type=wait.resource_type,
            resource_id=wait.resource_id,
            tenant_id=wait.tenant_id,
        ),
        "expected_version": str(wait.expected_version),
        "checkpoint_id": wait.checkpoint_id,
        "correlation_token_hash": _sha256(f"{ctx.correlation_id}:{wait.wait_id}"),
        "requested_at": wait.created_at.isoformat(),
        "expires_at": wait.expires_at.isoformat(),
        "timeout_action": "fail",
        "resolved_event_id": None,
        "resolved_at": None,
    }


def _wait_json(wait: ExternalWaitState) -> JsonValue:
    return cast(
        JsonValue,
        {
            **wait,
            "resource": wait["resource"].model_dump(mode="json"),
        },
    )


def scenario_a_request(state: WorkflowState) -> ScenarioAWorkflowRequest:
    steps = state["plan"]["steps"]
    if len(steps) != 1 or steps[0]["node_id"] != "scenario_a":
        raise ValueError("workflow plan does not contain exactly one Scenario A request")
    return ScenarioAWorkflowRequest.model_validate(steps[0]["params"])


def _service(ctx: Context) -> ScenarioAWorkflowService:
    service = ctx.domain.scenario_a
    if service is None:
        raise RuntimeError("Scenario A workflow service is unavailable")
    return service


def _checkpoint_id(runtime: Runtime[Context]) -> str:
    info = runtime.execution_info
    if info is None or not info.checkpoint_id:
        raise RuntimeError("LangGraph did not provide a trusted checkpoint id")
    return info.checkpoint_id


def _result(state: WorkflowState, key: str) -> NodeResult:
    try:
        return state["results"][key]
    except KeyError as exc:
        raise RuntimeError(f"required workflow result is missing: {key}") from exc


def _approval_payload(state: WorkflowState, key: str) -> dict[str, JsonValue]:
    result = _result(state, key)
    payload = result.updates.get("approval")
    if not isinstance(payload, Mapping):
        raise RuntimeError(f"{key} did not produce an approval binding")
    required = ("approval_id", "args_hash", "checkpoint_id", "policy_version")
    if any(not isinstance(payload.get(name), str) for name in required):
        raise RuntimeError(f"{key} produced an invalid approval binding")
    return cast(dict[str, JsonValue], dict(payload))


def _validate_approval_resume(
    value: object,
    *,
    frozen: Mapping[str, JsonValue],
) -> ApprovalInterruptResume:
    resume = ApprovalInterruptResume.model_validate(value)
    observed = (
        resume.approval_id,
        resume.args_hash,
        resume.checkpoint_id,
        resume.policy_version,
    )
    expected = tuple(
        cast(str, frozen[name])
        for name in ("approval_id", "args_hash", "checkpoint_id", "policy_version")
    )
    if observed != expected:
        raise PermissionError("approval resume payload does not match the frozen binding")
    return resume


async def _draft(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    result = await _service(runtime.context).generate_draft(
        scenario_a_request(state), runtime.context
    )
    return {"results": {"draft": result}}


async def _request_launch(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    result = await _service(runtime.context).request_launch_approval(
        scenario_a_request(state), state, _checkpoint_id(runtime), runtime.context
    )
    return {"results": {"launch_approval": result}}


async def _launch_hitl(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    frozen = _approval_payload(state, "launch_approval")
    value = interrupt({"kind": "launch_approval", **frozen})
    resume = _validate_approval_resume(value, frozen=frozen)
    result = await _service(runtime.context).resume_launch(
        scenario_a_request(state), state, resume, runtime.context
    )
    return {"results": {"launch": result}}


async def _prepare_enrollment(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    result, waits = await _service(runtime.context).prepare_enrollment(
        scenario_a_request(state), state, _checkpoint_id(runtime), runtime.context
    )
    return {"results": {"enrollment_prepared": result}, "external_waits": waits}


class _AutoEnrollmentNode:
    name = "auto_enrollment"

    async def execute(self, state: dict[str, Any], ctx: Context) -> NodeResult:
        workflow = cast(WorkflowState, state)
        return await _service(ctx).run_auto_enrollment(scenario_a_request(workflow), workflow, ctx)


class _MerchantEnrollmentNode:
    name = "merchant_enrollment"

    async def execute(self, state: dict[str, Any], ctx: Context) -> NodeResult:
        workflow = cast(WorkflowState, state)
        prepared = _result(workflow, "enrollment_prepared")
        wait = prepared.updates.get("merchant_window_wait")
        if not isinstance(wait, Mapping):
            raise RuntimeError("merchant enrollment window wait is unavailable")
        event = interrupt({"kind": "enrollment_window", **dict(wait)})
        return await _service(ctx).close_merchant_enrollment(
            scenario_a_request(workflow), workflow, event, ctx
        )


def _enrollment_route(state: WorkflowState) -> str:
    return scenario_a_request(state).enrollment_mode


async def _join_enrollment(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    result = await _service(runtime.context).join_enrollment(
        scenario_a_request(state), state, runtime.context
    )
    return {"results": {"enrollment_join": result}}


async def _prepare_confirmation(
    state: WorkflowState, runtime: Runtime[Context]
) -> dict[str, object]:
    result, wait = await _service(runtime.context).prepare_confirmation(
        scenario_a_request(state), state, _checkpoint_id(runtime), runtime.context
    )
    task_id = result.updates.get("confirmation_task_id")
    key = "confirmation_chain_complete" if task_id is None else f"confirmation_wait:{task_id}"
    update: dict[str, object] = {"results": {key: result}}
    if wait is not None:
        update["external_waits"] = {wait["wait_id"]: wait}
    return update


def _confirmation_route(state: WorkflowState) -> str:
    return END if "confirmation_chain_complete" in state["results"] else "confirmation_hitl"


def _pending_confirmation(state: WorkflowState) -> tuple[str, NodeResult]:
    decided = {
        key.removeprefix("confirmation_decision:")
        for key in state["results"]
        if key.startswith("confirmation_decision:")
    }
    waiting = [
        (key.removeprefix("confirmation_wait:"), value)
        for key, value in state["results"].items()
        if key.startswith("confirmation_wait:")
        and key.removeprefix("confirmation_wait:") not in decided
    ]
    if len(waiting) != 1:
        raise RuntimeError("workflow must have exactly one active confirmation task")
    return waiting[0]


async def _confirmation_hitl(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    task_id, waiting = _pending_confirmation(state)
    value = interrupt(
        {
            "kind": "business_confirmation",
            "confirmation_task_id": task_id,
            "wait": waiting.updates.get("wait"),
        }
    )
    resume = ConfirmationInterruptResume.model_validate(value)
    if resume.confirmation_task_id != task_id:
        raise PermissionError("confirmation resume does not match the active task")
    result = await _service(runtime.context).decide_confirmation(
        scenario_a_request(state), state, resume, runtime.context
    )
    return {"results": {f"confirmation_decision:{task_id}": result}}


async def _confirmation_handoff(state: WorkflowState) -> dict[str, object]:
    value = interrupt({"kind": "workflow_handoff", "target_role": "campaign_admin"})
    if not isinstance(value, Mapping) or value.get("continue") is not True:
        raise PermissionError("confirmation handoff requires the trusted workflow executor")
    return {"results": {"confirmation_handoff": _node_result(continued=True)}}


async def _link_coupon(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    result = await _service(runtime.context).link_coupon_batch(
        scenario_a_request(state), state, runtime.context
    )
    return {"results": {"coupon_link": result}}


async def _submit_assortment(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    result = await _service(runtime.context).submit_assortment(
        scenario_a_request(state), state, _checkpoint_id(runtime), runtime.context
    )
    return {"results": {"assortment_submission": result}}


async def _prepare_selection_wait(
    state: WorkflowState, runtime: Runtime[Context]
) -> dict[str, object]:
    result, wait = await _service(runtime.context).prepare_selection_wait(
        scenario_a_request(state), state, _checkpoint_id(runtime), runtime.context
    )
    return {
        "results": {"selection_wait": result},
        "external_waits": {wait["wait_id"]: wait},
    }


async def _selection_event_wait(
    state: WorkflowState, runtime: Runtime[Context]
) -> dict[str, object]:
    wait = _result(state, "selection_wait").updates.get("wait")
    if not isinstance(wait, Mapping):
        raise RuntimeError("selection wait binding is unavailable")
    event = interrupt({"kind": "selection_event", **dict(wait)})
    result = await _service(runtime.context).apply_selection_event(
        scenario_a_request(state), state, event, runtime.context
    )
    return {"results": {"selection": result}}


async def _request_consumer_publish(
    state: WorkflowState, runtime: Runtime[Context]
) -> dict[str, object]:
    result = await _service(runtime.context).request_consumer_publish_approval(
        scenario_a_request(state), state, _checkpoint_id(runtime), runtime.context
    )
    return {"results": {"consumer_publish_approval": result}}


async def _consumer_publish_hitl(
    state: WorkflowState, runtime: Runtime[Context]
) -> dict[str, object]:
    frozen = _approval_payload(state, "consumer_publish_approval")
    value = interrupt({"kind": "consumer_publish_approval", **frozen})
    resume = _validate_approval_resume(value, frozen=frozen)
    result = await _service(runtime.context).resume_consumer_publish(
        scenario_a_request(state), state, resume, runtime.context
    )
    return {"results": {"consumer_publish": result}}


async def _notify_merchants(state: WorkflowState, runtime: Runtime[Context]) -> dict[str, object]:
    result = await _service(runtime.context).notify_merchants(
        scenario_a_request(state), state, runtime.context
    )
    return {"results": {"merchant_notifications": result}}


def _continue_after(result_key: str) -> Callable[[WorkflowState], str]:
    def route(state: WorkflowState) -> str:
        return END if _result(state, result_key).status == "failed" else "continue"

    return route


def build_scenario_a_graph(
    *,
    checkpointer: BaseCheckpointSaver[Any],
) -> CompiledStateGraph[WorkflowState, Context, WorkflowState, WorkflowState]:
    """Compile the ten-step graph with official checkpoint and interrupt semantics."""

    merchant = cast(Node, _MerchantEnrollmentNode())
    auto = cast(Node, _AutoEnrollmentNode())
    enrollment_graphs = {
        "merchant": parallelization([merchant], join="merge"),
        "auto": parallelization([auto], join="merge"),
        "hybrid": parallelization([merchant, auto], join="merge"),
    }

    builder = StateGraph(WorkflowState, context_schema=Context)
    builder.add_node("draft", _draft)
    builder.add_node("request_launch_approval", _request_launch)
    builder.add_node("launch_hitl", _launch_hitl)
    builder.add_node("prepare_enrollment", _prepare_enrollment)
    for mode, graph in enrollment_graphs.items():
        builder.add_node(f"enrollment_{mode}", graph)
    builder.add_node("join_enrollment", _join_enrollment)
    builder.add_node("prepare_confirmation", _prepare_confirmation)
    builder.add_node("confirmation_hitl", _confirmation_hitl)
    builder.add_node("confirmation_handoff", _confirmation_handoff)
    builder.add_node("link_coupon_batch", _link_coupon)
    builder.add_node("submit_assortment", _submit_assortment)
    builder.add_node("prepare_selection_wait", _prepare_selection_wait)
    builder.add_node("selection_event_wait", _selection_event_wait)
    builder.add_node("request_consumer_publish_approval", _request_consumer_publish)
    builder.add_node("consumer_publish_hitl", _consumer_publish_hitl)
    builder.add_node("notify_merchants", _notify_merchants)

    builder.add_edge(START, "draft")
    builder.add_edge("draft", "request_launch_approval")
    builder.add_edge("request_launch_approval", "launch_hitl")
    builder.add_conditional_edges(
        "launch_hitl",
        _continue_after("launch"),
        {"continue": "prepare_enrollment", END: END},
    )
    builder.add_conditional_edges(
        "prepare_enrollment",
        lambda state: f"enrollment_{_enrollment_route(state)}",
        {f"enrollment_{mode}": f"enrollment_{mode}" for mode in enrollment_graphs},
    )
    for mode in enrollment_graphs:
        builder.add_edge(f"enrollment_{mode}", "join_enrollment")
    builder.add_edge("join_enrollment", "prepare_confirmation")
    builder.add_conditional_edges(
        "prepare_confirmation",
        _confirmation_route,
        {"confirmation_hitl": "confirmation_hitl", END: "confirmation_handoff"},
    )
    builder.add_edge("confirmation_hitl", "prepare_confirmation")
    builder.add_edge("confirmation_handoff", "link_coupon_batch")
    builder.add_edge("link_coupon_batch", "submit_assortment")
    builder.add_edge("submit_assortment", "prepare_selection_wait")
    builder.add_edge("prepare_selection_wait", "selection_event_wait")
    builder.add_edge("selection_event_wait", "request_consumer_publish_approval")
    builder.add_edge("request_consumer_publish_approval", "consumer_publish_hitl")
    builder.add_conditional_edges(
        "consumer_publish_hitl",
        _continue_after("consumer_publish"),
        {"continue": "notify_merchants", END: END},
    )
    builder.add_edge("notify_merchants", END)
    return builder.compile(checkpointer=checkpointer, name="scenario_a")


def rejected_result(code: str) -> NodeResult:
    """Return a stable terminal result for an explicitly rejected HITL decision."""

    return NodeResult(
        status="failed",
        error=NodeError(code=code, safe_message="Workflow approval was rejected"),
    )

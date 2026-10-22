"""Fixed draft persistence and coupon reservation inside trusted Job transactions."""

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.context import Context
from oria.core.jobs import Job
from oria.core.types import EventEnvelope, JsonValue, ResourceRef
from oria.domain.business import LaunchSagaState
from oria.domain.launch import (
    CampaignDraft,
    DefaultCampaignLaunchService,
    MaterializeCouponBatchArgs,
    PublishRecruitmentArgs,
)
from oria.domain.ledger import DomainEvent, LaunchPlan, OutboxRecord, ToolExecution
from oria.permission.local import LOCAL_POLICY_VERSION
from oria.storage.ledger import (
    SQLiteBusinessAuditRepository,
    SQLiteDomainEventRepository,
    SQLiteOutboxRepository,
    SQLiteToolExecutionRepository,
)
from oria.storage.repositories import SQLiteCampaignDraftRepository, SQLiteLaunchSagaStateRepository


class JobCampaignDraftWriter:
    """No callback, SQL, free tool/action name or adapter crosses this boundary."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._drafts = SQLiteCampaignDraftRepository(sessions)
        self._tools = SQLiteToolExecutionRepository(sessions)
        self._events = SQLiteDomainEventRepository(sessions)
        self._audit = SQLiteBusinessAuditRepository(sessions)
        self._outbox = SQLiteOutboxRepository(sessions)

    async def persist(
        self, session: AsyncSession, draft: CampaignDraft, reservation: ToolExecution, ctx: Context
    ) -> None:
        execution = await self._tools._reserve_in_session(session, reservation)
        if execution.status == "succeeded":
            if execution.receipt_summary_hash != draft.campaign_draft_hash:
                raise ValueError("draft receipt does not match the requested facts")
            # Return only if all original draft facts are still present and unchanged.
            pairs = (
                (
                    self._drafts._rule_refs,
                    draft.rule_snapshot_ref,
                    draft.rule_snapshot_ref.campaign_rule_snapshot_ref_id,
                ),
                (self._drafts._campaigns, draft.campaign, draft.campaign.campaign_id),
                (self._drafts._coupons, draft.coupon_batch, draft.coupon_batch.coupon_batch_id),
                (
                    self._drafts._publications,
                    draft.recruitment_publication,
                    draft.recruitment_publication.recruitment_publication_id,
                ),
            )
            for repository, entity, entity_id in pairs:
                if await repository._find_by_id(session, entity_id, ctx.tenant_id) != entity:
                    raise ValueError("persisted draft facts changed or are unavailable")
            return
        if execution != reservation:
            raise ValueError("local draft reservation is not recoverable")
        await self._drafts._create_bundle(
            session,
            rule_snapshot_ref=draft.rule_snapshot_ref,
            campaign=draft.campaign,
            coupon_batch=draft.coupon_batch,
            recruitment_publication=draft.recruitment_publication,
            ctx=ctx,
        )
        await self._tools._transition(
            session,
            tenant_id=ctx.tenant_id,
            execution_id=execution.execution_id,
            target="executing",
            expected_status="reserved",
            updated_at=reservation.updated_at,
        )
        await self._tools._transition(
            session,
            tenant_id=ctx.tenant_id,
            execution_id=execution.execution_id,
            target="succeeded",
            expected_status="executing",
            updated_at=reservation.updated_at,
            receipt_id=execution.execution_id,
            receipt_summary_hash=draft.campaign_draft_hash,
        )
        event_id = f"{execution.execution_id}:persisted"
        payload: dict[str, JsonValue] = {
            "job_id": ctx.job_id,
            "execution_id": execution.execution_id,
        }
        now = reservation.created_at
        await self._events._append(
            session,
            DomainEvent(
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                aggregate_type="campaign",
                aggregate_id=draft.campaign.campaign_id,
                event_type="campaign.draft.persisted",
                event_version=1,
                payload=payload,
                occurred_at=now,
                correlation_id=ctx.correlation_id,
            ),
        )
        await self._audit._append(
            session,
            EventEnvelope(
                event_id=f"{execution.execution_id}:audit",
                occurred_at=now,
                tenant_id=ctx.tenant_id,
                actor=ctx.actor.subject_id,
                action="campaign:draft:write",
                resource=ResourceRef(
                    tenant_id=ctx.tenant_id,
                    resource_type="campaign",
                    resource_id=draft.campaign.campaign_id,
                ),
                decision="allow",
                policy_version=LOCAL_POLICY_VERSION,
                args_hash=execution.canonical_args_hash,
                result="success",
                correlation_id=ctx.correlation_id,
                payload=payload,
            ),
        )
        await self._outbox._append(
            session,
            OutboxRecord(
                event_id=event_id,
                tenant_id=ctx.tenant_id,
                topic="campaign.draft.persisted",
                payload_json=json.dumps(payload),
                occurred_at=now,
                available_at=now,
            ),
        )


class JobCouponReservationWriter:
    """Read current unlaunched facts and reserve only in the caller's transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._drafts = SQLiteCampaignDraftRepository(sessions)
        self._tools = SQLiteToolExecutionRepository(sessions)
        self._sagas = SQLiteLaunchSagaStateRepository(sessions)

    async def prepare(
        self, session: AsyncSession, job: Job, args: MaterializeCouponBatchArgs, plan: LaunchPlan
    ) -> str:
        await self.validate_draft(session, job, args, plan)
        saga = await self._sagas._find_by_unique_key(
            session, (job.tenant_id, args.campaign_id), job.tenant_id
        )
        if saga is None or saga.status != "planned":
            raise PermissionError("coupon reservation requires a persisted planned saga")
        return saga.checkpoint

    @staticmethod
    def launch_saga(job: Job, args: MaterializeCouponBatchArgs) -> LaunchSagaState:
        """Stable logical approval point; it is never a saver checkpoint."""
        return LaunchSagaState(
            launch_saga_id=f"{job.job_id}:launch:v1",
            tenant_id=job.tenant_id,
            campaign_id=args.campaign_id,
            status="planned",
            checkpoint=f"job:{job.job_id}:launch-approval:v1",
            version=1,
            created_at=job.created_at,
            updated_at=job.created_at,
        )

    async def ensure_launch(
        self, session: AsyncSession, job: Job, args: MaterializeCouponBatchArgs, plan: LaunchPlan
    ) -> None:
        await self.validate_draft(session, job, args, plan)
        expected = self.launch_saga(job, args)
        saga = await self._sagas._find_by_unique_key(
            session, (job.tenant_id, args.campaign_id), job.tenant_id
        )
        if saga is None:
            await self._sagas._insert(session, expected)
        elif saga != expected:
            raise PermissionError("existing launch saga does not match the Job approval point")

    async def validate_draft(
        self, session: AsyncSession, job: Job, args: MaterializeCouponBatchArgs, plan: LaunchPlan
    ) -> None:
        # Obtain the Business DB write lock BEFORE reading any binding facts. Other
        # Business writers cannot change draft/saga/history before this commit.
        campaign_id = (
            await session.execute(
                text(
                    "UPDATE campaigns SET version = version WHERE tenant_id = :tenant "
                    "AND campaign_id = :campaign RETURNING campaign_id"
                ),
                {"tenant": job.tenant_id, "campaign": args.campaign_id},
            )
        ).scalar_one_or_none()
        if campaign_id is None:
            raise PermissionError("persisted Job draft is unavailable")
        campaign = await self._drafts._campaigns._find_by_id(
            session, args.campaign_id, job.tenant_id
        )
        if campaign is None:
            raise PermissionError("persisted Job campaign is unavailable")
        rule = await self._drafts._rule_refs._find_by_id(
            session, campaign.rule_snapshot_ref_id, job.tenant_id
        )
        coupon = await self._drafts._coupons._find_by_id(
            session, args.coupon_batch_id, job.tenant_id
        )
        publication = await self._drafts._publications._find_by_id(
            session, f"recruitment_{job.job_id.removeprefix('job_')}", job.tenant_id
        )
        if rule is None or coupon is None or publication is None:
            raise PermissionError("persisted Job draft facts are unavailable")
        if any(entity.version != 1 for entity in (campaign, rule, coupon, publication)):
            raise PermissionError("Job reservation requires the original draft versions")
        draft = CampaignDraft(
            campaign=campaign,
            rule_snapshot_ref=rule,
            coupon_batch=coupon,
            recruitment_publication=publication,
            compensation_policy_version=plan.compensation_policy_version,
            campaign_draft_hash=plan.campaign_draft_hash,
            coupon_batch_draft_hash=plan.coupon_batch_draft_hash,
        )
        current_plan = DefaultCampaignLaunchService.build_launch_plan(
            draft=draft,
            materialize_args=args,
            publish_args=PublishRecruitmentArgs(
                campaign_id=campaign.campaign_id,
                recruitment_publication_id=publication.recruitment_publication_id,
                merchant_scope_hash=publication.merchant_scope_hash,
                material_version=publication.material_version,
            ),
        )
        if current_plan != plan:
            raise PermissionError("current draft/child plan no longer matches approval")
        history = await self._tools._find_by_idempotency(
            session, job.tenant_id, "persist_campaign_draft", f"{job.job_id}:campaign-draft:v1"
        )
        if (
            history is None
            or history.status != "succeeded"
            or (history.receipt_summary_hash != draft.campaign_draft_hash)
        ):
            raise PermissionError("the Job has no matching committed draft")

    async def require_launch(
        self, session: AsyncSession, job: Job, args: MaterializeCouponBatchArgs, plan: LaunchPlan
    ) -> str:
        await self.validate_draft(session, job, args, plan)
        saga = await self._sagas._find_by_unique_key(
            session, (job.tenant_id, args.campaign_id), job.tenant_id
        )
        expected = self.launch_saga(job, args)
        if saga != expected:
            raise PermissionError("persisted Job launch approval point changed")
        return expected.checkpoint

    async def reserve(self, session: AsyncSession, reservation: ToolExecution) -> ToolExecution:
        history = await self._tools._find_by_id(
            session, reservation.tenant_id, reservation.execution_id
        )
        if history is not None and (
            history.idempotency_key != reservation.idempotency_key
            or history.canonical_args_hash != reservation.canonical_args_hash
        ):
            raise ValueError("Job coupon reservation content conflicts with history")
        result = await self._tools._reserve_in_session(session, reservation)
        if result.checkpoint_id != reservation.checkpoint_id:
            raise PermissionError("existing coupon reservation checkpoint is not approved")
        if result.status != "reserved":
            raise PermissionError("coupon execution has already left the reservation stage")
        return result

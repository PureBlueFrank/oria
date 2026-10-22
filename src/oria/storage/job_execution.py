"""Fixed local draft/ledger/event transaction used only by the trusted Job boundary."""

import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.context import Context
from oria.core.types import EventEnvelope, JsonValue, ResourceRef
from oria.domain.launch import CampaignDraft
from oria.domain.ledger import DomainEvent, OutboxRecord, ToolExecution
from oria.permission.local import LOCAL_POLICY_VERSION
from oria.storage.ledger import (
    SQLiteBusinessAuditRepository,
    SQLiteDomainEventRepository,
    SQLiteOutboxRepository,
    SQLiteToolExecutionRepository,
)
from oria.storage.repositories import SQLiteCampaignDraftRepository


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

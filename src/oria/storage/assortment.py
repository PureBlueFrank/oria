"""SQLite persistence for the T06 assortment workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.approvals import ApprovalBusinessBinding
from oria.core.execution_ledger import ExecutionOutcome
from oria.domain.assortment import AssortmentSelection, MerchantNotificationMessage
from oria.domain.business import (
    AssortmentSubmission,
    ConsumerPlacement,
    EnrollmentCouponLink,
    EnrollmentItem,
    MerchantNotification,
    SelectionDecision,
)
from oria.storage.repositories import (
    BusinessRepositoryError,
    SQLiteAssortmentSubmissionRepository,
    SQLiteCampaignRepository,
    SQLiteConsumerPlacementRepository,
    SQLiteEnrollmentCouponLinkRepository,
    SQLiteEnrollmentItemRepository,
    SQLiteMerchantNotificationRepository,
    SQLiteSelectionDecisionRepository,
)


class SQLiteAssortmentWorkflowRepository:
    """Keep T06 aggregate changes inside the ledger-owned Business transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._campaigns = SQLiteCampaignRepository(sessions)
        self._submissions = SQLiteAssortmentSubmissionRepository(sessions)
        self._decisions = SQLiteSelectionDecisionRepository(sessions)
        self._items = SQLiteEnrollmentItemRepository(sessions)
        self._links = SQLiteEnrollmentCouponLinkRepository(sessions)
        self._placements = SQLiteConsumerPlacementRepository(sessions)
        self._notifications = SQLiteMerchantNotificationRepository(sessions)

    async def get_approval_binding(
        self, *, tenant_id: str, campaign_id: str
    ) -> ApprovalBusinessBinding | None:
        try:
            async with self._sessions() as session:
                return await self._find_binding(session, tenant_id, campaign_id)
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("approval business binding read failed") from exc

    async def persist_submission_outcome(
        self,
        session: AsyncSession,
        *,
        submission: AssortmentSubmission,
        enrollment_item_ids: tuple[str, ...],
        expected_campaign_version: int,
        outcome: ExecutionOutcome,
    ) -> None:
        expected_status = {
            "succeeded": "submitted",
            "failed": "failed",
            "unknown": "unknown",
        }[outcome]
        campaign = await self._campaigns._find_by_id(
            session, submission.campaign_id, submission.tenant_id
        )
        if (
            campaign is None
            or campaign.version != expected_campaign_version
            or campaign.status not in {"recruiting", "selecting"}
            or submission.status != expected_status
        ):
            raise BusinessRepositoryError("assortment submission state changed before commit")
        existing = await self._submissions._find_by_unique_key(
            session, submission.unique_key(), submission.tenant_id
        )
        if existing is None:
            await self._submissions._insert(session, submission)
        elif existing != submission:
            raise BusinessRepositoryError("assortment submission conflicts with persisted outcome")
        for item_id in enrollment_item_ids:
            await session.execute(
                text(
                    "INSERT INTO assortment_submission_items (tenant_id, campaign_id, "
                    "submission_version, enrollment_item_id, created_at) VALUES (:tenant_id, "
                    ":campaign_id, :submission_version, :item_id, :created_at)"
                ),
                {
                    "tenant_id": submission.tenant_id,
                    "campaign_id": submission.campaign_id,
                    "submission_version": submission.submission_version,
                    "item_id": item_id,
                    "created_at": submission.created_at,
                },
            )

    async def load_submission(
        self, *, tenant_id: str, campaign_id: str, submission_version: str
    ) -> tuple[AssortmentSubmission, tuple[str, ...]]:
        try:
            async with self._sessions() as session:
                submission = await self._submissions._find_by_unique_key(
                    session,
                    (tenant_id, campaign_id, submission_version),
                    tenant_id,
                )
                if submission is None:
                    raise BusinessRepositoryError("assortment submission is unavailable")
                item_ids = await self._submission_item_ids(
                    session, tenant_id, campaign_id, submission_version
                )
                return submission, item_ids
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("assortment submission read failed") from exc

    async def record_selection_decision(
        self,
        session: AsyncSession,
        *,
        decision: SelectionDecision,
    ) -> None:
        submission = await self._submissions._find_by_unique_key(
            session,
            (decision.tenant_id, decision.campaign_id, decision.submission_version),
            decision.tenant_id,
        )
        if submission is None or submission.status != "submitted":
            raise BusinessRepositoryError("assortment submission is not accepting decisions")
        existing = await self._decisions._find_by_unique_key(
            session, decision.unique_key(), decision.tenant_id
        )
        if existing is None:
            await self._decisions._insert(session, decision)
        elif existing != decision:
            raise BusinessRepositoryError("selection decision conflicts with persisted result")

    async def complete_selection(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        campaign_id: str,
        submission_version: str,
        selection_version: str,
        expected_binding: ApprovalBusinessBinding,
        updated_binding: ApprovalBusinessBinding,
        updated_at: datetime,
    ) -> None:
        submission = await self._submissions._find_by_unique_key(
            session,
            (tenant_id, campaign_id, submission_version),
            tenant_id,
        )
        if submission is None or submission.status != "submitted":
            raise BusinessRepositoryError("assortment submission is not completable")
        decision_versions = await session.execute(
            text(
                "SELECT DISTINCT selection_version FROM selection_decisions WHERE tenant_id = "
                ":tenant_id AND campaign_id = :campaign_id AND submission_version = "
                ":submission_version"
            ),
            {
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "submission_version": submission_version,
            },
        )
        if {str(row[0]) for row in decision_versions} != {selection_version}:
            raise BusinessRepositoryError("selection completion version does not match decisions")
        current_binding = await self._find_binding(session, tenant_id, campaign_id)
        if current_binding != expected_binding:
            raise BusinessRepositoryError("selection approval binding optimistic lock conflict")
        completed = submission._next_version(updated_at=updated_at, status="completed")
        await self._submissions._update(session, submission, completed, allow_status_change=True)
        campaign = await self._campaigns._find_by_id(session, campaign_id, tenant_id)
        if campaign is None or campaign.status != "selecting":
            raise BusinessRepositoryError("campaign is not completing selection")
        await self._campaigns._update(
            session,
            campaign,
            campaign.transition_to("pending_consumer_publish", updated_at=updated_at),
            allow_status_change=True,
        )
        await self._write_binding(
            session,
            tenant_id=tenant_id,
            current=expected_binding,
            updated=updated_binding,
        )

    async def load_selection(
        self, *, tenant_id: str, campaign_id: str, selection_version: str
    ) -> AssortmentSelection:
        try:
            async with self._sessions() as session:
                return await self._load_selection(
                    session,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    selection_version=selection_version,
                )
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("selection result read failed") from exc

    async def persist_placement_outcome(
        self,
        session: AsyncSession,
        *,
        placement: ConsumerPlacement,
        expected_binding: ApprovalBusinessBinding,
        selected_item_ids: tuple[str, ...],
        outcome: ExecutionOutcome,
    ) -> None:
        selection = await self._load_selection(
            session,
            tenant_id=placement.tenant_id,
            campaign_id=placement.campaign_id,
            selection_version=placement.selection_version,
        )
        if (
            selection.binding != expected_binding
            or selection.selected_item_ids != selected_item_ids
        ):
            raise BusinessRepositoryError("consumer placement selection binding is stale")
        expected_status = {
            "succeeded": "published",
            "failed": "failed",
            "unknown": "unknown",
        }[outcome]
        if placement.status != expected_status:
            raise BusinessRepositoryError("consumer placement outcome status is invalid")
        existing = await self._placements._find_by_unique_key(
            session, placement.unique_key(), placement.tenant_id
        )
        if existing is None:
            await self._placements._insert(session, placement)
        elif existing != placement:
            raise BusinessRepositoryError("consumer placement conflicts with persisted outcome")
        if outcome == "succeeded":
            campaign = selection.campaign
            if campaign.status != "pending_consumer_publish":
                raise BusinessRepositoryError("campaign is not ready for consumer publication")
            await self._campaigns._update(
                session,
                campaign,
                campaign.transition_to("active", updated_at=placement.updated_at),
                allow_status_change=True,
            )

    async def load_placement(self, *, tenant_id: str, placement_id: str) -> ConsumerPlacement:
        try:
            async with self._sessions() as session:
                placement = await self._placements._find_by_id(session, placement_id, tenant_id)
                if placement is None:
                    raise BusinessRepositoryError("consumer placement is unavailable")
                return placement
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("consumer placement read failed") from exc

    async def notification_message(
        self,
        *,
        tenant_id: str,
        merchant_id: str,
        campaign_id: str,
        result_version: str,
        template_id: str,
        channel: str,
    ) -> MerchantNotificationMessage:
        try:
            async with self._sessions() as session:
                merchant = await session.execute(
                    text(
                        "SELECT 1 FROM merchants WHERE tenant_id = :tenant_id AND merchant_id = "
                        ":merchant_id"
                    ),
                    {"tenant_id": tenant_id, "merchant_id": merchant_id},
                )
                if merchant.one_or_none() is None:
                    raise BusinessRepositoryError("notification merchant is unavailable")
                selection = await self._load_selection(
                    session,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    selection_version=result_version,
                )
                merchant_items = {
                    item.enrollment_item_id
                    for item in selection.items
                    if item.merchant_id == merchant_id
                }
                selected = tuple(
                    sorted(
                        decision.enrollment_item_id
                        for decision in selection.decisions
                        if decision.enrollment_item_id in merchant_items
                        and decision.decision == "selected"
                    )
                )
                rejected = tuple(
                    sorted(
                        cast(str, decision.reason_code)
                        for decision in selection.decisions
                        if decision.enrollment_item_id in merchant_items
                        and decision.decision == "rejected"
                    )
                )
                return MerchantNotificationMessage(
                    merchant_id=merchant_id,
                    campaign_id=campaign_id,
                    result_version=result_version,
                    selected_item_ids=selected,
                    rejected_reasons=rejected,
                    template_id=template_id,
                    channel=channel,
                )
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("notification message read failed") from exc

    async def persist_notification_outcome(
        self,
        session: AsyncSession,
        *,
        notification: MerchantNotification,
    ) -> None:
        existing = await self._notifications._find_by_unique_key(
            session, notification.unique_key(), notification.tenant_id
        )
        if existing is None:
            await self._notifications._insert(session, notification)
        elif existing != notification:
            raise BusinessRepositoryError("merchant notification conflicts with persisted outcome")

    async def load_notification(
        self, *, tenant_id: str, notification_id: str
    ) -> MerchantNotification:
        try:
            async with self._sessions() as session:
                notification = await self._notifications._find_by_id(
                    session, notification_id, tenant_id
                )
                if notification is None:
                    raise BusinessRepositoryError("merchant notification is unavailable")
                return notification
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("merchant notification read failed") from exc

    async def _load_selection(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        campaign_id: str,
        selection_version: str,
    ) -> AssortmentSelection:
        decision_result = await session.execute(
            text(
                "SELECT tenant_id, selection_decision_id, version, created_at, updated_at, "
                "campaign_id, submission_version, selection_version, enrollment_item_id, "
                "decision, reason_code FROM selection_decisions WHERE tenant_id = :tenant_id "
                "AND campaign_id = :campaign_id AND selection_version = :selection_version "
                "ORDER BY enrollment_item_id"
            ),
            {
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "selection_version": selection_version,
            },
        )
        decisions = tuple(self._decisions._from_row(row) for row in decision_result.mappings())
        submission_versions = {decision.submission_version for decision in decisions}
        if len(submission_versions) != 1:
            raise BusinessRepositoryError("selection result has no unique submission")
        submission_version = next(iter(submission_versions))
        submission = await self._submissions._find_by_unique_key(
            session,
            (tenant_id, campaign_id, submission_version),
            tenant_id,
        )
        campaign = await self._campaigns._find_by_id(session, campaign_id, tenant_id)
        binding = await self._find_binding(session, tenant_id, campaign_id)
        if submission is None or campaign is None or binding is None:
            raise BusinessRepositoryError("selection aggregate is unavailable")
        item_ids = await self._submission_item_ids(
            session, tenant_id, campaign_id, submission_version
        )
        items: list[EnrollmentItem] = []
        links: list[EnrollmentCouponLink] = []
        for item_id in item_ids:
            item = await self._items._find_by_id(session, item_id, tenant_id)
            if item is None:
                raise BusinessRepositoryError("selection enrollment item is unavailable")
            items.append(item)
            link_result = await session.execute(
                text(
                    "SELECT tenant_id, enrollment_coupon_link_id, version, created_at, "
                    "updated_at, enrollment_item_id, coupon_batch_id, benefit_tier, status FROM "
                    "enrollment_coupon_links WHERE tenant_id = :tenant_id AND "
                    "enrollment_item_id = :item_id ORDER BY enrollment_coupon_link_id"
                ),
                {"tenant_id": tenant_id, "item_id": item_id},
            )
            links.extend(self._links._from_row(row) for row in link_result.mappings())
        return AssortmentSelection(
            campaign=campaign,
            binding=binding,
            submission=submission,
            enrollment_item_ids=item_ids,
            decisions=decisions,
            items=tuple(items),
            links=tuple(links),
        )

    @staticmethod
    async def _submission_item_ids(
        session: AsyncSession,
        tenant_id: str,
        campaign_id: str,
        submission_version: str,
    ) -> tuple[str, ...]:
        result = await session.execute(
            text(
                "SELECT enrollment_item_id FROM assortment_submission_items WHERE tenant_id = "
                ":tenant_id AND campaign_id = :campaign_id AND submission_version = "
                ":submission_version ORDER BY enrollment_item_id"
            ),
            {
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "submission_version": submission_version,
            },
        )
        return tuple(str(row[0]) for row in result)

    @staticmethod
    async def _find_binding(
        session: AsyncSession,
        tenant_id: str,
        campaign_id: str,
    ) -> ApprovalBusinessBinding | None:
        result = await session.execute(
            text(
                "SELECT campaign_id, enrollment_version, link_version, selection_version, "
                "rule_snapshot_hash FROM campaign_approval_bindings WHERE tenant_id = "
                ":tenant_id AND campaign_id = :campaign_id"
            ),
            {"tenant_id": tenant_id, "campaign_id": campaign_id},
        )
        row = result.mappings().one_or_none()
        return None if row is None else ApprovalBusinessBinding.model_validate(dict(row))

    @staticmethod
    async def _write_binding(
        session: AsyncSession,
        *,
        tenant_id: str,
        current: ApprovalBusinessBinding,
        updated: ApprovalBusinessBinding,
    ) -> None:
        if updated.campaign_id != current.campaign_id:
            raise BusinessRepositoryError("approval business binding campaign does not match")
        result = cast(
            CursorResult[Any],
            await session.execute(
                text(
                    "UPDATE campaign_approval_bindings SET enrollment_version = "
                    ":enrollment_version, link_version = :link_version, selection_version = "
                    ":selection_version, rule_snapshot_hash = :rule_snapshot_hash WHERE "
                    "tenant_id = :tenant_id AND campaign_id = :campaign_id AND "
                    "enrollment_version = :expected_enrollment_version AND link_version = "
                    ":expected_link_version AND selection_version = :expected_selection_version "
                    "AND rule_snapshot_hash = :expected_rule_snapshot_hash"
                ),
                {
                    "tenant_id": tenant_id,
                    **updated.model_dump(),
                    "expected_enrollment_version": current.enrollment_version,
                    "expected_link_version": current.link_version,
                    "expected_selection_version": current.selection_version,
                    "expected_rule_snapshot_hash": current.rule_snapshot_hash,
                },
            ),
        )
        if result.rowcount != 1:
            raise BusinessRepositoryError("approval business binding optimistic lock conflict")

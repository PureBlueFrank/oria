"""SQLite persistence for the T06 assortment workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from oria.core.approvals import ApprovalBusinessBinding
from oria.core.execution_ledger import ExecutionOutcome, ProjectionOutcome
from oria.domain.assortment import (
    AssortmentCandidateSet,
    AssortmentSelection,
    MerchantNotificationMessage,
    selection_result_hash,
)
from oria.domain.business import (
    AssortmentSubmission,
    ConsumerPlacement,
    EnrollmentCouponLink,
    EnrollmentItem,
    MerchantNotification,
    SelectionDecision,
)
from oria.domain.product_eligibility import ProductEligibilityCriteria
from oria.storage.repositories import (
    BusinessRepositoryError,
    SQLiteAssortmentSubmissionRepository,
    SQLiteCampaignRepository,
    SQLiteConsumerPlacementRepository,
    SQLiteEnrollmentCouponLinkRepository,
    SQLiteEnrollmentItemRepository,
    SQLiteEnrollmentWorkflowRepository,
    SQLiteMerchantNotificationRepository,
    SQLiteProductSnapshotRepository,
    SQLiteSelectionDecisionRepository,
)


@dataclass(frozen=True, slots=True)
class _SQLiteAssortmentOutcomeProjection:
    repository: SQLiteAssortmentWorkflowRepository
    tenant_id: str
    execution_id: str
    aggregate_type: str
    aggregate_id: str
    outcome: ProjectionOutcome
    entity: AssortmentSubmission | ConsumerPlacement | MerchantNotification

    async def apply(self, session: AsyncSession) -> None:
        await self.repository._persist_outcome_projection(session, self)


class SQLiteAssortmentWorkflowRepository:
    """Keep T06 aggregate changes inside the ledger-owned Business transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._campaigns = SQLiteCampaignRepository(sessions)
        self._submissions = SQLiteAssortmentSubmissionRepository(sessions)
        self._decisions = SQLiteSelectionDecisionRepository(sessions)
        self._items = SQLiteEnrollmentItemRepository(sessions)
        self._products = SQLiteProductSnapshotRepository(sessions)
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

    async def load_submission_candidates(
        self,
        *,
        tenant_id: str,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        product_criteria: ProductEligibilityCriteria,
        assortment_policy_ref: str,
        assortment_policy_version: str,
        approval_binding: ApprovalBusinessBinding,
    ) -> AssortmentCandidateSet:
        try:
            async with self._sessions() as session:
                return await self._candidate_set(
                    session,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    rule_snapshot_ref_id=rule_snapshot_ref_id,
                    product_criteria=product_criteria,
                    assortment_policy_ref=assortment_policy_ref,
                    assortment_policy_version=assortment_policy_version,
                    approval_binding=approval_binding,
                )
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("assortment candidate read failed") from exc

    async def persist_submission_outcome(
        self,
        session: AsyncSession,
        *,
        submission: AssortmentSubmission,
        enrollment_item_ids: tuple[str, ...],
        candidate_set: AssortmentCandidateSet,
        product_criteria: ProductEligibilityCriteria,
        expected_campaign_version: int,
        outcome: ExecutionOutcome,
    ) -> None:
        if outcome != "succeeded" or submission.status != "submitted":
            raise BusinessRepositoryError(
                "ordinary submission mutation requires a successful execution"
            )
        expected_status = {
            "succeeded": "submitted",
            "failed": "failed",
            "unknown": "unknown",
        }[outcome]
        observed_candidates = await self._candidate_set(
            session,
            tenant_id=submission.tenant_id,
            campaign_id=submission.campaign_id,
            rule_snapshot_ref_id=candidate_set.rule_snapshot_ref_id,
            product_criteria=product_criteria,
            assortment_policy_ref=submission.assortment_policy_ref,
            assortment_policy_version=submission.assortment_policy_version,
            approval_binding=candidate_set.approval_binding,
        )
        if observed_candidates != candidate_set or not set(enrollment_item_ids).issubset(
            observed_candidates.enrollment_item_ids
        ):
            raise BusinessRepositoryError("assortment candidate set changed before commit")
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
        if campaign.status == "recruiting":
            await self._campaigns._update(
                session,
                campaign,
                campaign.transition_to("selecting", updated_at=submission.updated_at),
                allow_status_change=True,
            )

    def submission_outcome_projection(
        self,
        *,
        execution_id: str,
        submission: AssortmentSubmission,
    ) -> _SQLiteAssortmentOutcomeProjection:
        if submission.status not in {"failed", "unknown"}:
            raise ValueError("submission outcome projection cannot contain a success state")
        return _SQLiteAssortmentOutcomeProjection(
            repository=self,
            tenant_id=submission.tenant_id,
            execution_id=execution_id,
            aggregate_type="assortment_submission",
            aggregate_id=submission.submission_version,
            outcome=cast(ProjectionOutcome, submission.status),
            entity=submission,
        )

    async def _candidate_set(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        campaign_id: str,
        rule_snapshot_ref_id: str,
        product_criteria: ProductEligibilityCriteria,
        assortment_policy_ref: str,
        assortment_policy_version: str,
        approval_binding: ApprovalBusinessBinding,
    ) -> AssortmentCandidateSet:
        campaign = await self._campaigns._find_by_id(session, campaign_id, tenant_id)
        current_binding = await self._find_binding(session, tenant_id, campaign_id)
        if (
            campaign is None
            or campaign.rule_snapshot_ref_id != rule_snapshot_ref_id
            or campaign.status not in {"recruiting", "selecting"}
            or current_binding != approval_binding
            or approval_binding.rule_snapshot_hash != product_criteria.rule_snapshot_hash
        ):
            raise BusinessRepositoryError("assortment candidate policy binding is stale")
        result = await session.execute(
            text(
                "SELECT i.enrollment_item_id FROM enrollment_items AS i WHERE i.tenant_id = "
                ":tenant_id AND i.campaign_id = :campaign_id AND i.status = 'confirmed' AND "
                "NOT EXISTS (SELECT 1 FROM confirmation_tasks AS t WHERE t.tenant_id = "
                "i.tenant_id AND t.enrollment_item_id = i.enrollment_item_id AND t.status != "
                "'confirmed') AND EXISTS (SELECT 1 FROM enrollment_coupon_links AS l JOIN "
                "coupon_batches AS c ON c.tenant_id = l.tenant_id AND c.coupon_batch_id = "
                "l.coupon_batch_id WHERE l.tenant_id = i.tenant_id AND l.enrollment_item_id = "
                "i.enrollment_item_id AND l.status = 'active' AND c.campaign_id = :campaign_id "
                "AND c.status = 'ready') ORDER BY i.enrollment_item_id"
            ),
            {"tenant_id": tenant_id, "campaign_id": campaign_id},
        )
        item_ids = tuple(str(row[0]) for row in result)
        if not item_ids:
            raise BusinessRepositoryError("assortment has no eligible server candidates")
        for item_id in item_ids:
            item = await self._items._find_by_id(session, item_id, tenant_id)
            if item is None or item.campaign_id != campaign_id:
                raise BusinessRepositoryError("assortment candidate item is unavailable")
            product = await self._products._find_by_id(session, item.product_snapshot_id, tenant_id)
            if product is None:
                raise BusinessRepositoryError("assortment candidate product is unavailable")
            SQLiteEnrollmentWorkflowRepository._validate_product_bundle(
                product,
                item,
                product_criteria=product_criteria,
                attestation=None,
            )
        return AssortmentCandidateSet.create(
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            rule_snapshot_ref_id=rule_snapshot_ref_id,
            product_criteria=product_criteria,
            assortment_policy_ref=assortment_policy_ref,
            assortment_policy_version=assortment_policy_version,
            approval_binding=approval_binding,
            enrollment_item_ids=item_ids,
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
        membership = await session.scalar(
            text(
                "SELECT 1 FROM assortment_submission_items WHERE tenant_id = :tenant_id AND "
                "campaign_id = :campaign_id AND submission_version = :submission_version AND "
                "enrollment_item_id = :enrollment_item_id"
            ),
            {
                "tenant_id": decision.tenant_id,
                "campaign_id": decision.campaign_id,
                "submission_version": decision.submission_version,
                "enrollment_item_id": decision.enrollment_item_id,
            },
        )
        if membership is None:
            raise BusinessRepositoryError("selection decision item is outside the submission")
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
        selection_hash = await self._validated_selection_hash(
            session,
            tenant_id=tenant_id,
            campaign_id=campaign_id,
            submission_version=submission_version,
            selection_version=selection_version,
        )
        current_binding = await self._find_binding(session, tenant_id, campaign_id)
        if current_binding != expected_binding:
            raise BusinessRepositoryError("selection current binding optimistic lock conflict")
        if updated_binding != expected_binding.model_copy(
            update={
                "selection_version": selection_version,
                "selection_hash": selection_hash,
            }
        ):
            raise BusinessRepositoryError("selection result hash binding conflict")
        completed = submission._next_version(
            updated_at=updated_at,
            status="completed",
            selection_version=selection_version,
            selection_hash=selection_hash,
        )
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

    async def selection_completion_hash(
        self,
        *,
        tenant_id: str,
        campaign_id: str,
        submission_version: str,
        selection_version: str,
    ) -> str:
        try:
            async with self._sessions() as session:
                return await self._validated_selection_hash(
                    session,
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    submission_version=submission_version,
                    selection_version=selection_version,
                )
        except BusinessRepositoryError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise BusinessRepositoryError("selection completion validation failed") from exc

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
        if outcome != "succeeded" or placement.status != "published":
            raise BusinessRepositoryError(
                "ordinary placement mutation requires a successful execution"
            )
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

    def placement_outcome_projection(
        self,
        *,
        execution_id: str,
        placement: ConsumerPlacement,
    ) -> _SQLiteAssortmentOutcomeProjection:
        if placement.status not in {"failed", "unknown"}:
            raise ValueError("placement outcome projection cannot contain a success state")
        if placement.request_id != execution_id:
            raise ValueError("placement outcome projection execution does not match")
        return _SQLiteAssortmentOutcomeProjection(
            repository=self,
            tenant_id=placement.tenant_id,
            execution_id=execution_id,
            aggregate_type="consumer_placement",
            aggregate_id=placement.consumer_placement_id,
            outcome=cast(ProjectionOutcome, placement.status),
            entity=placement,
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
        if notification.status != "sent":
            raise BusinessRepositoryError(
                "ordinary notification mutation requires a successful execution"
            )
        existing = await self._notifications._find_by_unique_key(
            session, notification.unique_key(), notification.tenant_id
        )
        if existing is None:
            await self._notifications._insert(session, notification)
        elif existing != notification:
            raise BusinessRepositoryError("merchant notification conflicts with persisted outcome")

    def notification_outcome_projection(
        self,
        *,
        execution_id: str,
        notification: MerchantNotification,
        outcome: ProjectionOutcome,
    ) -> _SQLiteAssortmentOutcomeProjection:
        if notification.status != "dead_letter":
            raise ValueError("notification outcome projection cannot contain a success state")
        return _SQLiteAssortmentOutcomeProjection(
            repository=self,
            tenant_id=notification.tenant_id,
            execution_id=execution_id,
            aggregate_type="merchant_notification",
            aggregate_id=notification.merchant_notification_id,
            outcome=outcome,
            entity=notification,
        )

    async def _persist_outcome_projection(
        self,
        session: AsyncSession,
        projection: _SQLiteAssortmentOutcomeProjection,
    ) -> None:
        entity = projection.entity
        if isinstance(entity, AssortmentSubmission):
            existing_submission = await self._submissions._find_by_unique_key(
                session, entity.unique_key(), entity.tenant_id
            )
            if existing_submission is None:
                await self._submissions._insert(session, entity)
            elif existing_submission != entity or existing_submission.status not in {
                "failed",
                "unknown",
            }:
                raise BusinessRepositoryError(
                    "submission outcome projection cannot overwrite business success"
                )
            return
        if isinstance(entity, ConsumerPlacement):
            existing_placement = await self._placements._find_by_unique_key(
                session, entity.unique_key(), entity.tenant_id
            )
            if existing_placement is None:
                await self._placements._insert(session, entity)
            elif existing_placement != entity or existing_placement.status not in {
                "failed",
                "unknown",
            }:
                raise BusinessRepositoryError(
                    "placement outcome projection cannot overwrite business success"
                )
            return
        existing_notification = await self._notifications._find_by_unique_key(
            session, entity.unique_key(), entity.tenant_id
        )
        if existing_notification is None:
            await self._notifications._insert(session, entity)
        elif existing_notification != entity or existing_notification.status != "dead_letter":
            raise BusinessRepositoryError(
                "notification outcome projection cannot overwrite business success"
            )

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

    async def _validated_selection_hash(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        campaign_id: str,
        submission_version: str,
        selection_version: str,
    ) -> str:
        item_ids = await self._submission_item_ids(
            session, tenant_id, campaign_id, submission_version
        )
        decision_result = await session.execute(
            text(
                "SELECT tenant_id, selection_decision_id, version, created_at, updated_at, "
                "campaign_id, submission_version, selection_version, enrollment_item_id, "
                "decision, reason_code FROM selection_decisions WHERE tenant_id = :tenant_id "
                "AND campaign_id = :campaign_id AND submission_version = :submission_version "
                "ORDER BY selection_version, enrollment_item_id"
            ),
            {
                "tenant_id": tenant_id,
                "campaign_id": campaign_id,
                "submission_version": submission_version,
            },
        )
        decisions = tuple(self._decisions._from_row(row) for row in decision_result.mappings())
        decision_item_ids = tuple(decision.enrollment_item_id for decision in decisions)
        if not item_ids or not decisions:
            raise BusinessRepositoryError("selection completion requires item decisions")
        if {decision.selection_version for decision in decisions} != {selection_version}:
            raise BusinessRepositoryError("selection completion contains cross-version decisions")
        if len(decision_item_ids) != len(set(decision_item_ids)) or set(decision_item_ids) != set(
            item_ids
        ):
            raise BusinessRepositoryError(
                "selection completion must decide every submitted item exactly once"
            )
        return selection_result_hash(
            campaign_id=campaign_id,
            submission_version=submission_version,
            selection_version=selection_version,
            decisions=decisions,
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
                "selection_hash, rule_snapshot_hash FROM campaign_approval_bindings WHERE "
                "tenant_id = "
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
                    ":selection_version, selection_hash = :selection_hash, rule_snapshot_hash = "
                    ":rule_snapshot_hash WHERE "
                    "tenant_id = :tenant_id AND campaign_id = :campaign_id AND "
                    "enrollment_version = :expected_enrollment_version AND link_version = "
                    ":expected_link_version AND selection_version = :expected_selection_version "
                    "AND selection_hash IS :expected_selection_hash AND rule_snapshot_hash = "
                    ":expected_rule_snapshot_hash"
                ),
                {
                    "tenant_id": tenant_id,
                    **updated.model_dump(),
                    "expected_enrollment_version": current.enrollment_version,
                    "expected_link_version": current.link_version,
                    "expected_selection_version": current.selection_version,
                    "expected_selection_hash": current.selection_hash,
                    "expected_rule_snapshot_hash": current.rule_snapshot_hash,
                },
            ),
        )
        if result.rowcount != 1:
            raise BusinessRepositoryError("approval business binding optimistic lock conflict")

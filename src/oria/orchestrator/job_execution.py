"""Explicit trusted worker boundary for one local campaign draft operation."""

import hashlib
import json

from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from oria.core.job_leases import JobLeaseToken, validate_lease
from oria.core.jobs import CampaignJobSpec, JobConflict
from oria.domain.launch import CampaignDraft, CampaignDraftSpec, build_campaign_draft
from oria.domain.ledger import ToolExecution
from oria.orchestrator.job_leases import JobLeaseService
from oria.rag.models import CampaignRuleSnapshot
from oria.storage.database import DatabaseResources
from oria.storage.job_execution import JobCampaignDraftWriter
from oria.storage.repositories import BusinessRepositoryError


class JobExecutionService:
    """Opt in explicitly; does not execute Graph or authorize any external call."""

    def __init__(self, leases: JobLeaseService, databases: DatabaseResources) -> None:
        if databases.platform_backend != "sqlite" or databases.business_backend != "sqlite":
            raise ValueError("local Job execution fencing currently requires SQLite")
        if leases._repository._sessions is not databases.platform_sessions:
            raise ValueError("Job repository must use the supplied platform sessions")
        platform = databases.platform_sessions.kw["bind"].url.database
        business = databases.business_sessions.kw["bind"].url.database
        if not platform or not business or platform == business:
            raise ValueError("execution fencing requires separate file-backed databases")
        self._leases, self._databases = leases, databases
        self._writer = JobCampaignDraftWriter(databases.business_sessions)

    async def persist_campaign_draft(
        self, token: JobLeaseToken, spec: CampaignDraftSpec, snapshot: CampaignRuleSnapshot
    ) -> CampaignDraft:
        spec = CampaignDraftSpec.model_validate(spec.model_dump())
        snapshot = CampaignRuleSnapshot.model_validate(
            snapshot.internal_payload()
            | {
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.snapshot_hash,
            }
        )
        job, ctx, guard = await self._leases._leased(token, business_resource_id=spec.campaign_id)
        if job.status != "running" or job.cancel_requested:
            raise JobConflict("cancelled execution cannot persist a draft")
        suffix = job.job_id.removeprefix("job_")
        if not isinstance(job.submission.spec, CampaignJobSpec) or (
            spec.campaign_id,
            spec.coupon_batch_id,
            spec.recruitment_publication_id,
        ) != (f"campaign_{suffix}", f"coupon_{suffix}", f"recruitment_{suffix}"):
            raise PermissionError("draft must belong to the campaign Job")
        if snapshot.enrollment_policy.mode != job.submission.spec.enrollment_mode:
            raise ValueError("draft enrollment mode does not match the Job")
        draft = build_campaign_draft(
            spec,
            snapshot,
            tenant_id=ctx.tenant_id,
            now=job.created_at,
            rule_ref_id=f"rule_ref_{suffix}",
        )
        args = json.dumps(
            {
                "spec": spec.model_dump(mode="json"),
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_hash": snapshot.snapshot_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        now = self._leases._now()
        reservation = ToolExecution(
            execution_id=f"{job.job_id}:campaign-draft:v1",
            tenant_id=job.tenant_id,
            tool_name="persist_campaign_draft",
            idempotency_key=f"{job.job_id}:campaign-draft:v1",
            canonical_args_hash=f"sha256:{hashlib.sha256(args.encode()).hexdigest()}",
            checkpoint_id=f"{token.checkpoint_ns}:campaign-draft",
            created_at=now,
            updated_at=now,
        )

        async def persist() -> CampaignDraft:
            async with self._leases._repository._hold_execution_lease(
                job, token, self._leases._now
            ):
                await guard()
                validate_lease(job, token, self._leases._now())
                try:
                    async with self._databases.business_sessions() as session:

                        def before_commit(_: Session) -> None:
                            guard.check_commit()
                            validate_lease(job, token, self._leases._now())

                        event.listen(session.sync_session, "before_commit", before_commit)
                        try:
                            async with session.begin():
                                await self._writer.persist(session, draft, reservation, ctx)
                                await guard()
                                validate_lease(job, token, self._leases._now())
                        finally:
                            event.remove(session.sync_session, "before_commit", before_commit)
                except SQLAlchemyError:
                    raise BusinessRepositoryError("Job draft persistence failed") from None
            return draft

        return await self._leases._write(persist())

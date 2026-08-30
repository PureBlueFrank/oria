"""Rule-driven business confirmation chain generation and timeout handling."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import Field

from oria.core.types import ValueModel
from oria.domain.business import ConfirmationTask, EnrollmentItem
from oria.rag.models import CampaignRuleSnapshot

ConfirmationSubjectType: TypeAlias = Literal["merchant", "sales", "sales_manager"]
TimeoutAction: TypeAlias = Literal["reject", "escalate", "explicit_auto_confirm"]


class BusinessConfirmationPolicy(ValueModel):
    """Confirmation rules copied from one immutable campaign-rule snapshot."""

    rule_snapshot_id: str = Field(pattern=r"^rs_[A-Za-z0-9_-]{24,64}$")
    rule_snapshot_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ordered_steps: tuple[ConfirmationSubjectType, ...]
    timeout_action: TimeoutAction

    @classmethod
    def from_snapshot(cls, snapshot: CampaignRuleSnapshot) -> BusinessConfirmationPolicy:
        if snapshot.recompute_hash() != snapshot.snapshot_hash:
            raise ValueError("campaign rule snapshot integrity verification failed")
        rule = snapshot.confirmation_policy
        return cls(
            rule_snapshot_id=snapshot.snapshot_id,
            rule_snapshot_hash=snapshot.snapshot_hash,
            ordered_steps=rule.ordered_steps,
            timeout_action=rule.timeout_action,
        )

    def generate_tasks(
        self,
        *,
        enrollment_item: EnrollmentItem,
        subject_ids: Mapping[ConfirmationSubjectType, str],
        created_at: datetime,
        due_at: datetime,
    ) -> tuple[ConfirmationTask, ...]:
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise ValueError("due_at must include a timezone")
        tasks: list[ConfirmationTask] = []
        for sequence, subject_type in enumerate(self.ordered_steps, start=1):
            subject_id = subject_ids.get(subject_type)
            if not subject_id:
                raise ValueError(f"confirmation subject is missing for {subject_type}")
            identity = (
                f"{self.rule_snapshot_hash}:{enrollment_item.tenant_id}:"
                f"{enrollment_item.enrollment_item_id}:{sequence}:{subject_type}"
            )
            task_id = "confirmation_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
            tasks.append(
                ConfirmationTask(
                    tenant_id=enrollment_item.tenant_id,
                    confirmation_task_id=task_id,
                    version=1,
                    created_at=created_at,
                    updated_at=created_at,
                    enrollment_item_id=enrollment_item.enrollment_item_id,
                    subject_type=subject_type,
                    subject_id=subject_id,
                    sequence=sequence,
                    due_at=due_at,
                    timeout_action=self.timeout_action,
                    status="pending",
                )
            )
        return tuple(tasks)

    def resolve_timeout(
        self,
        task: ConfirmationTask,
        *,
        updated_at: datetime,
        auto_confirm_authorized: bool = False,
    ) -> ConfirmationTask:
        if task.timeout_action != self.timeout_action:
            raise ValueError("confirmation task timeout action does not match frozen policy")
        if task.status != "pending":
            raise ValueError("only pending confirmation tasks can time out")
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError("updated_at must include a timezone")
        if updated_at < task.updated_at:
            raise ValueError("updated_at must not move backwards")
        if self.timeout_action == "explicit_auto_confirm":
            if not auto_confirm_authorized:
                raise PermissionError("explicit auto confirmation requires policy authorization")
            status = "confirmed"
        elif self.timeout_action == "reject":
            status = "rejected"
        else:
            status = "timed_out"
        return task.model_copy(
            update={"version": task.version + 1, "updated_at": updated_at, "status": status}
        )

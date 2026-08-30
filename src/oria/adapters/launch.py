"""Deterministic in-memory launch adapters; this module never performs network I/O."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from oria.domain.launch import (
    CompensateCouponBatchArgs,
    MaterializeCouponBatchArgs,
    PublishRecruitmentArgs,
)
from oria.domain.ledger import Receipt


class InMemoryCouponBatchAdapter:
    adapter_id = "mock_coupon_batch"

    def __init__(
        self,
        *,
        materialize_status: Literal["accepted", "unknown", "rejected"] = "accepted",
        compensation_status: Literal["accepted", "unknown", "rejected"] = "accepted",
        idempotent_compensation_contract_verified: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.materialize_status = materialize_status
        self.compensation_status = compensation_status
        self.idempotent_compensation_contract_verified = idempotent_compensation_contract_verified
        self._clock = clock
        self.materialize_calls: list[str] = []
        self.compensation_calls: list[str] = []

    async def materialize(
        self,
        args: MaterializeCouponBatchArgs,
        *,
        idempotency_key: str,
    ) -> Receipt:
        self.materialize_calls.append(idempotency_key)
        return self._receipt(
            operation="materialize",
            resource_ref=f"coupon_batch:{args.coupon_batch_id}",
            idempotency_key=idempotency_key,
            status=self.materialize_status,
        )

    async def compensate(
        self,
        args: CompensateCouponBatchArgs,
        *,
        idempotency_key: str,
    ) -> Receipt:
        if not self.idempotent_compensation_contract_verified:
            raise PermissionError("coupon compensation contract is not verified")
        self.compensation_calls.append(idempotency_key)
        return self._receipt(
            operation="compensate",
            resource_ref=f"coupon_batch:{args.coupon_batch_id}",
            idempotency_key=idempotency_key,
            status=self.compensation_status,
        )

    def _receipt(
        self,
        *,
        operation: str,
        resource_ref: str,
        idempotency_key: str,
        status: Literal["accepted", "unknown", "rejected"],
    ) -> Receipt:
        digest = hashlib.sha256(
            f"{self.adapter_id}:{operation}:{idempotency_key}:{status}".encode()
        ).hexdigest()
        return Receipt(
            receipt_id=f"receipt_{digest[:32]}",
            adapter_id=self.adapter_id,
            resource_ref=resource_ref,
            external_id=f"mock_{digest[32:48]}",
            status=status,
            received_at=self._now(),
            summary_hash=f"sha256:{digest}",
        )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("launch adapter clock must return a timezone-aware timestamp")
        return now


class InMemoryRecruitmentAdapter:
    adapter_id = "mock_recruitment"

    def __init__(
        self,
        *,
        status: Literal["accepted", "unknown", "rejected"] = "accepted",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.status = status
        self._clock = clock
        self.publish_calls: list[str] = []

    async def publish(
        self,
        args: PublishRecruitmentArgs,
        *,
        idempotency_key: str,
    ) -> Receipt:
        self.publish_calls.append(idempotency_key)
        digest = hashlib.sha256(
            f"{self.adapter_id}:publish:{idempotency_key}:{self.status}".encode()
        ).hexdigest()
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("launch adapter clock must return a timezone-aware timestamp")
        return Receipt(
            receipt_id=f"receipt_{digest[:32]}",
            adapter_id=self.adapter_id,
            resource_ref=f"recruitment_publication:{args.recruitment_publication_id}",
            external_id=f"mock_{digest[32:48]}",
            status=self.status,
            received_at=now,
            summary_hash=f"sha256:{digest}",
        )

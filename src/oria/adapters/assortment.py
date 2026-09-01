"""Deterministic T06 assortment, placement, and notification adapters."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Literal

from oria.domain.assortment import (
    AssortmentAdapterCapabilities,
    MerchantNotificationMessage,
    NotificationAdapterCapabilities,
    PublishConsumerPlacementArgs,
    SubmitAssortmentArgs,
)
from oria.domain.ledger import Receipt

ReceiptStatus = Literal["accepted", "unknown", "rejected"]


def _receipt(
    *,
    adapter_id: str,
    operation: str,
    resource_ref: str,
    idempotency_key: str,
    status: ReceiptStatus,
    clock: Callable[[], datetime],
) -> Receipt:
    digest = hashlib.sha256(
        f"{adapter_id}:{operation}:{idempotency_key}:{status}".encode()
    ).hexdigest()
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("adapter clock must return a timezone-aware timestamp")
    return Receipt(
        receipt_id=f"receipt_{digest[:32]}",
        adapter_id=adapter_id,
        resource_ref=resource_ref,
        external_id=f"mock_{digest[32:48]}",
        status=status,
        received_at=now,
        summary_hash=f"sha256:{digest}",
    )


class InMemoryAssortmentAdapter:
    adapter_id = "mock_assortment"

    def __init__(
        self,
        *,
        status: ReceiptStatus = "accepted",
        reversible: bool = True,
        max_automatic_items: int = 100,
        preapproved_policy_bindings: frozenset[str] = frozenset(
            {"synthetic-assortment-policy@1.0.0"}
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.status = status
        self.capabilities = AssortmentAdapterCapabilities(
            reversible=reversible,
            max_automatic_items=max_automatic_items,
            preapproved_policy_bindings=preapproved_policy_bindings,
        )
        self._clock = clock
        self.calls: list[str] = []

    async def submit(
        self,
        args: SubmitAssortmentArgs,
        *,
        submission_version: str,
        idempotency_key: str,
    ) -> Receipt:
        self.calls.append(idempotency_key)
        return _receipt(
            adapter_id=self.adapter_id,
            operation="submit",
            resource_ref=f"assortment_submission:{args.campaign_id}:{submission_version}",
            idempotency_key=idempotency_key,
            status=self.status,
            clock=self._clock,
        )


class InMemoryConsumerPlacementAdapter:
    adapter_id = "mock_consumer_placement"

    def __init__(
        self,
        *,
        status: ReceiptStatus = "accepted",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.status = status
        self._clock = clock
        self.calls: list[str] = []

    async def publish(
        self,
        args: PublishConsumerPlacementArgs,
        *,
        selected_item_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> Receipt:
        if not selected_item_ids:
            raise ValueError("consumer placement adapter requires selected items")
        self.calls.append(idempotency_key)
        return _receipt(
            adapter_id=self.adapter_id,
            operation="publish",
            resource_ref=f"consumer_placement:{args.campaign_id}:{args.selection_version}",
            idempotency_key=idempotency_key,
            status=self.status,
            clock=self._clock,
        )


class InMemoryMerchantNotificationAdapter:
    adapter_id = "mock_merchant_notification"

    def __init__(
        self,
        *,
        statuses: Sequence[ReceiptStatus] = ("accepted",),
        standard_template_ids: frozenset[str] = frozenset({"selection-result-v1"}),
        sensitive_template_ids: frozenset[str] = frozenset(),
        max_attempts: int = 3,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not statuses:
            raise ValueError("notification adapter requires at least one outcome")
        self._statuses = tuple(statuses)
        self.capabilities = NotificationAdapterCapabilities(
            standard_template_ids=standard_template_ids,
            sensitive_template_ids=sensitive_template_ids,
            max_attempts=max_attempts,
        )
        self._clock = clock
        self.calls: list[tuple[str, int]] = []

    async def send(
        self,
        message: MerchantNotificationMessage,
        *,
        idempotency_key: str,
        attempt: int,
    ) -> Receipt:
        del message
        self.calls.append((idempotency_key, attempt))
        status = self._statuses[min(attempt - 1, len(self._statuses) - 1)]
        return _receipt(
            adapter_id=self.adapter_id,
            operation=f"send:{attempt}",
            resource_ref="merchant_notification",
            idempotency_key=idempotency_key,
            status=status,
            clock=self._clock,
        )

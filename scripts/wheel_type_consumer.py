"""Minimal downstream typed-package consumer used by the wheel smoke."""

from oria.core.context import Context
from oria.core.types import JsonValue
from oria.domain.models import EligibleMerchantSet
from oria.domain.services import MerchantService


def normalize(value: JsonValue) -> JsonValue:
    return value


payload: JsonValue = {"finite": 1.25, "items": [True, None, "ok"]}
normalized = normalize(payload)


async def select_merchants(service: MerchantService, ctx: Context) -> EligibleMerchantSet:
    return await service.eligible_merchants("demo-east-dining-v1", 10, ctx)

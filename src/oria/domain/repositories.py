"""Repository Protocols owned by the domain layer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from oria.domain.models import MerchantRecord, MerchantSeedSet

if TYPE_CHECKING:
    from oria.core.context import Context


class MerchantRepository(Protocol):
    """Tenant-scoped merchant facts; no arbitrary predicates cross this seam."""

    async def list_for_eligibility(self, ctx: Context) -> tuple[MerchantRecord, ...]: ...

    async def seed(self, seed_set: MerchantSeedSet) -> int: ...

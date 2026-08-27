"""Fixed, typed domain service container exposed to tools through Context."""

from __future__ import annotations

from dataclasses import dataclass

from oria.core.protocols import DomainService


@dataclass(frozen=True, slots=True)
class DomainServiceRegistry:
    """Factory-owned domain seams; it is intentionally not a plugin registry."""

    campaign_rules: DomainService | None = None
    merchants: DomainService | None = None

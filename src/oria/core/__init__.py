"""Core runtime contracts and immutable value types."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oria.core.context import Context, RuntimeServices

__all__ = ["Context", "RuntimeServices"]


def __getattr__(name: str) -> object:
    if name in __all__:
        from oria.core.context import Context, RuntimeServices

        return {"Context": Context, "RuntimeServices": RuntimeServices}[name]
    raise AttributeError(name)

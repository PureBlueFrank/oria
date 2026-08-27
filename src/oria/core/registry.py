"""Small startup-only registries used by the process runtime skeleton."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Generic, TypeVar

T = TypeVar("T")


class RegistrySealedError(RuntimeError):
    """Raised when code tries to mutate a registry after runtime startup."""


class ServiceRegistry(Generic[T]):
    """A registry that becomes read-only before RuntimeServices is returned."""

    def __init__(self) -> None:
        self._items: dict[str, T] | Mapping[str, T] = {}
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def register(self, name: str, service: T) -> None:
        if self._sealed:
            raise RegistrySealedError("runtime registry registration is sealed")
        if not name or name in self._items:
            raise ValueError(f"invalid or duplicate registry name: {name!r}")
        if not isinstance(self._items, dict):
            raise RegistrySealedError("runtime registry registration is sealed")
        self._items[name] = service

    def seal(self) -> None:
        if not self._sealed:
            self._items = MappingProxyType(dict(self._items))
            self._sealed = True

    def get(self, name: str) -> T:
        return self._items[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

"""Small immutable container primitives shared by validated Oria snapshots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Never, SupportsIndex, overload


class FrozenDict(dict[str, Any]):
    """A dict-compatible read-only snapshot that Pydantic serializes as an object."""

    @staticmethod
    def _reject_mutation() -> Never:
        raise TypeError("validated mappings are immutable")

    def __setitem__(self, key: str, value: Any) -> None:
        self._reject_mutation()

    def __delitem__(self, key: str) -> None:
        self._reject_mutation()

    def clear(self) -> None:
        self._reject_mutation()

    def pop(self, key: str, default: Any = None) -> Any:
        self._reject_mutation()

    def popitem(self) -> tuple[str, Any]:
        self._reject_mutation()

    def setdefault(self, key: str, default: Any = None) -> Any:
        self._reject_mutation()

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._reject_mutation()


class FrozenList(list[Any]):
    """A list-compatible read-only snapshot that Pydantic serializes as an array."""

    @staticmethod
    def _reject_mutation() -> Never:
        raise TypeError("validated sequences are immutable")

    @overload
    def __setitem__(self, key: SupportsIndex, value: Any) -> None: ...

    @overload
    def __setitem__(self, key: slice[SupportsIndex | None], value: Iterable[Any]) -> None: ...

    def __setitem__(
        self,
        key: SupportsIndex | slice[SupportsIndex | None],
        value: Any,
    ) -> None:
        self._reject_mutation()

    def __delitem__(self, key: SupportsIndex | slice[SupportsIndex | None]) -> None:
        self._reject_mutation()

    def append(self, value: Any) -> None:
        self._reject_mutation()

    def clear(self) -> None:
        self._reject_mutation()

    def extend(self, values: Iterable[Any]) -> None:
        self._reject_mutation()

    def insert(self, index: SupportsIndex, value: Any) -> None:
        self._reject_mutation()

    def pop(self, index: SupportsIndex = -1) -> Any:
        self._reject_mutation()

    def remove(self, value: Any) -> None:
        self._reject_mutation()

    def reverse(self) -> None:
        self._reject_mutation()

    def sort(self, *, key: Any = None, reverse: bool = False) -> None:
        self._reject_mutation()

    def __imul__(self, value: SupportsIndex) -> Never:
        self._reject_mutation()


def _reject_dict_ior(instance: FrozenDict, other: Any) -> Never:
    raise TypeError("validated mappings are immutable")


def _reject_list_iadd(instance: FrozenList, values: Iterable[Any]) -> Never:
    raise TypeError("validated sequences are immutable")


_DICT_IN_PLACE_UNION = "__i" + "or__"
_LIST_IN_PLACE_ADD = "__i" + "add__"
setattr(FrozenDict, _DICT_IN_PLACE_UNION, _reject_dict_ior)
setattr(FrozenList, _LIST_IN_PLACE_ADD, _reject_list_iadd)


def deep_freeze(value: Any) -> Any:
    """Copy nested builtin containers into recursively immutable equivalents."""
    if isinstance(value, Mapping):
        return FrozenDict({str(key): deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value

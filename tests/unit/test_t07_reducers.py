from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from oria.core.types import NodeResult
from oria.orchestrator.state import (
    StateConflictError,
    merge_results,
    merge_unique,
)

pytestmark = pytest.mark.unit


class Kind(StrEnum):
    PRIMARY = "primary"


class ReducerValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    amount: Decimal
    observed_at: datetime
    kind: Kind


@dataclass(frozen=True)
class DataclassValue:
    amount: Decimal
    observed_at: datetime
    kind: Kind


Reducer = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@pytest.mark.parametrize("reducer", [merge_results, merge_unique])
def test_same_key_with_different_value_raises_conflict_with_key(reducer: Reducer) -> None:
    with pytest.raises(StateConflictError) as caught:
        reducer({"shared": {"value": 1}}, {"shared": {"value": 2}})

    assert caught.value.key == "shared"


def test_merge_results_treats_same_node_result_as_idempotent_replay() -> None:
    original = NodeResult(status="completed", updates={"count": 1})
    replay = NodeResult(status="completed", updates={"count": 1})

    merged = merge_results({"node": original}, {"node": replay})

    assert merged == {"node": original}
    assert merged["node"] is original


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (Decimal("1.50"), Decimal("1.5")),
        (
            datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
            datetime(2026, 9, 1, 16, 0, tzinfo=timezone(timedelta(hours=8))),
        ),
        (Kind.PRIMARY, Kind.PRIMARY),
        (
            ReducerValue(
                amount=Decimal("1.50"),
                observed_at=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
                kind=Kind.PRIMARY,
            ),
            ReducerValue(
                amount=Decimal("1.5"),
                observed_at=datetime(2026, 9, 1, 16, 0, tzinfo=timezone(timedelta(hours=8))),
                kind=Kind.PRIMARY,
            ),
        ),
        (
            DataclassValue(
                amount=Decimal("1.50"),
                observed_at=datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
                kind=Kind.PRIMARY,
            ),
            DataclassValue(
                amount=Decimal("1.5"),
                observed_at=datetime(2026, 9, 1, 16, 0, tzinfo=timezone(timedelta(hours=8))),
                kind=Kind.PRIMARY,
            ),
        ),
    ],
)
def test_merge_unique_canonicalizes_semantically_equal_replays(left: object, right: object) -> None:
    merged = merge_unique({"identity": left}, {"identity": right})

    assert merged["identity"] is left


@pytest.mark.parametrize("reducer", [merge_results, merge_unique])
def test_reducer_is_associative_on_non_conflicting_inputs(reducer: Reducer) -> None:
    a = {"a": {"value": 1}}
    b = {"b": {"value": 2}}
    c = {"c": {"value": 3}}

    assert reducer(reducer(a, b), c) == reducer(a, reducer(b, c))


@pytest.mark.parametrize("reducer", [merge_results, merge_unique])
def test_reducer_does_not_modify_either_input(reducer: Reducer) -> None:
    left = {"left": {"items": [1]}}
    right = {"right": {"items": [2]}}
    left_before = {"left": {"items": [1]}}
    right_before = {"right": {"items": [2]}}

    merged = reducer(left, right)

    assert left == left_before
    assert right == right_before
    assert merged is not left
    assert merged is not right


@pytest.mark.parametrize(
    "invalid",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        float("nan"),
        float("inf"),
        datetime(2026, 9, 1, 8, 0),
    ],
)
@pytest.mark.parametrize("reducer", [merge_results, merge_unique])
def test_reducer_rejects_non_finite_numbers_and_naive_datetimes(
    reducer: Reducer, invalid: object
) -> None:
    with pytest.raises(ValueError):
        reducer({"identity": invalid}, {"identity": invalid})

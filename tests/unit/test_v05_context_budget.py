"""Unit coverage for deterministic V0.5 context budgeting."""

from __future__ import annotations

import json

import pytest

from oria.core.types import Message
from oria.memory import (
    ContextBudget,
    compress_history,
    estimate_message_tokens,
    estimate_tokens,
)

pytestmark = pytest.mark.unit


def test_token_estimator_has_deterministic_boundaries() -> None:
    empty = Message(role="user", content="")
    ascii_message = Message(role="user", content="abcd")
    chinese_message = Message(role="user", content="商家")

    assert estimate_tokens([]) == 0
    assert estimate_message_tokens(empty) == 4
    assert estimate_message_tokens(ascii_message) == 5
    assert estimate_message_tokens(chinese_message) == 6
    assert estimate_tokens([ascii_message, chinese_message]) == 11


def test_token_estimator_rejects_invalid_coefficients() -> None:
    message = Message(role="user", content="test")

    with pytest.raises(ValueError, match="at least 1"):
        estimate_message_tokens(message, chars_per_token=0)
    with pytest.raises(ValueError, match="non-negative"):
        estimate_message_tokens(message, message_overhead_tokens=-1)


def test_context_budget_requires_usable_message_capacity() -> None:
    with pytest.raises(ValueError, match="smaller"):
        ContextBudget(max_context_tokens=100, reserve_tokens=100)


def test_history_within_budget_is_not_compressed() -> None:
    messages = [Message(role="system", content="rules"), Message(role="user", content="hi")]
    budget = ContextBudget(max_context_tokens=100, reserve_tokens=10)

    compressed, ledger, changed = compress_history(messages, budget)

    assert compressed == messages
    assert ledger.entries == ()
    assert changed is False


def test_overflow_compresses_and_preserves_each_fixed_fact() -> None:
    fixed = {
        "merchant_id": "merchant-042",
        "merchant_name": "海棠商行",
        "amount": 128500,
        "conclusion": "经营异常由客单价下降导致",
        "details": "x" * 900,
    }
    messages = [
        Message(role="system", content="Follow the verified evidence."),
        Message(role="tool", tool_call_id="old-result", content=json.dumps(fixed)),
        *[Message(role="assistant", content="older analysis " + "z" * 220) for _ in range(5)],
        Message(role="user", content="Please continue from the latest evidence."),
    ]
    budget = ContextBudget(max_context_tokens=260, reserve_tokens=20)

    compressed, ledger, changed = compress_history(messages, budget)

    facts = {entry.key: entry.value for entry in ledger.entries}
    summary = "\n".join(
        message.content for message in compressed if isinstance(message.content, str)
    )
    assert changed is True
    assert facts["merchant_id"] == "merchant-042"
    assert facts["merchant_name"] == "海棠商行"
    assert facts["amount"] == 128500
    assert facts["conclusion"] == "经营异常由客单价下降导致"
    assert "merchant-042" in summary
    assert "海棠商行" in summary
    assert "128500" in summary
    assert "经营异常由客单价下降导致" in summary
    assert estimate_tokens(compressed) <= budget.message_token_limit


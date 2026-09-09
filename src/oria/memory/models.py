"""Serializable values for session context governance."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from oria.core.types import JsonValue, Message, ValueModel

DEFAULT_MAX_CONTEXT_TOKENS = 32_000
DEFAULT_RESERVE_TOKENS = 8_000
DEFAULT_CHARS_PER_TOKEN = 4
DEFAULT_MESSAGE_OVERHEAD_TOKENS = 4


class ContextBudget(ValueModel):
    """A model-input ceiling with capacity reserved outside message history."""

    max_context_tokens: int = Field(default=DEFAULT_MAX_CONTEXT_TOKENS, ge=2)
    reserve_tokens: int = Field(default=DEFAULT_RESERVE_TOKENS, ge=0)
    chars_per_token: int = Field(default=DEFAULT_CHARS_PER_TOKEN, ge=1)
    message_overhead_tokens: int = Field(
        default=DEFAULT_MESSAGE_OVERHEAD_TOKENS,
        ge=0,
    )

    @model_validator(mode="after")
    def validate_reserve(self) -> ContextBudget:
        if self.reserve_tokens >= self.max_context_tokens:
            raise ValueError("reserve_tokens must be smaller than max_context_tokens")
        return self

    @property
    def message_token_limit(self) -> int:
        """Maximum estimated tokens available to serialized input messages."""

        return self.max_context_tokens - self.reserve_tokens


FactValue: TypeAlias = str | int | float | bool | None


class FactLedgerEntry(ValueModel):
    """One deterministic, ordered fact retained from compressed history."""

    ordinal: int = Field(ge=0)
    source_role: Literal["assistant", "tool"]
    key: str = Field(min_length=1)
    value: FactValue


class FactLedger(ValueModel):
    """Checkpoint-safe ordered facts retained independently of prose summaries."""

    version: Literal[1] = 1
    entries: tuple[FactLedgerEntry, ...] = ()


def _content_text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    payload: list[JsonValue] = [block.model_dump(mode="json") for block in message.content]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def estimate_message_tokens(
    message: Message,
    *,
    chars_per_token: int = DEFAULT_CHARS_PER_TOKEN,
    message_overhead_tokens: int = DEFAULT_MESSAGE_OVERHEAD_TOKENS,
) -> int:
    """Estimate tokens deterministically from UTF-8 bytes with fixed overhead."""

    if chars_per_token < 1:
        raise ValueError("chars_per_token must be at least 1")
    if message_overhead_tokens < 0:
        raise ValueError("message_overhead_tokens must be non-negative")
    byte_count = len(_content_text(message).encode("utf-8"))
    content_tokens = (byte_count + chars_per_token - 1) // chars_per_token
    return content_tokens + message_overhead_tokens


def estimate_tokens(
    messages: Sequence[Message],
    *,
    chars_per_token: int = DEFAULT_CHARS_PER_TOKEN,
    message_overhead_tokens: int = DEFAULT_MESSAGE_OVERHEAD_TOKENS,
) -> int:
    """Return a pure, deterministic estimate for an ordered message sequence."""

    return sum(
        estimate_message_tokens(
            message,
            chars_per_token=chars_per_token,
            message_overhead_tokens=message_overhead_tokens,
        )
        for message in messages
    )

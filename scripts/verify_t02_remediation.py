"""Re-run the independently reproduced V0.1-T02 P0/P1 defect paths."""

from __future__ import annotations

import asyncio
import operator
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import ValidationError

from oria.config import resolve_runtime_config
from oria.core.context import RuntimeSealedError, SealedAsyncExitStack
from oria.core.registry import RegistrySealedError, ServiceRegistry
from oria.core.runtime import build_runtime
from oria.core.types import (
    AuthorizationContext,
    ChatResult,
    JsonValue,
    Message,
    Principal,
    ReasoningDelta,
    TextBlock,
    ToolCall,
    Usage,
)


class _TrackedResource:
    def __init__(self, closed: list[bool]) -> None:
        self._closed = closed

    async def __aenter__(self) -> _TrackedResource:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed.append(True)


def _principal(subject: str, tenant: str, kind: str) -> Principal:
    return Principal(
        subject_id=subject,
        tenant_id=tenant,
        kind=kind,
        roles=("operator",) if kind == "human" else ("runtime",),
        authn_method="remediation-script",
    )


def _require_rejected(operation: Callable[[], Any], error: type[BaseException]) -> None:
    try:
        operation()
    except error:
        return
    raise AssertionError(f"operation was not rejected with {error.__name__}")


async def main() -> None:
    closed: list[bool] = []
    config = resolve_runtime_config(environ={}, data_dir=Path(".artifacts/repro-data"))
    runtime = await build_runtime(
        config,
        resource_factories=(lambda: _TrackedResource(closed),),
    )
    actor_a = _principal("actor-a", "tenant-a", "human")
    executor_a = _principal("executor-a", "tenant-a", "service")
    actor_b = _principal("actor-b", "tenant-b", "human")
    executor_b = _principal("executor-b", "tenant-b", "service")
    context_a = runtime.new_context(
        actor=actor_a,
        executor=executor_a,
        session_id="session-a",
        thread_id="thread-a",
        run_id="run-a",
    )
    context_b = runtime.new_context(
        actor=actor_b,
        executor=executor_b,
        session_id="session-b",
        thread_id="thread-b",
        run_id="run-b",
    )

    _require_rejected(
        lambda: setattr(runtime, "stashed_run_id", context_a.run_id), RuntimeSealedError
    )
    assert context_b.run_id == "run-b" and not hasattr(runtime, "stashed_run_id")
    print("P0-1 runtime execution metadata write: rejected")

    original_tools = runtime.tools
    _require_rejected(lambda: setattr(runtime, "tools", ServiceRegistry()), RuntimeSealedError)
    _require_rejected(lambda: original_tools.register("late", object()), RegistrySealedError)
    assert runtime.tools is original_tools
    print("P0-2 registry replacement and late registration: rejected")

    original_stack = runtime._exit_stack
    _require_rejected(
        lambda: setattr(runtime, "_exit_stack", SealedAsyncExitStack()), RuntimeSealedError
    )
    await runtime.aclose()
    assert original_stack.closed is True and closed == [True]
    print("P0-3 exit-stack replacement: rejected; original stack closed")

    call = ToolCall(id="call-1", name="query", args={"tenant": "safe"})
    auth = AuthorizationContext(correlation_id="corr-1", attributes={})
    message = Message(role="assistant", content=[TextBlock(text="safe")])
    content: Any = message.content
    _require_rejected(lambda: operator.setitem(call.args, "tenant", "HIJACKED"), TypeError)
    _require_rejected(lambda: operator.setitem(auth.attributes, "injected", "x"), TypeError)
    _require_rejected(lambda: content.append(TextBlock(text="tampered")), AttributeError)
    print("P1-2 args, authorization attributes and message content mutation: rejected")

    for value in (float("nan"), float("inf"), float("-inf")):
        _require_rejected(
            lambda candidate=value: ToolCall(
                id="call-nonfinite", name="query", args={"value": candidate}
            ),
            ValidationError,
        )
    finite = ToolCall(id="call-finite", name="query", args={"value": 1.25})
    assert ToolCall.model_validate_json(finite.model_dump_json()) == finite
    print("P1-3 NaN and infinities: rejected; finite float round-trip: accepted")

    reasoning = ReasoningDelta(sequence=1, provider="mock", model="mock", text="internal")
    assert reasoning.internal_text() == "internal"
    assert "internal" not in reasoning.model_dump_json()
    print("P1-4 reasoning default serialization: redacted; internal access: retained")

    result = ChatResult(
        content=[TextBlock(text="visible")],
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1),
        raw_response={"provider": "internal"},
    )
    assert result.internal_raw_response() == {"provider": "internal"}
    assert "raw_response" not in result.model_dump_json()
    print("P1-4 raw response default serialization: redacted; internal access: retained")

    downstream_value: JsonValue = {"installed-consumer": True}
    assert downstream_value == {"installed-consumer": True}
    print("P1-5 JsonValue runtime import: accepted (installed-wheel mypy remains a separate gate)")


if __name__ == "__main__":
    asyncio.run(main())

"""Contract tests for per-execution context isolation under concurrency (V0.1-T02)."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import TracebackType

import pytest

from oria.config import resolve_runtime_config
from oria.config.models import ResolvedRuntimeConfig
from oria.core.context import Context, RuntimeSealedError
from oria.core.runtime import build_runtime
from oria.core.types import Principal

pytestmark = pytest.mark.contract

_INTERLEAVE_ROUNDS = 25


class _ObservableResource:
    """Async resource that appends its own name to a shared list when closed."""

    def __init__(self, name: str, closed: list[str]) -> None:
        self._name = name
        self._closed = closed

    async def __aenter__(self) -> _ObservableResource:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._closed.append(self._name)


def _tracked_factory(name: str, created: list[str], closed: list[str]) -> object:
    def factory() -> _ObservableResource:
        created.append(name)
        return _ObservableResource(name, closed)

    return factory


def _resolve_config(tmp_path: Path) -> ResolvedRuntimeConfig:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    return resolve_runtime_config(
        config_path=config_path,
        environ={},
        data_dir=tmp_path / "data",
    )


def _principal(
    subject_id: str,
    tenant_id: str,
    *,
    kind: str = "human",
    roles: tuple[str, ...] = ("operator",),
) -> Principal:
    return Principal(
        subject_id=subject_id,
        tenant_id=tenant_id,
        kind=kind,
        roles=roles,
        authn_method="test-identity",
    )


def _identity(ctx: Context) -> tuple[object, ...]:
    return (ctx.actor, ctx.executor, ctx.tenant_id, ctx.session_id, ctx.thread_id, ctx.run_id)


@pytest.mark.asyncio
async def test_concurrent_contexts_across_tenants_stay_isolated(tmp_path: Path) -> None:
    """V01-CTX-01: concurrently used tenant contexts never cross-contaminate."""
    process_closed: list[str] = []
    created: list[str] = []
    config = _resolve_config(tmp_path)
    runtime = await build_runtime(
        config,
        resource_factories=(_tracked_factory("process-store", created, process_closed),),
    )
    try:
        actor_a = _principal("actor-a", "tenant-alpha")
        executor_a = _principal("executor-a", "tenant-alpha", kind="service", roles=("runtime",))
        actor_b = _principal("actor-b", "tenant-beta")
        executor_b = _principal("executor-b", "tenant-beta", kind="service", roles=("runtime",))

        ctx_a = runtime.new_context(
            actor=actor_a,
            executor=executor_a,
            session_id="session-a",
            thread_id="thread-a",
            run_id="run-a",
        )
        ctx_b = runtime.new_context(
            actor=actor_b,
            executor=executor_b,
            session_id="session-b",
            thread_id="thread-b",
            run_id="run-b",
        )

        assert ctx_a.actor != ctx_b.actor
        assert ctx_a.executor != ctx_b.executor
        assert ctx_a.tenant_id != ctx_b.tenant_id
        assert ctx_a.run_id != ctx_b.run_id
        assert ctx_a.session_id != ctx_b.session_id
        assert ctx_a.thread_id != ctx_b.thread_id

        identity_a = (actor_a, executor_a, "tenant-alpha", "session-a", "thread-a", "run-a")
        identity_b = (actor_b, executor_b, "tenant-beta", "session-b", "thread-b", "run-b")

        with pytest.raises(FrozenInstanceError):
            ctx_a.run_id = "run-b"
        with pytest.raises(FrozenInstanceError):
            ctx_a.actor = actor_b
        with pytest.raises(RuntimeSealedError):
            runtime.stashed_run_id = ctx_a.run_id

        assert _identity(ctx_a) == identity_a
        assert _identity(ctx_b) == identity_b

        events: list[tuple[str, tuple[object, ...]]] = []
        run_a_local_closed: list[str] = []
        run_b_local_closed: list[str] = []
        run_a_torn_down = asyncio.Event()
        run_b_saw_ready_after_a: list[bool] = []

        async def _interleave(ctx: Context, tag: str, local_closed: list[str]) -> None:
            for _ in range(_INTERLEAVE_ROUNDS):
                await asyncio.sleep(0)
                events.append((tag, _identity(ctx)))
            async with AsyncExitStack() as run_stack:
                await run_stack.enter_async_context(
                    _ObservableResource(f"local-{tag}", local_closed)
                )
                await asyncio.sleep(0)
                events.append((tag, _identity(ctx)))

        async def _run_a() -> None:
            await _interleave(ctx_a, "a", run_a_local_closed)
            run_a_torn_down.set()

        async def _run_b() -> None:
            await _interleave(ctx_b, "b", run_b_local_closed)
            await run_a_torn_down.wait()
            events.append(("b", _identity(ctx_b)))
            run_b_saw_ready_after_a.append(runtime.ready)
            events.append(("b", _identity(ctx_b)))

        await asyncio.gather(_run_a(), _run_b())

        observations_a = [obs for tag, obs in events if tag == "a"]
        observations_b = [obs for tag, obs in events if tag == "b"]
        assert len(observations_a) == _INTERLEAVE_ROUNDS + 1
        assert len(observations_b) == _INTERLEAVE_ROUNDS + 3

        assert all(obs == identity_a for obs in observations_a)
        assert all(obs == identity_b for obs in observations_b)
        assert all(obs != identity_b for obs in observations_a)
        assert all(obs != identity_a for obs in observations_b)

        tags = [tag for tag, _ in events]
        adjacent = [(tags[i], tags[i + 1]) for i in range(len(tags) - 1)]
        assert ("a", "b") in adjacent
        assert ("b", "a") in adjacent

        assert run_a_local_closed == ["local-a"]
        assert run_b_local_closed == ["local-b"]
        assert run_b_saw_ready_after_a == [True]
        assert process_closed == []
        assert runtime.ready is True
        assert _identity(ctx_b) == identity_b

        followup = runtime.new_context(
            actor=actor_b,
            executor=executor_b,
            session_id="session-b-followup",
            thread_id="thread-b-followup",
            run_id="run-b-followup",
        )
        assert followup.run_id == "run-b-followup"
    finally:
        await runtime.aclose()

    assert process_closed == ["process-store"]
    assert runtime.ready is False

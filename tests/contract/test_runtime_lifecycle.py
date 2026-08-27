"""Contract tests for runtime lifecycle and teardown sealing (V0.1-T02)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from types import TracebackType

import pytest

from oria.config import resolve_runtime_config
from oria.config.models import ResolvedRuntimeConfig
from oria.core.context import LifecycleSealedError, SealedAsyncExitStack
from oria.core.registry import RegistrySealedError
from oria.core.runtime import RuntimeResourceFactory, build_runtime
from oria.permission.local import local_cli_executor, local_operator

pytestmark = pytest.mark.contract


class _FactoryFailure(RuntimeError):
    """Sentinel error injected by a failing resource factory."""


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


def _tracked_factory(name: str, created: list[str], closed: list[str]) -> RuntimeResourceFactory:
    def factory() -> _ObservableResource:
        created.append(name)
        return _ObservableResource(name, closed)

    return factory


def _failing_factory(name: str) -> RuntimeResourceFactory:
    def factory() -> AbstractAsyncContextManager[object]:
        raise _FactoryFailure(name)

    return factory


def _resolve_config(tmp_path: Path) -> ResolvedRuntimeConfig:
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    return resolve_runtime_config(
        config_path=config_path,
        environ={},
        data_dir=tmp_path / "data",
    )


@pytest.mark.asyncio
async def test_failed_runtime_build_unwinds_resources_in_reverse_order(tmp_path: Path) -> None:
    """V01-LIFE-01: a mid-startup failure unwinds created resources in strict reverse order."""
    created: list[str] = []
    closed: list[str] = []
    config = _resolve_config(tmp_path)

    with pytest.raises(_FactoryFailure) as excinfo:
        await build_runtime(
            config,
            resource_factories=(
                _tracked_factory("alpha", created, closed),
                _tracked_factory("beta", created, closed),
                _tracked_factory("gamma", created, closed),
                _failing_factory("delta"),
                _tracked_factory("epsilon", created, closed),
            ),
        )

    assert str(excinfo.value) == "delta"
    assert created == ["alpha", "beta", "gamma"]
    assert closed == ["gamma", "beta", "alpha"]
    assert closed == list(reversed(created))
    assert all(closed.count(name) == 1 for name in created)
    assert "delta" not in created
    assert "epsilon" not in created
    assert "delta" not in closed
    assert "epsilon" not in closed


@pytest.mark.asyncio
async def test_ready_runtime_seals_teardown_and_registries(tmp_path: Path) -> None:
    """V01-LIFE-02: once ready, process teardown and registry mutation are permanently sealed."""
    process_closed: list[str] = []
    config = _resolve_config(tmp_path)
    runtime = await build_runtime(
        config,
        resource_factories=(_tracked_factory("process-store", [], process_closed),),
    )
    try:
        assert runtime.ready is True

        exit_stack = runtime._exit_stack
        assert isinstance(exit_stack, SealedAsyncExitStack)
        assert exit_stack.sealed is True
        assert exit_stack.closed is False

        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="session-life-02",
            thread_id="thread-life-02",
            run_id="run-life-02",
        )

        context_members = {name for name in dir(ctx) if not name.startswith("_")}
        assert "exit_stack" not in context_members
        for name in context_members:
            assert getattr(ctx, name) is not exit_stack

        with pytest.raises(LifecycleSealedError):
            await exit_stack.enter_async_context(
                _ObservableResource("late-teardown", process_closed)
            )
        assert "late-teardown" not in process_closed

        registries = (
            runtime.tools,
            runtime.guardrails,
            runtime.nodes,
            runtime.agents,
            runtime.ingress,
            runtime.notifier,
        )
        for registry in registries:
            assert registry.sealed is True
            with pytest.raises(RegistrySealedError):
                registry.register("latecomer", object())

        node_local_closed: list[str] = []
        async with AsyncExitStack() as node_stack:
            await node_stack.enter_async_context(
                _ObservableResource("node-local", node_local_closed)
            )
            assert node_local_closed == []
        assert node_local_closed == ["node-local"]
        assert process_closed == []
        assert runtime.ready is True
    finally:
        await runtime.aclose()

    assert process_closed == ["process-store"]
    assert runtime.ready is False

"""Application-selection and environment-source contracts for attribution ask."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import oria.attribution_demo as attribution_demo

pytestmark = pytest.mark.unit


def test_mock_uses_a_clean_environment_while_live_merges_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> Any:
        calls.append(kwargs)
        provider = "mock" if kwargs["llm_profile"] == "mock" else "deepseek"
        return SimpleNamespace(llm=SimpleNamespace(provider=provider))

    monkeypatch.setattr(attribution_demo, "resolve_runtime_config", capture)
    mock_dir = tmp_path / "mock"
    live_dir = tmp_path / "live"
    mock_dir.mkdir()
    live_dir.mkdir()

    attribution_demo._resolve_config(run_dir=mock_dir, llm_profile=None)
    attribution_demo._resolve_config(run_dir=live_dir, llm_profile="deepseek")

    assert calls[0]["environ"] == {}
    assert calls[0]["config_path"] == mock_dir / "clean-mock-config.yaml"
    assert calls[0]["embedding_profile"] == "fixture"
    assert calls[1]["environ"] is None
    assert calls[1]["llm_profile"] == "deepseek"


def test_explicit_mock_profile_is_not_mislabeled_as_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        attribution_demo,
        "resolve_runtime_config",
        lambda **_: SimpleNamespace(llm=SimpleNamespace(provider="mock")),
    )

    with pytest.raises(attribution_demo.AttributionAskError, match="live_profile_required"):
        attribution_demo._resolve_config(run_dir=tmp_path, llm_profile="mock")

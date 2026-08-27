"""Security boundary tests for runtime configuration resolution (V0.1-T02)."""

from pathlib import Path

import pytest

from oria.config import ConfigResolutionError, resolve_runtime_config

pytestmark = pytest.mark.security


def _write_config(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_production_edition_rejects_demo_runtime_profile(tmp_path: Path) -> None:
    """V01-CFG-03: production edition combined with the demo runtime profile is rejected."""
    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=_write_config(tmp_path, "empty.yaml", "{}\n"),
            runtime_profile="demo",
            environ={"ORIA_EDITION": "production"},
        )
    assert "production edition only permits the standard profile" in str(excinfo.value)


def test_production_edition_rejects_mock_llm(tmp_path: Path) -> None:
    """V01-CFG-03: production edition rejects the MockLLM provider."""
    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=_write_config(tmp_path, "empty.yaml", "{}\n"),
            runtime_profile="standard",
            environ={
                "ORIA_ENVIRONMENT": "test",
                "ORIA_EDITION": "production",
            },
        )
    assert "production edition forbids MockLLM" in str(excinfo.value)


def test_production_edition_rejects_fixture_embedder(tmp_path: Path) -> None:
    """V01-CFG-03: production edition rejects the FixtureEmbedder provider."""
    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=_write_config(tmp_path, "empty.yaml", "{}\n"),
            runtime_profile="standard",
            llm_profile="deepseek",
            environ={
                "ORIA_ENVIRONMENT": "test",
                "ORIA_EDITION": "production",
                "DEEPSEEK_API_KEY": "sk-provider-key-123",
            },
        )
    assert "production edition forbids FixtureEmbedder" in str(excinfo.value)


def test_production_edition_rejects_relative_data_dir(tmp_path: Path) -> None:
    """V01-CFG-05: production edition rejects a relative data_dir."""
    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=_write_config(tmp_path, "empty.yaml", "{}\n"),
            runtime_profile="standard",
            llm_profile="deepseek",
            data_dir=Path("oria-relative-data"),
            environ={
                "ORIA_ENVIRONMENT": "test",
                "ORIA_EDITION": "production",
                "ORIA_EMBEDDING_PROFILE": "bge",
                "DEEPSEEK_API_KEY": "sk-provider-key-123",
                "ORIA_BGE_REVISION": "rev-test-42",
            },
        )
    assert "production edition requires an absolute data_dir" in str(excinfo.value)


def test_resolving_config_does_not_create_or_escape_injected_data_dir(tmp_path: Path) -> None:
    """The non-creation assertion is an extra guard beyond CFG-05's path-boundary contract."""
    empty = _write_config(tmp_path, "empty.yaml", "{}\n")

    resolved = resolve_runtime_config(
        config_path=empty,
        environ={},
        data_dir=tmp_path / "data",
    )
    assert resolved.data_dir == tmp_path / "data"
    assert resolved.data_dir.is_relative_to(tmp_path)
    assert resolved.data_dir != Path.home()
    assert not resolved.data_dir.is_relative_to(Path.home())
    assert not resolved.data_dir.exists()

    paths = resolved.data_paths
    resolved_paths = (
        paths.root,
        paths.platform_db,
        paths.business_db,
        paths.chroma,
        paths.objects,
        paths.reports_tmp,
    )
    for path in resolved_paths:
        assert path.is_relative_to(resolved.data_dir)
        assert not path.is_relative_to(Path.home())
    assert paths.platform_db == tmp_path / "data" / "sqlite" / "platform.db"
    assert paths.business_db == tmp_path / "data" / "sqlite" / "business.db"
    assert paths.chroma == tmp_path / "data" / "chroma"

    resolved_relative = resolve_runtime_config(
        config_path=empty,
        environ={},
        data_dir=Path("oria-relative-data"),
        cwd=tmp_path,
    )
    expected = (tmp_path / "oria-relative-data").resolve(strict=False)
    assert resolved_relative.data_dir == expected
    assert resolved_relative.data_paths.root == expected
    assert resolved_relative.data_dir.is_relative_to(tmp_path.resolve())
    assert not resolved_relative.data_dir.is_relative_to(Path.home())

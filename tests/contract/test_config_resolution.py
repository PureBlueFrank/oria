"""Contract tests for runtime configuration resolution (V0.1-T02)."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from oria.config import ConfigResolutionError, resolve_runtime_config

pytestmark = pytest.mark.contract

_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _write_config(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_community_demo_without_key_resolves_mock_sqlite_chroma(tmp_path: Path) -> None:
    """V01-CFG-01: community+demo without any API key resolves the offline stack."""
    resolved = resolve_runtime_config(
        config_path=_write_config(tmp_path, "empty.yaml", "{}\n"),
        environ={},
        cwd=tmp_path,
    )

    assert resolved.environment == "development"
    assert resolved.edition == "community"
    assert resolved.runtime_profile == "demo"
    assert resolved.llm.profile_id == "mock"
    assert resolved.llm.provider == "mock"
    assert resolved.llm.api_dialect == "mock"
    assert resolved.llm.model == "mock-demo"
    assert resolved.llm.api_key is None
    assert resolved.llm.base_url is None
    assert resolved.embedding.profile_id == "fixture"
    assert resolved.embedding.provider == "fixture"
    assert resolved.storage.vector == "chroma"
    assert resolved.storage.platform_db == "sqlite"
    assert resolved.storage.biz_db == "sqlite"
    assert resolved.storage.cache == "memory"
    assert resolved.storage.object == "local"
    assert _FINGERPRINT_PATTERN.fullmatch(resolved.config_fingerprint) is not None
    assert resolved.public_summary()["llm"]["credential_configured"] is False


def test_community_standard_deepseek_without_key_fails_closed(tmp_path: Path) -> None:
    """V01-CFG-02: community+standard with deepseek but no key fails closed, never falls back."""
    empty = _write_config(tmp_path, "empty.yaml", "{}\n")
    embedding_yaml = _write_config(
        tmp_path,
        "standard-embedding.yaml",
        """\
embedding:
  active_profile: bge
  profiles:
    bge:
      revision: a7ec18349c42fc774b0e86af26215e38a10fbe9d
""",
    )
    keyless_yaml = _write_config(
        tmp_path,
        "keyless-deepseek.yaml",
        """\
llm:
  profiles:
    deepseek:
      api_key: null
embedding:
  active_profile: bge
  profiles:
    bge:
      revision: null
""",
    )

    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=empty,
            runtime_profile="standard",
            llm_profile="deepseek",
            environ={},
        )
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)

    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=keyless_yaml,
            runtime_profile="standard",
            llm_profile="deepseek",
            environ={},
        )
    assert "requires an API key" in str(excinfo.value)

    resolved = resolve_runtime_config(
        config_path=embedding_yaml,
        runtime_profile="standard",
        llm_profile="deepseek",
        environ={"DEEPSEEK_API_KEY": "sk-provider-key-123"},
        cwd=tmp_path,
    )
    assert resolved.llm.provider == "deepseek"
    assert resolved.llm.model == "deepseek-v4-flash"
    assert resolved.llm.api_key is not None
    assert resolved.llm.structured_output_mode == "native_json_schema"
    assert resolved.embedding.revision == "a7ec18349c42fc774b0e86af26215e38a10fbe9d"


def test_resolved_config_rejects_mutation(tmp_path: Path) -> None:
    """V01-CFG-04: resolved runtime config and its nested models are read-only."""
    resolved = resolve_runtime_config(
        config_path=_write_config(tmp_path, "empty.yaml", "{}\n"),
        environ={},
        data_dir=tmp_path / "data",
    )

    with pytest.raises(ValidationError):
        resolved.environment = "production"
    with pytest.raises(ValidationError):
        resolved.runtime_profile = "standard"
    with pytest.raises(ValidationError):
        resolved.llm.model = "mutated-model"
    with pytest.raises(ValidationError):
        resolved.storage.vector = "milvus"

    assert resolved.environment == "development"
    assert resolved.runtime_profile == "demo"
    assert resolved.llm.model == "mock-demo"
    assert resolved.storage.vector == "chroma"


def test_invalid_references_profiles_and_yaml_roots_are_rejected(tmp_path: Path) -> None:
    """V01-CFG-04: semantic source errors fail; same-field sources use declared precedence."""
    empty = _write_config(tmp_path, "empty.yaml", "{}\n")
    partial_ref_yaml = _write_config(
        tmp_path,
        "partial-ref.yaml",
        """\
llm:
  active_profile: deepseek
  profiles:
    deepseek:
      api_key: "sk-${DEEPSEEK_API_KEY}"
""",
    )
    non_mapping_yaml = _write_config(
        tmp_path,
        "non-mapping.yaml",
        "- development\n- community\n",
    )

    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=partial_ref_yaml,
            environ={"DEEPSEEK_API_KEY": "sk-provider-key-123"},
        )
    assert "environment references must occupy the entire value" in str(excinfo.value)

    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(
            config_path=empty,
            llm_profile="does-not-exist",
            environ={},
        )
    assert "unknown LLM profile" in str(excinfo.value)

    with pytest.raises(ConfigResolutionError) as excinfo:
        resolve_runtime_config(config_path=non_mapping_yaml, environ={})
    assert "string-keyed mapping" in str(excinfo.value)


def test_cli_environment_and_yaml_use_declared_precedence(tmp_path: Path) -> None:
    """V01-CFG-04: CLI overrides environment, which overrides YAML for the same field."""
    config = _write_config(tmp_path, "precedence.yaml", "runtime_profile: demo\n")

    resolved = resolve_runtime_config(
        config_path=config,
        runtime_profile="demo",
        environ={"ORIA_RUNTIME_PROFILE": "standard"},
        cwd=tmp_path,
    )

    assert resolved.runtime_profile == "demo"


@pytest.mark.parametrize(
    ("provider", "dialect"),
    [("mock", "responses"), ("deepseek", "mock")],
)
def test_provider_and_api_dialect_must_match(tmp_path: Path, provider: str, dialect: str) -> None:
    config = _write_config(
        tmp_path,
        "provider-dialect.yaml",
        f"""\
llm:
  profiles:
    mock:
      provider: {provider}
      api_dialect: {dialect}
      api_key: null
""",
    )

    with pytest.raises(ConfigResolutionError, match="api_dialect"):
        resolve_runtime_config(config_path=config, environ={}, cwd=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("log_level", "VERBOSE"),
        ("telemetry.log_exporter", "unknown"),
        ("telemetry.trace_exporter", "unknown"),
        ("telemetry.metric_exporter", "unknown"),
        ("im.default", "unknown"),
    ],
)
def test_runtime_enums_reject_unknown_consumer_values(
    tmp_path: Path, field: str, value: str
) -> None:
    parts = field.split(".")
    body = f"{parts[0]}: {value}\n" if len(parts) == 1 else f"{parts[0]}:\n  {parts[1]}: {value}\n"

    with pytest.raises(ConfigResolutionError):
        resolve_runtime_config(
            config_path=_write_config(tmp_path, f"invalid-{field}.yaml", body),
            environ={},
            cwd=tmp_path,
        )


def test_resolved_im_config_preserves_channels_without_publicly_dumping_secrets(
    tmp_path: Path,
) -> None:
    secret = "im-secret-value"
    config = _write_config(
        tmp_path,
        "im.yaml",
        f"""\
im:
  default: feishu
  channels:
    feishu:
      app_id: app-1
      app_secret: {secret}
""",
    )

    resolved = resolve_runtime_config(config_path=config, environ={}, cwd=tmp_path)

    assert resolved.im.channels["feishu"].app_id == "app-1"
    assert resolved.im.channels["feishu"].app_secret is not None
    assert resolved.im.channels["feishu"].app_secret.get_secret_value() == secret
    assert secret not in json.dumps(resolved.public_summary())


def test_selected_non_mock_im_channel_requires_a_matching_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigResolutionError, match="selected IM channel"):
        resolve_runtime_config(
            config_path=_write_config(tmp_path, "missing-im.yaml", "im:\n  default: feishu\n"),
            environ={},
            cwd=tmp_path,
        )


def test_fingerprint_and_public_projection_exclude_secrets(tmp_path: Path) -> None:
    """V01-CFG-04: config_fingerprint and public_summary never expose the API key."""
    empty = _write_config(tmp_path, "empty.yaml", "{}\n")
    real_key = "sk-live-7f3a91c4e2b84d0f9a6c"

    with_key = resolve_runtime_config(
        config_path=empty,
        environ={"DEEPSEEK_API_KEY": real_key},
        llm_profile="deepseek",
        data_dir=tmp_path / "data",
    )
    other_key = resolve_runtime_config(
        config_path=empty,
        environ={"DEEPSEEK_API_KEY": "sk-live-0000000000000000000000"},
        llm_profile="deepseek",
        data_dir=tmp_path / "data",
    )
    without_key = resolve_runtime_config(
        config_path=empty,
        environ={},
        data_dir=tmp_path / "data",
    )

    assert with_key.llm.api_key is not None
    assert with_key.llm.api_key.get_secret_value() == real_key
    assert with_key.public_summary()["llm"]["credential_configured"] is True

    assert _FINGERPRINT_PATTERN.fullmatch(with_key.config_fingerprint) is not None
    assert real_key not in with_key.config_fingerprint
    assert with_key.config_fingerprint == other_key.config_fingerprint
    assert with_key.config_fingerprint != without_key.config_fingerprint

    summary_text = json.dumps(with_key.public_summary(), ensure_ascii=False, sort_keys=True)
    assert real_key not in summary_text

    assert real_key not in repr(with_key)
    assert real_key not in str(with_key)
    assert real_key not in repr(with_key.llm)
    assert real_key not in str(with_key.llm)
    assert real_key not in str(with_key.llm.api_key)

"""Resolve YAML, environment and CLI configuration into one frozen snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from oria.config.models import (
    ConfigResolutionError,
    EmbeddingProfileConfig,
    LLMProfileConfig,
    ResolvedEmbeddingProfile,
    ResolvedIMChannelConfig,
    ResolvedIMConfig,
    ResolvedLLMProfile,
    ResolvedRuntimeConfig,
    ResolvedStorageConfig,
    ResolvedTelemetryConfig,
    RuntimeConfig,
)

_ENV_REFERENCE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


class RuntimeEnvironmentSettings(BaseSettings):
    """ORIA-prefixed environment overrides loaded without dotenv files."""

    model_config = SettingsConfigDict(env_prefix="ORIA_", env_file=None, extra="ignore")

    environment: str | None = None
    edition: str | None = None
    runtime_profile: str | None = None
    llm_profile: str | None = None
    embedding_profile: str | None = None
    data_dir: Path | None = None
    log_level: str | None = None


def _defaults() -> dict[str, Any]:
    return {
        "environment": "development",
        "edition": "community",
        "runtime_profile": "demo",
        "llm": {
            "active_profile": "mock",
            "profiles": {
                "mock": {
                    "provider": "mock",
                    "api_dialect": "mock",
                    "model": "mock-demo",
                    "structured_output_mode": "native_json_schema",
                },
                "deepseek": {
                    "provider": "deepseek",
                    "api_dialect": "responses",
                    "model": "deepseek-v4-flash",
                    "api_key": "${DEEPSEEK_API_KEY}",
                    "base_url": "https://api.deepseek.com",
                    "structured_output_mode": "native_json_schema",
                },
                "kimi": {
                    "provider": "kimi",
                    "api_dialect": "chat_completions",
                    "model": "${MOONSHOT_MODEL}",
                    "api_key": "${MOONSHOT_API_KEY}",
                    "base_url": "https://api.moonshot.cn/v1",
                    "structured_output_mode": "synthetic_tool",
                },
                "zhipu": {
                    "provider": "zhipu",
                    "api_dialect": "chat_completions",
                    "model": "${ZHIPU_MODEL}",
                    "api_key": "${ZHIPU_API_KEY}",
                    "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "structured_output_mode": "synthetic_tool",
                },
                "openai": {
                    "provider": "openai",
                    "api_dialect": "chat_completions",
                    "model": "${OPENAI_MODEL}",
                    "api_key": "${OPENAI_API_KEY}",
                    "base_url": "https://api.openai.com/v1",
                    "structured_output_mode": "native_json_schema",
                },
                "anthropic": {
                    "provider": "anthropic",
                    "api_dialect": "anthropic_messages",
                    "model": "${ANTHROPIC_MODEL}",
                    "api_key": "${ANTHROPIC_API_KEY}",
                    "base_url": "https://api.anthropic.com",
                    "structured_output_mode": "native_json_schema",
                },
            },
        },
        "embedding": {
            "active_profile": "fixture",
            "profiles": {
                "fixture": {"provider": "fixture"},
                "bge": {
                    "provider": "sentence_transformers",
                    "model": "BAAI/bge-small-zh-v1.5",
                    "revision": "a7ec18349c42fc774b0e86af26215e38a10fbe9d",
                    "trust_remote_code": False,
                },
            },
        },
        "im": {"default": "mock", "channels": {}},
        "log_level": "INFO",
        "data_dir": ".oria-data",
        "storage": {
            "vector": "chroma",
            "platform_db": "sqlite",
            "biz_db": "sqlite",
            "cache": "memory",
            "object": "local",
        },
        "telemetry": {
            "log_exporter": "console_json",
            "trace_exporter": "console",
            "metric_exporter": "console",
            "capture_content": False,
        },
    }


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _load_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigResolutionError(f"configuration file does not exist: {path}")
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigResolutionError(f"cannot read configuration file: {path}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict) or any(not isinstance(key, str) for key in loaded):
        raise ConfigResolutionError("configuration root must be a string-keyed mapping")
    return loaded


def _environment_overrides(environ: Mapping[str, str] | None) -> dict[str, Any]:
    if environ is None:
        values = RuntimeEnvironmentSettings().model_dump(exclude_none=True)
    else:
        fields = {
            "ORIA_ENVIRONMENT": "environment",
            "ORIA_EDITION": "edition",
            "ORIA_RUNTIME_PROFILE": "runtime_profile",
            "ORIA_DATA_DIR": "data_dir",
            "ORIA_LOG_LEVEL": "log_level",
        }
        values = {field: environ[key] for key, field in fields.items() if key in environ}
        if "ORIA_LLM_PROFILE" in environ:
            values["llm_profile"] = environ["ORIA_LLM_PROFILE"]
        if "ORIA_EMBEDDING_PROFILE" in environ:
            values["embedding_profile"] = environ["ORIA_EMBEDDING_PROFILE"]

    llm_profile = values.pop("llm_profile", None)
    embedding_profile = values.pop("embedding_profile", None)
    if llm_profile is not None:
        values["llm"] = {"active_profile": llm_profile}
    if embedding_profile is not None:
        values["embedding"] = {"active_profile": embedding_profile}
    if "data_dir" in values:
        values["data_dir"] = str(values["data_dir"])
    return values


def _expand(value: str | None, environ: Mapping[str, str]) -> str | None:
    if value is None:
        return None
    match = _ENV_REFERENCE.fullmatch(value)
    if match is None:
        if "${" in value:
            raise ConfigResolutionError("environment references must occupy the entire value")
        return value
    variable = match.group(1)
    resolved = environ.get(variable)
    if not resolved:
        raise ConfigResolutionError(f"active profile requires environment variable {variable}")
    return resolved


def _resolve_llm(
    profile_id: str, profile: LLMProfileConfig, environ: Mapping[str, str]
) -> ResolvedLLMProfile:
    secret = profile.api_key.get_secret_value() if profile.api_key is not None else None
    resolved_secret = _expand(secret, environ)
    model = _expand(profile.model, environ)
    if model is None:
        raise ConfigResolutionError(f"LLM profile {profile_id!r} requires a model")
    return ResolvedLLMProfile(
        profile_id=profile_id,
        provider=profile.provider,
        api_dialect=profile.api_dialect,
        model=model,
        api_key=SecretStr(resolved_secret) if resolved_secret is not None else None,
        base_url=_expand(profile.base_url, environ),
        structured_output_mode=profile.structured_output_mode,
    )


def _resolve_embedding(
    profile_id: str, profile: EmbeddingProfileConfig, environ: Mapping[str, str]
) -> ResolvedEmbeddingProfile:
    return ResolvedEmbeddingProfile(
        profile_id=profile_id,
        provider=profile.provider,
        model=_expand(profile.model, environ),
        revision=_expand(profile.revision, environ),
        trust_remote_code=profile.trust_remote_code,
    )


def _validate_matrix(
    config: RuntimeConfig,
    llm: ResolvedLLMProfile,
    embedding: ResolvedEmbeddingProfile,
    original_data_dir: Path,
) -> None:
    is_test = config.environment == "test"
    if config.runtime_profile == "standard" and not is_test:
        if llm.provider == "mock":
            raise ConfigResolutionError("standard profile requires a non-mock LLM")
        if embedding.provider == "fixture":
            raise ConfigResolutionError("standard profile requires a non-fixture embedder")
    if llm.provider != "mock" and llm.api_key is None:
        raise ConfigResolutionError(f"LLM profile {llm.profile_id!r} requires an API key")
    if llm.provider != "mock" and llm.base_url is None:
        raise ConfigResolutionError(f"LLM profile {llm.profile_id!r} requires a base_url")
    if llm.provider == "deepseek" and (
        llm.api_dialect != "responses"
        or llm.model != "deepseek-v4-flash"
        or llm.structured_output_mode != "native_json_schema"
    ):
        raise ConfigResolutionError(
            "DeepSeek requires responses dialect, deepseek-v4-flash, and native JSON schema"
        )
    if embedding.provider == "sentence_transformers":
        if embedding.model is None or embedding.revision is None:
            raise ConfigResolutionError(
                "sentence-transformers embedder requires model and revision"
            )
        if embedding.trust_remote_code:
            raise ConfigResolutionError("sentence-transformers embedder forbids remote code")

    if config.edition != "production":
        return
    if config.runtime_profile != "standard":
        raise ConfigResolutionError("production edition only permits the standard profile")
    if llm.provider == "mock":
        raise ConfigResolutionError("production edition forbids MockLLM")
    if embedding.provider == "fixture":
        raise ConfigResolutionError("production edition forbids FixtureEmbedder")
    if not original_data_dir.is_absolute():
        raise ConfigResolutionError("production edition requires an absolute data_dir")
    expected = {
        "vector": {"milvus"},
        "platform_db": {"postgres"},
        "biz_db": {"dms", "mysql"},
        "cache": {"redis"},
        "object": {"s3", "minio"},
    }
    actual = config.storage.model_dump()
    invalid = [name for name, allowed in expected.items() if actual[name] not in allowed]
    if invalid:
        raise ConfigResolutionError(
            "production edition requires production storage backends: " + ", ".join(invalid)
        )


def _fingerprint_payload(
    config: RuntimeConfig,
    llm: ResolvedLLMProfile,
    embedding: ResolvedEmbeddingProfile,
    data_dir: Path,
) -> dict[str, Any]:
    return {
        "environment": config.environment,
        "edition": config.edition,
        "runtime_profile": config.runtime_profile,
        "llm": {
            "profile_id": llm.profile_id,
            "provider": llm.provider,
            "api_dialect": llm.api_dialect,
            "model": llm.model,
            "base_url": llm.base_url,
            "structured_output_mode": llm.structured_output_mode,
        },
        "embedding": embedding.model_dump(mode="json"),
        "im": {
            "default": config.im.default,
            "channels": {
                name: {
                    "app_id": channel.app_id,
                    "credential_configured": any(
                        credential is not None
                        for credential in (channel.webhook, channel.app_secret, channel.secret)
                    ),
                }
                for name, channel in sorted(config.im.channels.items())
            },
        },
        "log_level": config.log_level,
        "data_dir": str(data_dir),
        "storage": config.storage.model_dump(mode="json"),
        "telemetry": config.telemetry.model_dump(mode="json"),
    }


def resolve_runtime_config(
    *,
    config_path: Path | None = None,
    runtime_profile: str | None = None,
    llm_profile: str | None = None,
    embedding_profile: str | None = None,
    data_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> ResolvedRuntimeConfig:
    """Resolve sources once using CLI > environment > YAML > defaults precedence."""
    env = os.environ if environ is None else environ
    default_path = Path.home() / ".oria" / "config.yaml"
    selected_path = default_path if config_path is None else config_path
    merged = _deep_merge(_defaults(), _load_yaml(selected_path, required=config_path is not None))
    merged = _deep_merge(merged, _environment_overrides(environ))

    cli_overrides: dict[str, Any] = {}
    if runtime_profile is not None:
        cli_overrides["runtime_profile"] = runtime_profile
    if llm_profile is not None:
        cli_overrides["llm"] = {"active_profile": llm_profile}
    if embedding_profile is not None:
        cli_overrides["embedding"] = {"active_profile": embedding_profile}
    if data_dir is not None:
        cli_overrides["data_dir"] = str(data_dir)
    merged = _deep_merge(merged, cli_overrides)

    try:
        parsed = RuntimeConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigResolutionError(str(exc)) from exc

    try:
        active_llm = parsed.llm.profiles[parsed.llm.active_profile]
    except KeyError as exc:
        raise ConfigResolutionError(f"unknown LLM profile: {parsed.llm.active_profile}") from exc
    try:
        active_embedding = parsed.embedding.profiles[parsed.embedding.active_profile]
    except KeyError as exc:
        raise ConfigResolutionError(
            f"unknown embedding profile: {parsed.embedding.active_profile}"
        ) from exc

    llm = _resolve_llm(parsed.llm.active_profile, active_llm, env)
    embedding = _resolve_embedding(parsed.embedding.active_profile, active_embedding, env)
    original_data_dir = parsed.data_dir
    _validate_matrix(parsed, llm, embedding, original_data_dir)

    base_dir = Path.cwd() if cwd is None else cwd
    absolute_data_dir = (
        original_data_dir
        if original_data_dir.is_absolute()
        else (base_dir / original_data_dir).resolve(strict=False)
    )
    payload = _fingerprint_payload(parsed, llm, embedding, absolute_data_dir)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    return ResolvedRuntimeConfig(
        environment=parsed.environment,
        edition=parsed.edition,
        runtime_profile=parsed.runtime_profile,
        llm=llm,
        embedding=embedding,
        im=ResolvedIMConfig(
            default=parsed.im.default,
            channels={
                name: ResolvedIMChannelConfig(**channel.model_dump())
                for name, channel in parsed.im.channels.items()
            },
        ),
        log_level=parsed.log_level,
        data_dir=absolute_data_dir,
        storage=ResolvedStorageConfig(**parsed.storage.model_dump()),
        telemetry=ResolvedTelemetryConfig(**parsed.telemetry.model_dump()),
        config_fingerprint=fingerprint,
    )

"""Input and resolved runtime configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from oria._internal.immutable import FrozenDict

LLMProviderName = Literal["mock", "deepseek", "kimi", "zhipu", "openai", "anthropic"]
APIDialect = Literal["mock", "chat_completions", "responses", "anthropic_messages"]
IMChannelName = Literal["mock", "daxiang", "feishu", "dingtalk"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_PROVIDER_DIALECTS: dict[str, frozenset[str]] = {
    "mock": frozenset({"mock"}),
    "deepseek": frozenset({"responses"}),
    "kimi": frozenset({"chat_completions"}),
    "zhipu": frozenset({"chat_completions"}),
    "openai": frozenset({"chat_completions", "responses"}),
    "anthropic": frozenset({"anthropic_messages"}),
}


class ConfigResolutionError(ValueError):
    """Raised when runtime configuration cannot be safely resolved."""


class InputConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMProfileConfig(InputConfigModel):
    provider: LLMProviderName
    api_dialect: APIDialect
    model: str
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = None

    @model_validator(mode="after")
    def validate_provider_dialect(self) -> LLMProfileConfig:
        if self.api_dialect not in _PROVIDER_DIALECTS[self.provider]:
            raise ValueError(
                f"provider {self.provider!r} is incompatible with api_dialect {self.api_dialect!r}"
            )
        return self


class LLMConfig(InputConfigModel):
    active_profile: str
    profiles: dict[str, LLMProfileConfig]


class EmbeddingProfileConfig(InputConfigModel):
    provider: str
    model: str | None = None
    revision: str | None = None
    trust_remote_code: bool = False


class EmbeddingConfig(InputConfigModel):
    active_profile: str
    profiles: dict[str, EmbeddingProfileConfig]


class IMChannelConfig(InputConfigModel):
    webhook: SecretStr | None = Field(default=None, repr=False)
    app_id: str | None = None
    app_secret: SecretStr | None = Field(default=None, repr=False)
    secret: SecretStr | None = Field(default=None, repr=False)


class IMConfig(InputConfigModel):
    default: IMChannelName
    channels: dict[str, IMChannelConfig]

    @model_validator(mode="after")
    def validate_selected_channel_exists(self) -> IMConfig:
        if self.default != "mock" and self.default not in self.channels:
            raise ValueError(f"selected IM channel {self.default!r} is not configured")
        return self


class StorageConfig(InputConfigModel):
    vector: str
    platform_db: str
    biz_db: str
    cache: str
    object: str


class TelemetryConfig(InputConfigModel):
    log_exporter: Literal["console_json", "file_json", "enterprise"]
    trace_exporter: Literal["console", "otlp", "langfuse"]
    metric_exporter: Literal["console", "otlp"]
    capture_content: bool = False


class RuntimeConfig(InputConfigModel):
    environment: Literal["development", "test", "production"]
    edition: Literal["community", "production"]
    runtime_profile: Literal["demo", "standard"]
    llm: LLMConfig
    embedding: EmbeddingConfig
    im: IMConfig
    log_level: LogLevel
    data_dir: Path
    storage: StorageConfig
    telemetry: TelemetryConfig


class ResolvedConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolvedLLMProfile(ResolvedConfigModel):
    profile_id: str
    provider: LLMProviderName
    api_dialect: APIDialect
    model: str
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = None


class ResolvedEmbeddingProfile(ResolvedConfigModel):
    profile_id: str
    provider: str
    model: str | None = None
    revision: str | None = None
    trust_remote_code: bool = False


class ResolvedIMChannelConfig(ResolvedConfigModel):
    webhook: SecretStr | None = Field(default=None, repr=False)
    app_id: str | None = None
    app_secret: SecretStr | None = Field(default=None, repr=False)
    secret: SecretStr | None = Field(default=None, repr=False)


class ResolvedIMConfig(ResolvedConfigModel):
    default: IMChannelName
    channels: dict[str, ResolvedIMChannelConfig]

    @model_validator(mode="after")
    def freeze_channels(self) -> ResolvedIMConfig:
        object.__setattr__(self, "channels", FrozenDict(dict(self.channels)))
        return self


class ResolvedStorageConfig(ResolvedConfigModel):
    vector: str
    platform_db: str
    biz_db: str
    cache: str
    object: str


class ResolvedTelemetryConfig(ResolvedConfigModel):
    log_exporter: Literal["console_json", "file_json", "enterprise"]
    trace_exporter: Literal["console", "otlp", "langfuse"]
    metric_exporter: Literal["console", "otlp"]
    capture_content: bool


class RuntimeDataPaths(ResolvedConfigModel):
    root: Path
    platform_db: Path
    business_db: Path
    chroma: Path
    objects: Path
    reports_tmp: Path


class ResolvedRuntimeConfig(ResolvedConfigModel):
    """Fully validated startup snapshot; runtime code must not reread its sources."""

    environment: Literal["development", "test", "production"]
    edition: Literal["community", "production"]
    runtime_profile: Literal["demo", "standard"]
    llm: ResolvedLLMProfile
    embedding: ResolvedEmbeddingProfile
    im: ResolvedIMConfig
    log_level: LogLevel
    data_dir: Path
    storage: ResolvedStorageConfig
    telemetry: ResolvedTelemetryConfig
    config_fingerprint: str

    @property
    def data_paths(self) -> RuntimeDataPaths:
        root = self.data_dir
        return RuntimeDataPaths(
            root=root,
            platform_db=root / "sqlite" / "platform.db",
            business_db=root / "sqlite" / "business.db",
            chroma=root / "chroma",
            objects=root / "objects",
            reports_tmp=root / "reports-tmp",
        )

    def public_summary(self) -> dict[str, object]:
        """Return the stable, secret-free projection used by CLI and reports."""
        return {
            "environment": self.environment,
            "edition": self.edition,
            "runtime_profile": self.runtime_profile,
            "llm": {
                "active_profile": self.llm.profile_id,
                "provider": self.llm.provider,
                "api_dialect": self.llm.api_dialect,
                "model": self.llm.model,
                "base_url": self.llm.base_url,
                "credential_configured": self.llm.api_key is not None,
            },
            "embedding": {
                "active_profile": self.embedding.profile_id,
                "provider": self.embedding.provider,
                "model": self.embedding.model,
                "revision": self.embedding.revision,
                "trust_remote_code": self.embedding.trust_remote_code,
            },
            "im": {"default": self.im.default},
            "log_level": self.log_level,
            "data_dir": str(self.data_dir),
            "storage": self.storage.model_dump(mode="json"),
            "telemetry": self.telemetry.model_dump(mode="json"),
            "config_fingerprint": self.config_fingerprint,
        }

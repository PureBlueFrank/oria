"""Input and resolved runtime configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ConfigResolutionError(ValueError):
    """Raised when runtime configuration cannot be safely resolved."""


class InputConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMProfileConfig(InputConfigModel):
    provider: str
    api_dialect: Literal["mock", "chat_completions", "responses", "anthropic_messages"]
    model: str
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = None


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
    default: str
    channels: dict[str, IMChannelConfig]


class StorageConfig(InputConfigModel):
    vector: str
    platform_db: str
    biz_db: str
    cache: str
    object: str


class TelemetryConfig(InputConfigModel):
    log_exporter: str
    trace_exporter: str
    metric_exporter: str
    capture_content: bool = False


class RuntimeConfig(InputConfigModel):
    environment: Literal["development", "test", "production"]
    edition: Literal["community", "production"]
    runtime_profile: Literal["demo", "standard"]
    llm: LLMConfig
    embedding: EmbeddingConfig
    im: IMConfig
    log_level: str
    data_dir: Path
    storage: StorageConfig
    telemetry: TelemetryConfig


class ResolvedConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResolvedLLMProfile(ResolvedConfigModel):
    profile_id: str
    provider: str
    api_dialect: Literal["mock", "chat_completions", "responses", "anthropic_messages"]
    model: str
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = None


class ResolvedEmbeddingProfile(ResolvedConfigModel):
    profile_id: str
    provider: str
    model: str | None = None
    revision: str | None = None
    trust_remote_code: bool = False


class ResolvedIMConfig(ResolvedConfigModel):
    default: str


class ResolvedStorageConfig(ResolvedConfigModel):
    vector: str
    platform_db: str
    biz_db: str
    cache: str
    object: str


class ResolvedTelemetryConfig(ResolvedConfigModel):
    log_exporter: str
    trace_exporter: str
    metric_exporter: str
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
    log_level: str
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

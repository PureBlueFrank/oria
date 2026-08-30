"""Fail-closed configuration and budget preflight for external eval runs."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import Field, model_validator

from oria.core.types import ValueModel
from oria.eval.rag import RagDatasetError, load_rag_dataset


class NightlyConfigError(ValueError):
    """Raised before any request when a nightly run is not safe to start."""


class NightlyBudgetExceeded(RuntimeError):
    """Raised before the next request when any hard budget would be exceeded."""


class NightlyBudget(ValueModel):
    max_cases: int = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_cost_usd: float = Field(gt=0)
    max_wall_seconds: int = Field(gt=0)


class NightlyTarget(ValueModel):
    target_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    model: str = Field(min_length=1)
    credential_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    dataset_manifest: str = Field(min_length=1)
    dataset_version: str = Field(pattern=r"^[1-9][0-9]*$")
    repetitions: int = Field(gt=0)
    pricing_snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,127}$")
    rate_tier: Literal["peak", "off_peak"]
    budget: NightlyBudget


class NightlyConfig(ValueModel):
    schema_version: Literal[1] = 1
    targets: tuple[NightlyTarget, ...]

    @model_validator(mode="after")
    def require_unique_targets(self) -> Self:
        if not self.targets or len({target.target_id for target in self.targets}) != len(
            self.targets
        ):
            raise ValueError("nightly targets must be non-empty and unique")
        return self


class TokenPrices(ValueModel):
    input_cache_hit_per_million_usd: float = Field(gt=0)
    input_cache_miss_per_million_usd: float = Field(gt=0)
    output_per_million_usd: float = Field(gt=0)
    reasoning_per_million_usd: float = Field(gt=0)


class TieredModelPrices(ValueModel):
    peak: TokenPrices
    off_peak: TokenPrices


class PricingSnapshot(ValueModel):
    schema_version: Literal[1] = 1
    snapshot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,127}$")
    currency: Literal["USD"]
    unit: Literal["per_million_tokens"]
    source_url: str = Field(pattern=r"^https://[^\s]+$")
    verified_at: datetime
    valid_until: datetime
    models: dict[str, TieredModelPrices]

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if (
            self.verified_at.tzinfo is None
            or self.verified_at.utcoffset() is None
            or self.valid_until.tzinfo is None
            or self.valid_until.utcoffset() is None
        ):
            raise ValueError("pricing validity times must include timezones")
        if self.valid_until <= self.verified_at:
            raise ValueError("pricing validity window is invalid")
        if not self.models:
            raise ValueError("pricing snapshot must contain model prices")
        return self


class NightlyPreflight(ValueModel):
    target_id: str
    status: Literal["ready", "blocked"]
    request_count: Literal[0] = 0
    reason: str | None = None
    pricing_snapshot_id: str | None = None
    dataset_version: str | None = None


class NightlyReservation(ValueModel):
    reservation_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(gt=0)
    cost_usd: float = Field(ge=0)


class NightlyRequest(ValueModel):
    case_id: str = Field(min_length=1)
    repetition: int = Field(gt=0)
    input_tokens_reserved: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)


class NightlyProviderResponse(ValueModel):
    request_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_usage_details(self) -> Self:
        if self.cache_read_tokens > self.input_tokens:
            raise ValueError("cache-read tokens cannot exceed input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning tokens cannot exceed output tokens")
        return self


class NightlyCaseRecord(ValueModel):
    case_id: str
    repetition: int
    request_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_ms: float


class NightlyUsageTotals(ValueModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)


class NightlyRunCard(ValueModel):
    schema_version: Literal[1] = 1
    target_id: str
    status: Literal["passed", "failed"]
    model: str
    dataset_version: str
    pricing_snapshot_id: str
    expected_request_count: int = Field(gt=0)
    request_count: int = Field(ge=0)
    completed_request_count: int = Field(ge=0)
    usage: NightlyUsageTotals
    cases: tuple[NightlyCaseRecord, ...]
    reason: str | None = None


class NightlyBudgetLedger:
    """Reserve worst-case request cost, then settle only from provider usage."""

    def __init__(
        self,
        budget: NightlyBudget,
        prices: TokenPrices,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = budget
        self._prices = prices
        self._clock = clock
        self._started_at = clock()
        self._cases = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_usd = 0.0
        self._pending: NightlyReservation | None = None

    def reserve(self, *, input_tokens: int, max_output_tokens: int) -> NightlyReservation:
        if input_tokens < 0 or max_output_tokens <= 0:
            raise ValueError("nightly token reservation must be non-negative and bounded")
        if self._pending is not None:
            raise RuntimeError("nightly ledger already has a pending reservation")
        estimated_cost = (
            input_tokens * self._prices.input_cache_miss_per_million_usd
            + max_output_tokens
            * max(
                self._prices.output_per_million_usd,
                self._prices.reasoning_per_million_usd,
            )
        ) / 1_000_000
        if self._clock() - self._started_at >= self._budget.max_wall_seconds:
            raise NightlyBudgetExceeded("nightly wall-clock budget is exhausted")
        if self._cases + 1 > self._budget.max_cases:
            raise NightlyBudgetExceeded("nightly case budget is exhausted")
        if self._input_tokens + input_tokens > self._budget.max_input_tokens:
            raise NightlyBudgetExceeded("nightly input-token budget is exhausted")
        if self._output_tokens + max_output_tokens > self._budget.max_output_tokens:
            raise NightlyBudgetExceeded("nightly output-token budget is exhausted")
        if self._cost_usd + estimated_cost > self._budget.max_cost_usd:
            raise NightlyBudgetExceeded("nightly cost budget is exhausted")
        reservation = NightlyReservation(
            reservation_id=f"res_{uuid.uuid4().hex}",
            input_tokens=input_tokens,
            output_tokens=max_output_tokens,
            cost_usd=estimated_cost,
        )
        self._pending = reservation
        return reservation

    def settle(
        self,
        reservation: NightlyReservation,
        *,
        cache_hit_tokens: int,
        cache_miss_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
    ) -> float:
        if self._pending != reservation:
            raise RuntimeError("nightly reservation is not pending")
        if min(cache_hit_tokens, cache_miss_tokens, output_tokens, reasoning_tokens) < 0:
            raise ValueError("nightly usage cannot be negative")
        actual_input = cache_hit_tokens + cache_miss_tokens
        if reasoning_tokens > output_tokens:
            self._pending = None
            raise NightlyBudgetExceeded("reasoning usage exceeds total output usage")
        actual_output = output_tokens
        if actual_input > reservation.input_tokens or actual_output > reservation.output_tokens:
            self._pending = None
            raise NightlyBudgetExceeded("provider usage exceeded the reserved hard budget")
        actual_cost = _request_cost(
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            prices=self._prices,
        )
        self._cases += 1
        self._input_tokens += actual_input
        self._output_tokens += actual_output
        self._cost_usd += actual_cost
        self._pending = None
        if self._clock() - self._started_at > self._budget.max_wall_seconds:
            raise NightlyBudgetExceeded("nightly wall-clock budget was exceeded by the response")
        return actual_cost

    def cancel(self, reservation: NightlyReservation) -> None:
        if self._pending != reservation:
            raise RuntimeError("nightly reservation is not pending")
        self._pending = None

    def totals(self) -> NightlyUsageTotals:
        return NightlyUsageTotals(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cost_usd=self._cost_usd,
        )

    def complete(self, *, expected_cases: int) -> bool:
        return self._pending is None and self._cases == expected_cases


def _request_cost(
    *,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
    prices: TokenPrices,
) -> float:
    return (
        cache_hit_tokens * prices.input_cache_hit_per_million_usd
        + cache_miss_tokens * prices.input_cache_miss_per_million_usd
        + output_tokens
        * max(
            prices.output_per_million_usd,
            prices.reasoning_per_million_usd if reasoning_tokens else 0.0,
        )
    ) / 1_000_000


def load_nightly_config(path: Path) -> NightlyConfig:
    try:
        return NightlyConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise NightlyConfigError("nightly configuration is unavailable or invalid") from exc


def preflight_nightly_target(
    *,
    config_path: Path,
    pricing_dir: Path,
    target_id: str,
    environ: Mapping[str, str],
    now: datetime,
    known_targets: frozenset[str],
) -> NightlyPreflight:
    """Return a request-free card; every validation completes before provider setup."""

    try:
        if now.tzinfo is None or now.utcoffset() is None:
            raise NightlyConfigError("nightly preflight time must include a timezone")
        config = load_nightly_config(config_path)
        if target_id not in known_targets:
            raise NightlyConfigError("nightly target is unknown")
        target = next((item for item in config.targets if item.target_id == target_id), None)
        if target is None:
            raise NightlyConfigError("nightly target is not configured")
        snapshot_path = pricing_dir / f"{target.pricing_snapshot_id}.yaml"
        try:
            snapshot = PricingSnapshot.model_validate(
                yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise NightlyConfigError("pricing snapshot is unavailable or invalid") from exc
        if snapshot.snapshot_id != target.pricing_snapshot_id:
            raise NightlyConfigError("pricing snapshot identity does not match its filename")
        if now > snapshot.valid_until:
            raise NightlyConfigError("pricing snapshot is expired")
        if target.model not in snapshot.models:
            raise NightlyConfigError("pricing snapshot does not cover the selected model")
        if not environ.get(target.credential_env, "").strip():
            raise NightlyConfigError("nightly credential is missing")
        manifest_path = (config_path.parents[1] / target.dataset_manifest).resolve()
        dataset = load_rag_dataset(manifest_path)
        if dataset.manifest.dataset_version != target.dataset_version:
            raise NightlyConfigError("nightly dataset version does not match configuration")
        if target.budget.max_cases > dataset.manifest.case_count:
            raise NightlyConfigError("nightly max_cases exceeds the reviewed dataset")
        if re.fullmatch(r"[0-9a-f]{64}", dataset.manifest.dataset_sha256) is None:
            raise NightlyConfigError("nightly dataset fingerprint is invalid")
    except (NightlyConfigError, RagDatasetError, ValueError) as exc:
        return NightlyPreflight(target_id=target_id, status="blocked", reason=str(exc))
    return NightlyPreflight(
        target_id=target_id,
        status="ready",
        pricing_snapshot_id=snapshot.snapshot_id,
        dataset_version=dataset.manifest.dataset_version,
    )


async def run_nightly_requests(
    *,
    target: NightlyTarget,
    prices: TokenPrices,
    requests: Sequence[NightlyRequest],
    dataset_version: str,
    invoke: Callable[[NightlyRequest], Awaitable[NightlyProviderResponse]],
    clock: Callable[[], float] = time.monotonic,
) -> NightlyRunCard:
    """Run a pre-built bounded request set and never pass an incomplete sample."""

    expected = len(requests)
    identities = {(request.case_id, request.repetition) for request in requests}
    if expected == 0 or len(identities) != expected:
        raise NightlyConfigError("nightly requests must be non-empty and unique")
    if expected != target.budget.max_cases:
        raise NightlyConfigError("nightly request count must exactly match max_cases")

    ledger = NightlyBudgetLedger(target.budget, prices, clock=clock)
    records: list[NightlyCaseRecord] = []
    request_count = 0
    reason: str | None = None
    for request in requests:
        try:
            reservation = ledger.reserve(
                input_tokens=request.input_tokens_reserved,
                max_output_tokens=request.max_output_tokens,
            )
        except NightlyBudgetExceeded as exc:
            reason = str(exc)
            break
        request_count += 1
        try:
            response = await invoke(request)
        except asyncio.CancelledError:
            ledger.cancel(reservation)
            raise
        except Exception as exc:
            ledger.cancel(reservation)
            reason = f"provider request failed: {type(exc).__name__}"
            break
        if response.model != target.model:
            ledger.cancel(reservation)
            reason = "provider response model does not match target"
            break
        try:
            cost = ledger.settle(
                reservation,
                cache_hit_tokens=response.cache_read_tokens,
                cache_miss_tokens=response.input_tokens - response.cache_read_tokens,
                output_tokens=response.output_tokens,
                reasoning_tokens=response.reasoning_tokens,
            )
        except NightlyBudgetExceeded as exc:
            reason = str(exc)
            break
        records.append(
            NightlyCaseRecord(
                case_id=request.case_id,
                repetition=request.repetition,
                request_id=response.request_id,
                model=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_read_tokens=response.cache_read_tokens,
                reasoning_tokens=response.reasoning_tokens,
                cost_usd=cost,
                latency_ms=response.latency_ms,
            )
        )

    passed = reason is None and ledger.complete(expected_cases=expected)
    if not passed and reason is None:
        reason = "nightly sample is incomplete"
    return NightlyRunCard(
        target_id=target.target_id,
        status="passed" if passed else "failed",
        model=target.model,
        dataset_version=dataset_version,
        pricing_snapshot_id=target.pricing_snapshot_id,
        expected_request_count=expected,
        request_count=request_count,
        completed_request_count=len(records),
        usage=ledger.totals(),
        cases=tuple(records),
        reason=reason,
    )

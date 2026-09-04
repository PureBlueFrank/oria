"""Immutable synthetic analytics fact contracts for Scenario B."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise
from typing import Literal, Self

from pydantic import Field, model_validator

from oria.core.types import ValueModel


class FunnelDailyFact(ValueModel):
    tenant_id: str = Field(min_length=1)
    event_date: date
    region: str = Field(min_length=1)
    category: str = Field(min_length=1)
    impressions: int = Field(ge=0)
    visits: int = Field(ge=0)
    enrollments: int = Field(ge=0)
    confirmations: int = Field(ge=0)
    redemptions: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_funnel_order(self) -> Self:
        stages = (
            self.impressions,
            self.visits,
            self.enrollments,
            self.confirmations,
            self.redemptions,
        )
        if any(left < right for left, right in pairwise(stages)):
            raise ValueError("funnel counts must be monotonically non-increasing")
        return self


class ActivityFact(ValueModel):
    tenant_id: str = Field(min_length=1)
    activity_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    category: str = Field(min_length=1)
    activity_type: str = Field(min_length=1)
    merchant_id: str | None = Field(default=None, min_length=1)
    starts_on: date
    ends_on: date

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.starts_on > self.ends_on:
            raise ValueError("activity window must not end before it starts")
        return self


class MarketDailyFact(ValueModel):
    tenant_id: str = Field(min_length=1)
    event_date: date
    region: str = Field(min_length=1)
    category: str = Field(min_length=1)
    market_enrollments: int = Field(ge=0)
    market_redemptions: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_market_order(self) -> Self:
        if self.market_redemptions > self.market_enrollments:
            raise ValueError("market redemptions cannot exceed enrollments")
        return self


class AnalyticsPeriod(ValueModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("analytics period must not end before it starts")
        if self.end_date - self.start_date > timedelta(days=366):
            raise ValueError("analytics period cannot exceed 367 calendar days")
        return self


class FunnelMetrics(ValueModel):
    impressions: int = Field(ge=0)
    visits: int = Field(ge=0)
    enrollments: int = Field(ge=0)
    confirmations: int = Field(ge=0)
    redemptions: int = Field(ge=0)
    visit_rate: float = Field(ge=0, le=1)
    enrollment_rate: float = Field(ge=0, le=1)
    confirmation_rate: float = Field(ge=0, le=1)
    redemption_rate: float = Field(ge=0, le=1)


class FunnelPoint(ValueModel):
    event_date: date | None = None
    region: str | None = None
    category: str | None = None
    metrics: FunnelMetrics


class ActivityWindow(ValueModel):
    activity_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    category: str = Field(min_length=1)
    activity_type: str = Field(min_length=1)
    merchant_id: str | None = Field(default=None, min_length=1)
    starts_on: date
    ends_on: date


class MarketMetrics(ValueModel):
    enrollments: int = Field(ge=0)
    redemptions: int = Field(ge=0)
    redemption_rate: float = Field(ge=0, le=1)


class MarketSegmentOverview(ValueModel):
    region: str | None = None
    category: str | None = None
    current: MarketMetrics
    comparison: MarketMetrics | None = None
    redemption_rate_change: float | None = None


class AnalyticsEvidence(ValueModel):
    evidence_id: str = Field(pattern=r"^ae_[0-9a-f]{32}$")
    dataset_version: str = Field(min_length=1)
    source_tables: tuple[Literal["funnel_daily", "activity_windows", "market_daily"], ...]
    period: AnalyticsPeriod
    dimensions: tuple[Literal["event_date", "region", "category"], ...] = ()
    filters: dict[str, str] = Field(default_factory=dict)
    row_count: int = Field(ge=0)
    provenance: str = Field(pattern=r"^oria://analytics/.+")

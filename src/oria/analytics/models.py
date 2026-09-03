"""Immutable synthetic analytics fact contracts for Scenario B."""

from __future__ import annotations

from datetime import date
from itertools import pairwise
from typing import Self

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

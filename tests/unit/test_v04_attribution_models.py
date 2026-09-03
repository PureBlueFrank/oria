"""V0.4-T01 analytics value and invariant tests."""

from datetime import date

import pytest
from pydantic import ValidationError

from oria.analytics import ActivityFact, FunnelDailyFact, MarketDailyFact

pytestmark = pytest.mark.unit


def test_funnel_fact_requires_monotonic_stage_counts() -> None:
    with pytest.raises(ValidationError, match="monotonically non-increasing"):
        FunnelDailyFact(
            tenant_id="tenant-a",
            event_date=date(2026, 8, 31),
            region="east",
            category="full_service",
            impressions=100,
            visits=80,
            enrollments=40,
            confirmations=41,
            redemptions=20,
        )


def test_activity_fact_rejects_an_inverted_window() -> None:
    with pytest.raises(ValidationError, match="must not end before"):
        ActivityFact(
            tenant_id="tenant-a",
            activity_id="activity-1",
            region="east",
            category="full_service",
            activity_type="incentive",
            starts_on=date(2026, 9, 1),
            ends_on=date(2026, 8, 31),
        )


def test_market_fact_rejects_impossible_conversion() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        MarketDailyFact(
            tenant_id="tenant-a",
            event_date=date(2026, 8, 31),
            region="east",
            category="full_service",
            market_enrollments=10,
            market_redemptions=11,
        )

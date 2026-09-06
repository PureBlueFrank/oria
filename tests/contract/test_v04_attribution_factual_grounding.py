# ruff: noqa: RUF001
"""Regression checks for evidence-led Scenario B reference answers."""

import runpy
from pathlib import Path

import pytest

from oria.analytics.models import AnalyticsPeriod, FunnelPoint
from oria.analytics.query import AnalyticsQueryStore
from oria.eval.attribution_data import generate_attribution_fixture
from oria.eval.datasets import AttributionGoldenCase, load_golden_dataset

pytestmark = pytest.mark.contract

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST = _ROOT / "eval/datasets/scenario_b/manifest.json"


@pytest.mark.parametrize("number", [5, 8, 15, 23, 47])
def test_unestablished_causes_do_not_become_confirmed_or_conflicting(number: int) -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    case = dataset.cases[number - 1]
    assert isinstance(case, AttributionGoldenCase)
    assert case.expected_outcome == "insufficient"
    assert case.expected_abstain
    assert case.root_cause_code is None


def test_causal_answers_are_limited_to_controlled_variants() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    causal = {case.case_id: case.root_cause_code for case in dataset.cases if case.root_cause_code}
    assert causal == {
        "sb-v1-001": "campaign_withdrawal_best_supported",
        "sb-v1-006": "campaign_withdrawal_enrollment_effect",
        "sb-v1-007": "campaign_withdrawal_best_supported",
        "sb-v1-025": "visit_rate_drop",
        "sb-v1-044": "campaign_withdrawal_best_supported",
        "sb-v1-046": "campaign_withdrawal_enrollment_effect",
        "sb-v1-049": "campaign_withdrawal_confirmation_effect",
        "sb-v1-050": "campaign_withdrawal_best_supported",
    }
    assert all(case.fixture_variant != "standard" for case in dataset.cases if case.root_cause_code)


def test_conflicting_cases_have_distinct_observable_signals() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    conflicting = {
        case.case_id: case.fixture_variant
        for case in dataset.cases
        if case.expected_outcome == "conflicting"
    }
    assert conflicting == {
        "sb-v1-019": "market_conflict",
        "sb-v1-020": "mixed_funnel",
        "sb-v1-021": "overlapping_events",
        "sb-v1-022": "systemic_category",
        "sb-v1-024": "beverage_conflict",
        "sb-v1-043": "mixed_funnel",
    }


def test_followup_cases_carry_actual_conversation_history() -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    histories = {
        case.case_id: tuple(message.role for message in case.conversation_history)
        for case in dataset.cases
        if case.conversation_history
    }
    assert histories == {f"sb-v1-{number:03d}": ("user", "assistant") for number in range(39, 45)}


def test_generator_and_readable_review_match_the_draft() -> None:
    module = runpy.run_path(str(_ROOT / "scripts/generate_attribution_golden.py"))
    cases = module["_build_cases"]()
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    for index, case in enumerate(cases):
        case["split"] = "development" if index < 30 else "holdout"
        case["review"] = dataset.cases[index].review.model_dump(mode="json")
    assert cases == [case.model_dump(mode="json") for case in dataset.cases]
    assert module["_review_document"](cases) == (_MANIFEST.parent / "CASES.md").read_text(
        encoding="utf-8"
    )


def test_frozen_dataset_generator_refuses_in_place_overwrite() -> None:
    module = runpy.run_path(str(_ROOT / "scripts/generate_attribution_golden.py"))

    with pytest.raises(ValueError, match="refusing to overwrite frozen"):
        module["_assert_output_is_not_frozen"]()


@pytest.fixture(scope="module")
def verified_evidence(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    directory = tmp_path_factory.mktemp("attribution-evidence")
    query_db = directory / "analytics.db"
    generate_attribution_fixture(query_db, directory / "labels.db")
    store = AnalyticsQueryStore(query_db)

    def rows(
        region: str | None = "east",
        category: str | None = "full_service",
        start: str = "2026-08-18",
        end: str = "2026-09-01",
        tenant: str = "local-community",
    ) -> tuple[FunnelPoint, ...]:
        return store.query_funnel(
            tenant_id=tenant,
            period=AnalyticsPeriod(start_date=start, end_date=end),
            dimensions=("event_date", "region", "category"),
            region=region,
            category=category,
        )[0]

    def total(points: tuple[FunnelPoint, ...], field: str) -> int:
        return sum(getattr(p.metrics, field) for p in points)

    def rate(
        points: tuple[FunnelPoint, ...],
        numerator: str = "redemptions",
        denominator: str = "confirmations",
    ) -> float:
        return 100 * total(points, numerator) / total(points, denominator)

    def daily(points: tuple[FunnelPoint, ...], field: str = "enrollments") -> float:
        return total(points, field) / len(points)

    def market(region: str | None, category: str) -> tuple[float, float]:
        segments, _ = store.query_market_overview(
            tenant_id="local-community",
            period=AnalyticsPeriod(start_date="2026-08-31", end_date="2026-08-31"),
            comparison="previous_period",
            dimensions=("category",),
            region=region,
            category=category,
        )
        segment = segments[0]
        assert segment.comparison is not None
        return (
            100 * segment.comparison.redemptions / segment.comparison.enrollments,
            100 * segment.current.redemptions / segment.current.enrollments,
        )

    before = rows(end="2026-08-30")
    after = rows(start="2026-08-31")
    day30 = rows(start="2026-08-30", end="2026-08-30")
    day31 = rows(start="2026-08-31", end="2026-08-31")
    north = rows(region="north")
    north_before = rows(region="north", end="2026-08-30")
    north_after = rows(region="north", start="2026-08-31")
    quick = rows(category="quick_service")
    north_quick = rows(region="north", category="quick_service")
    beverage = rows(category="beverage")
    secondary = rows(tenant="tenant-secondary")
    secondary_north = rows(region="north", tenant="tenant-secondary")
    deadlines = rows(start="2026-08-28", end="2026-08-30")
    north_deadlines = rows(region="north", start="2026-08-28", end="2026-08-30")
    beverages = rows(category="beverage", start="2026-08-30")
    earlier = rows(start="2026-08-29", end="2026-08-30")
    market_full = market("east", "full_service")
    market_beverage = market("east", "beverage")
    overall_markets = [market(None, c) for c in ("full_service", "beverage", "quick_service")]

    def fmt(value: float) -> str:
        return f"{value:.2f}"

    evidence = {
        "daily": (
            f"2026-08-30→08-31，华东正餐访客 {total(day30, 'visits')}→{total(day31, 'visits')}、"
            f"报名 {total(day30, 'enrollments')}→{total(day31, 'enrollments')}、"
            f"确认 {total(day30, 'confirmations')}→{total(day31, 'confirmations')}、"
            f"核销 {total(day30, 'redemptions')}→{total(day31, 'redemptions')}；"
            f"核销/确认 {rate(day30):.2f}%→{rate(day31):.2f}%，"
            f"报名/访客 {rate(day30, 'enrollments', 'visits'):.2f}%→"
            f"{rate(day31, 'enrollments', 'visits'):.2f}%。"
        ),
        "period": (
            f"2026-08-18—08-30 与 08-31—09-01 相比，华东正餐日均访客 "
            f"{daily(before, 'visits'):.2f}→{daily(after, 'visits'):.2f}、"
            f"日均报名 {daily(before):.2f}→{daily(after):.2f}；"
            f"汇总核销/确认 {rate(before):.2f}%→{rate(after):.2f}%。"
        ),
        "confirmation": (
            f"同一前后窗口，华东正餐汇总确认/报名 "
            f"{rate(before, 'confirmations', 'enrollments'):.2f}%→"
            f"{rate(after, 'confirmations', 'enrollments'):.2f}%，北区正餐 "
            f"{rate(north_before, 'confirmations', 'enrollments'):.2f}%→"
            f"{rate(north_after, 'confirmations', 'enrollments'):.2f}%；华东曝光→报名 "
            f"{rate(before, 'enrollments', 'impressions'):.2f}%→"
            f"{rate(after, 'enrollments', 'impressions'):.2f}%。"
        ),
        "market": (
            f"2026-08-30→08-31，华东正餐大盘核销/报名 "
            f"{market_full[0]:.2f}%→{market_full[1]:.2f}%，变化 "
            f"{market_full[1] - market_full[0]:.2f} 个百分点；与业务漏斗核销/确认分母不同。"
        ),
        "quick": (
            f"2026-08-18—09-01，华东快餐日均报名 {daily(quick):.2f}，"
            f"北区快餐 {daily(north_quick):.2f}，华东正餐 {daily(rows()):.2f}。"
        ),
        "categories": (
            f"2026-08-18—09-01，华东饮料、快餐、正餐汇总核销/确认分别为 "
            f"{rate(beverage):.2f}%、{rate(quick):.2f}%、{rate(rows()):.2f}%。"
        ),
        "all_categories": (
            f"2026-08-18—09-01，合并华东和北区后，饮料与正餐汇总核销/确认分别为 "
            f"{rate(rows(region=None, category='beverage')):.2f}% 和 "
            f"{rate(rows(region=None)):.2f}%。"
        ),
        "north": (
            f"2026-08-18—09-01，北区正餐日核销/确认范围 "
            f"{min(rate((p,)) for p in north):.2f}%—{max(rate((p,)) for p in north):.2f}%，"
            f"报名范围 {min(p.metrics.enrollments for p in north)}—"
            f"{max(p.metrics.enrollments for p in north)}。"
        ),
        "regions": (
            f"2026-08-18—09-01，华东与北区正餐汇总核销/确认分别为 "
            f"{rate(rows()):.2f}% 和 {rate(north):.2f}%。"
        ),
        "secondary": (
            f"2026-08-18—09-01，在 tenant-secondary 内，华东与北区正餐日均报名分别为 "
            f"{daily(secondary):.2f} 和 {daily(secondary_north):.2f}；两区域漏斗记录均为 "
            f"{len(rows(tenant='tenant-secondary', category=None))} 条。"
        ),
        "deadline": (
            "2026-08-28、08-29、08-30，华东正餐报名为 "
            + "、".join(str(p.metrics.enrollments) for p in deadlines)
            + "，北区正餐为 "
            + "、".join(str(p.metrics.enrollments) for p in north_deadlines)
            + "；华东核销/确认分别为 "
            + "%、".join(fmt(rate((p,))) for p in deadlines)
            + "%。"
        ),
        "beverage": (
            "2026-08-30、08-31、09-01，华东饮料核销/确认分别为 "
            + "%、".join(fmt(rate((p,))) for p in beverages)
            + f"%；华东饮料大盘 08-30→08-31 核销/报名为 "
            f"{market_beverage[0]:.2f}%→{market_beverage[1]:.2f}%。"
        ),
        "market_categories": (
            "2026-08-30→08-31，合并华东和北区的大盘正餐核销/报名 "
            + "，".join(
                f"{prefix}{values[0]:.2f}%→{values[1]:.2f}%"
                for prefix, values in zip(("", "饮料 ", "快餐 "), overall_markets, strict=True)
            )
            + "。"
        ),
        "comparison": (
            f"华东正餐 2026-08-29—08-30 核销/确认为 "
            f"{total(earlier, 'redemptions')}/{total(earlier, 'confirmations')}="
            f"{rate(earlier):.2f}%，"
            f"08-31—09-01 为 {total(after, 'redemptions')}/{total(after, 'confirmations')}="
            f"{rate(after):.2f}%；下降 {rate(earlier) - rate(after):.2f} 个百分点，"
            f"相对下降 {100 * (1 - rate(after) / rate(earlier)):.2f}%。"
        ),
    }
    all_rows = rows(region=None, category=None)
    assert {p.region for p in all_rows} == {"east", "north"}
    assert {p.category for p in all_rows} == {"full_service", "quick_service", "beverage"}
    assert len({p.event_date for p in all_rows}) == 15
    assert not rows(start="2026-06-01", end="2026-06-30")
    assert not rows(category="retail")
    assert not rows(region="south")
    assert not rows(start="2025-08-18", end="2025-09-01")
    evidence["range"] = (
        f"当前合成漏斗与大盘仅覆盖 {min(p.event_date for p in all_rows)}—"
        f"{max(p.event_date for p in all_rows):%m-%d}，区域为 east/north，"
        "品类为 full_service/quick_service/beverage。"
    )
    period = AnalyticsPeriod(start_date="2026-08-18", end_date="2026-09-01")
    activities = store.query_activity(
        tenant_id="local-community", period=period, category="full_service", merchant_id=None
    )[0]
    assert len(activities) == 1
    activity = activities[0]
    assert activity.region == "east"
    assert not store.query_activity(
        tenant_id="local-community",
        period=AnalyticsPeriod(start_date="2026-08-31", end_date="2026-09-01"),
        category="full_service",
        merchant_id=None,
    )[0]
    evidence["activity"] = (
        f"现有 local-community 活动记录中，华东正餐 {activity.activity_id} 覆盖 "
        f"{activity.starts_on}—{activity.ends_on:%m-%d}；08-31—09-01 无覆盖华东正餐的记录。"
    )
    quick_activities = store.query_activity(
        tenant_id="local-community", period=period, category="quick_service", merchant_id=None
    )[0]
    assert len(quick_activities) == 1
    quick_activity = quick_activities[0]
    assert quick_activity.region == "east"
    assert quick_activity.activity_type == "always_on"
    assert not store.query_activity(
        tenant_id="local-community", period=period, category="beverage", merchant_id=None
    )[0]
    evidence["local_activity"] = (
        "现有 local-community 活动记录仅包含华东正餐激励活动和华东快餐 always-on 活动；"
        f"快餐覆盖 {quick_activity.starts_on}—{quick_activity.ends_on:%m-%d}，"
        "北区及饮料无活动记录。"
    )
    secondary_activities = store.query_activity(
        tenant_id="tenant-secondary", period=period, category="full_service", merchant_id=None
    )[0]
    assert len(secondary_activities) == 1
    secondary_activity = secondary_activities[0]
    assert secondary_activity.region == "east"
    for category in ("quick_service", "beverage"):
        assert not store.query_activity(
            tenant_id="tenant-secondary", period=period, category=category, merchant_id=None
        )[0]
    evidence["secondary_activity"] = (
        "现有 tenant-secondary 活动记录仅包含华东正餐 "
        f"{secondary_activity.activity_id}，覆盖 {secondary_activity.starts_on}—"
        f"{secondary_activity.ends_on:%m-%d}；北区无记录。"
    )
    assert not store.query_activity(
        tenant_id="local-community",
        period=period,
        category=None,
        merchant_id="synthetic-merchant-unlisted",
    )[0]
    evidence["unknown"] = (
        "在 local-community 的 2026-08-18—09-01 活动记录中，"
        "merchant_id=synthetic-merchant-unlisted 无匹配记录。"
    )
    from oria.analytics.models import FunnelMetrics
    from oria.tools.analytics import QueryFunnelParams

    assert not any("amount" in field for field in FunnelMetrics.model_fields)
    evidence["money"] = "漏斗只包含曝光、访客、报名、确认、核销数量及转化率，没有核销金额字段。"
    dimension_schema = QueryFunnelParams.model_json_schema()["properties"]["dimensions"]["items"]
    assert set(dimension_schema["enum"]) == {"event_date", "region", "category"}
    evidence["merchant_dimension"] = (
        "漏斗可分组维度仅为 event_date/region/category，不含 merchant_id。"
    )

    variant_stores: dict[str, AnalyticsQueryStore] = {}

    def variant_store(name: str) -> AnalyticsQueryStore:
        existing = variant_stores.get(name)
        if existing is not None:
            return existing
        variant_dir = directory / name
        query_database = variant_dir / "analytics.db"
        generate_attribution_fixture(
            query_database,
            variant_dir / "labels.db",
            fixture_variant=name,  # type: ignore[arg-type]
        )
        created = AnalyticsQueryStore(query_database)
        variant_stores[name] = created
        return created

    def variant_rows(
        name: str,
        *,
        region: str = "east",
        category: str = "full_service",
        start: str = "2026-08-18",
        end: str = "2026-09-01",
    ) -> tuple[FunnelPoint, ...]:
        return variant_store(name).query_funnel(
            tenant_id="local-community",
            period=AnalyticsPeriod(start_date=start, end_date=end),
            dimensions=("event_date",),
            region=region,
            category=category,
        )[0]

    def variant_market(name: str, region: str, category: str) -> tuple[float, float]:
        segments, _ = variant_store(name).query_market_overview(
            tenant_id="local-community",
            period=AnalyticsPeriod(start_date="2026-08-31", end_date="2026-09-01"),
            comparison="previous_period",
            dimensions=("category",),
            region=region,
            category=category,
        )
        segment = segments[0]
        assert segment.comparison is not None
        return segment.comparison.redemption_rate * 100, segment.current.redemption_rate * 100

    def variant_activities(name: str, category: str) -> tuple[object, ...]:
        return variant_store(name).query_activity(
            tenant_id="local-community",
            period=AnalyticsPeriod(start_date="2026-08-18", end_date="2026-09-01"),
            category=category,
            merchant_id=None,
        )[0]

    campaign_before = variant_rows("campaign_effect", end="2026-08-30")
    campaign_after = variant_rows("campaign_effect", start="2026-08-31")
    campaign_north_before = variant_rows("campaign_effect", region="north", end="2026-08-30")
    campaign_north_after = variant_rows("campaign_effect", region="north", start="2026-08-31")
    campaign_market = variant_market("campaign_effect", "east", "full_service")
    evidence["campaign_effect"] = (
        "活动退出变体中，华东正餐核销/确认在活动期 2026-08-18—08-30 为 "
        f"{rate(campaign_before):.2f}%，08-31—09-01 为 {rate(campaign_after):.2f}%；"
        f"北区同期为 {rate(campaign_north_before):.2f}%→{rate(campaign_north_after):.2f}%，"
        "华东正餐大盘在等长窗口 08-29—08-30 与 08-31—09-01 的核销/报名为 "
        f"{campaign_market[0]:.2f}%→{campaign_market[1]:.2f}%。"
    )
    evidence["campaign_upstream_stable"] = (
        "活动退出变体中，华东正餐报名/访客为 "
        f"{rate(campaign_before, 'enrollments', 'visits'):.2f}%→"
        f"{rate(campaign_after, 'enrollments', 'visits'):.2f}%，确认/报名为 "
        f"{rate(campaign_before, 'confirmations', 'enrollments'):.2f}%→"
        f"{rate(campaign_after, 'confirmations', 'enrollments'):.2f}%；"
        "异常集中在核销/确认环节。"
    )

    market_conflict_before = variant_rows("market_conflict", end="2026-08-30")
    market_conflict_after = variant_rows("market_conflict", start="2026-08-31")
    market_conflict_north_before = variant_rows("market_conflict", region="north", end="2026-08-30")
    market_conflict_north_after = variant_rows(
        "market_conflict", region="north", start="2026-08-31"
    )
    market_conflict_market = variant_market("market_conflict", "east", "full_service")
    evidence["market_conflict"] = (
        "大盘冲突变体中，华东正餐业务核销/确认为 "
        f"{rate(market_conflict_before):.2f}%→{rate(market_conflict_after):.2f}%，"
        "华东正餐大盘在等长窗口的核销/报名也由 "
        f"{market_conflict_market[0]:.2f}% 降至 {market_conflict_market[1]:.2f}%；"
        "北区业务核销/确认保持在 "
        f"{rate(market_conflict_north_before):.2f}%→"
        f"{rate(market_conflict_north_after):.2f}%。"
    )

    mixed_before = variant_rows("mixed_funnel", end="2026-08-30")
    mixed_after = variant_rows("mixed_funnel", start="2026-08-31")
    evidence["mixed_funnel"] = (
        "混合漏斗变体中，华东正餐访问/曝光为 "
        f"{rate(mixed_before, 'visits', 'impressions'):.2f}%→"
        f"{rate(mixed_after, 'visits', 'impressions'):.2f}%，核销/确认为 "
        f"{rate(mixed_before):.2f}%→{rate(mixed_after):.2f}%，报名/访问保持在 "
        f"{rate(mixed_before, 'enrollments', 'visits'):.2f}%→"
        f"{rate(mixed_after, 'enrollments', 'visits'):.2f}%；曝光日均约 "
        f"{daily(mixed_before, 'impressions'):.2f}→{daily(mixed_after, 'impressions'):.2f}。"
    )

    overlapping_before = variant_rows("overlapping_events", end="2026-08-30")
    overlapping_after = variant_rows("overlapping_events", start="2026-08-31")
    overlapping = variant_activities("overlapping_events", "full_service")
    assert {activity.activity_type for activity in overlapping} == {
        "merchant_incentive",
        "redemption_rule_pilot",
    }
    evidence["overlapping_events"] = (
        "重叠事件变体中，华东正餐业务核销/确认为 "
        f"{rate(overlapping_before):.2f}%→{rate(overlapping_after):.2f}%；"
        "merchant_incentive 和 redemption_rule_pilot 两项记录都在 2026-08-30 结束。"
    )

    beverage_before = variant_rows("beverage_conflict", category="beverage", end="2026-08-30")
    beverage_after = variant_rows("beverage_conflict", category="beverage", start="2026-08-31")
    beverage_market = variant_market("beverage_conflict", "east", "beverage")
    beverage_activities = variant_activities("beverage_conflict", "beverage")
    assert {activity.activity_type for activity in beverage_activities} == {
        "consumer_discount",
        "redemption_rule_pilot",
    }
    evidence["beverage_conflict"] = (
        "饮料冲突变体中，华东饮料核销/确认为 "
        f"{rate(beverage_before):.2f}%→{rate(beverage_after):.2f}%，"
        f"大盘在等长窗口的核销/报名为 {beverage_market[0]:.2f}%→"
        f"{beverage_market[1]:.2f}%；consumer_discount 与 redemption_rule_pilot "
        "两项记录都在 2026-08-30 结束。"
    )

    systemic_before = variant_rows("systemic_category", end="2026-08-30")
    systemic_after = variant_rows("systemic_category", start="2026-08-31")
    systemic_north_before = variant_rows("systemic_category", region="north", end="2026-08-30")
    systemic_north_after = variant_rows("systemic_category", region="north", start="2026-08-31")
    systemic_east_market = variant_market("systemic_category", "east", "full_service")
    systemic_north_market = variant_market("systemic_category", "north", "full_service")
    evidence["systemic_category"] = (
        "品类共同变化变体中，华东与北区正餐业务核销/确认分别为 "
        f"{rate(systemic_before):.2f}%→{rate(systemic_after):.2f}% 和 "
        f"{rate(systemic_north_before):.2f}%→{rate(systemic_north_after):.2f}%；"
        "两区域正餐大盘在等长窗口的核销/报名也分别由 "
        f"{systemic_east_market[0]:.2f}%→{systemic_east_market[1]:.2f}% 和 "
        f"{systemic_north_market[0]:.2f}%→{systemic_north_market[1]:.2f}%。"
    )

    upstream_before = variant_rows("upstream_drop", end="2026-08-30")
    upstream_after = variant_rows("upstream_drop", start="2026-08-31")
    evidence["upstream_drop"] = (
        "上游变化变体中，华东正餐访问/曝光为 "
        f"{rate(upstream_before, 'visits', 'impressions'):.2f}%→"
        f"{rate(upstream_after, 'visits', 'impressions'):.2f}%，日均访问约 "
        f"{daily(upstream_before, 'visits'):.2f}→{daily(upstream_after, 'visits'):.2f}；"
        "报名/访问保持 "
        f"{rate(upstream_before, 'enrollments', 'visits'):.2f}%→"
        f"{rate(upstream_after, 'enrollments', 'visits'):.2f}%，核销/确认保持 "
        f"{rate(upstream_before):.2f}%→{rate(upstream_after):.2f}%。"
    )

    enrollment_before = variant_rows("campaign_enrollment", end="2026-08-30")
    enrollment_after = variant_rows("campaign_enrollment", start="2026-08-31")
    enrollment_north_before = variant_rows("campaign_enrollment", region="north", end="2026-08-30")
    enrollment_north_after = variant_rows("campaign_enrollment", region="north", start="2026-08-31")
    evidence["campaign_enrollment"] = (
        "报名环节变体中，华东正餐报名/访问为 "
        f"{rate(enrollment_before, 'enrollments', 'visits'):.2f}%→"
        f"{rate(enrollment_after, 'enrollments', 'visits'):.2f}%，访问/曝光为 "
        f"{rate(enrollment_before, 'visits', 'impressions'):.2f}%→"
        f"{rate(enrollment_after, 'visits', 'impressions'):.2f}%，核销/确认为 "
        f"{rate(enrollment_before):.2f}%→{rate(enrollment_after):.2f}%；"
        "北区报名/访问保持 "
        f"{rate(enrollment_north_before, 'enrollments', 'visits'):.2f}%→"
        f"{rate(enrollment_north_after, 'enrollments', 'visits'):.2f}%。"
    )

    confirmation_before = variant_rows("campaign_confirmation", end="2026-08-30")
    confirmation_after = variant_rows("campaign_confirmation", start="2026-08-31")
    confirmation_north_before = variant_rows(
        "campaign_confirmation", region="north", end="2026-08-30"
    )
    confirmation_north_after = variant_rows(
        "campaign_confirmation", region="north", start="2026-08-31"
    )
    evidence["campaign_confirmation"] = (
        "确认环节变体中，华东正餐确认/报名为 "
        f"{rate(confirmation_before, 'confirmations', 'enrollments'):.2f}%→"
        f"{rate(confirmation_after, 'confirmations', 'enrollments'):.2f}%，"
        f"核销/确认保持 {rate(confirmation_before):.2f}%→"
        f"{rate(confirmation_after):.2f}%；北区确认/报名保持 "
        f"{rate(confirmation_north_before, 'confirmations', 'enrollments'):.2f}%→"
        f"{rate(confirmation_north_after, 'confirmations', 'enrollments'):.2f}%。"
    )

    missing_before = variant_rows("missing_activity", end="2026-08-30")
    missing_after = variant_rows("missing_activity", start="2026-08-31")
    missing_market = variant_market("missing_activity", "east", "full_service")
    assert not variant_activities("missing_activity", "full_service")
    evidence["missing_activity"] = (
        "缺失活动变体中，华东正餐业务核销/确认为 "
        f"{rate(missing_before):.2f}%→{rate(missing_after):.2f}%，"
        "但授权活动记录中没有华东正餐活动；华东正餐大盘等长窗口核销/报名为 "
        f"{missing_market[0]:.2f}%→{missing_market[1]:.2f}%。"
    )
    return evidence


_CASE_EVIDENCE = {
    1: (
        "campaign_effect",
        "campaign_upstream_stable",
        "activity",
    ),
    2: (
        "quick",
        "local_activity",
    ),
    3: (
        "north",
        "regions",
    ),
    4: (
        "all_categories",
        "categories",
    ),
    5: (
        "secondary",
        "secondary_activity",
    ),
    6: (
        "campaign_enrollment",
        "activity",
    ),
    7: (
        "campaign_effect",
        "campaign_upstream_stable",
        "activity",
    ),
    8: (
        "range",
        "local_activity",
    ),
    9: (
        "daily",
        "period",
        "confirmation",
    ),
    10: (
        "secondary",
        "secondary_activity",
    ),
    11: ("unknown",),
    12: ("range",),
    13: ("range",),
    14: ("range",),
    15: ("missing_activity",),
    16: ("money",),
    17: (),
    18: ("range",),
    19: (
        "market_conflict",
        "activity",
    ),
    20: ("mixed_funnel",),
    21: ("overlapping_events",),
    22: (
        "systemic_category",
        "activity",
    ),
    23: (),
    24: ("beverage_conflict",),
    25: ("upstream_drop",),
    26: ("systemic_category",),
    27: (),
    28: (),
    29: (),
    30: (),
    31: ("merchant_dimension",),
    32: (),
    33: (),
    34: (),
    35: (
        "daily",
        "activity",
    ),
    36: (),
    37: (),
    38: (),
    39: ("daily",),
    40: ("activity",),
    41: ("comparison",),
    42: (
        "north",
        "regions",
    ),
    43: ("mixed_funnel",),
    44: (
        "campaign_effect",
        "campaign_upstream_stable",
        "activity",
    ),
    45: (
        "quick",
        "local_activity",
    ),
    46: (
        "campaign_enrollment",
        "activity",
    ),
    47: (
        "north",
        "local_activity",
    ),
    48: (
        "categories",
        "local_activity",
    ),
    49: (
        "campaign_confirmation",
        "activity",
    ),
    50: (
        "campaign_effect",
        "campaign_upstream_stable",
        "activity",
    ),
}


@pytest.mark.parametrize("number", range(1, 51))
def test_each_case_references_verified_facts_in_its_scope(
    number: int, verified_evidence: dict[str, str]
) -> None:
    dataset = load_golden_dataset(_MANIFEST, require_human_review=False)
    case = dataset.cases[number - 1]
    assert case.required_evidence == tuple(verified_evidence[key] for key in _CASE_EVIDENCE[number])
    if case.expected_outcome == "insufficient":
        assert case.requested_data
    else:
        assert not case.requested_data

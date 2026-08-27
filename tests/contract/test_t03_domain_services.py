"""Direct-path contracts for typed T03 domain services."""

from __future__ import annotations

from pathlib import Path

import pytest

from oria.config import resolve_runtime_config
from oria.core.runtime import build_runtime
from oria.domain.eligibility import EligibilityPolicy
from oria.domain.models import MerchantRecord, MerchantSeedSet
from oria.domain.repositories import MerchantRepository
from oria.domain.services import (
    CampaignRuleService,
    DefaultMerchantService,
    MerchantService,
    PackageCampaignRuleService,
)
from oria.permission.local import local_cli_executor, local_operator
from oria.resources.loader import load_demo_data

pytestmark = pytest.mark.contract


class _FakeMerchantRepository:
    def __init__(self, records: tuple[MerchantRecord, ...]) -> None:
        self.records = records
        self.contexts: list[object] = []

    async def list_for_eligibility(self, ctx: object) -> tuple[MerchantRecord, ...]:
        self.contexts.append(ctx)
        return self.records

    async def seed(self, seed_set: MerchantSeedSet) -> int:
        return len(seed_set.merchants)


def _record(merchant_id: str, *, city: str = "上海") -> MerchantRecord:
    return MerchantRecord(
        tenant_id="local-community",
        merchant_id=merchant_id,
        version=1,
        display_name=f"虚构-{merchant_id}",
        categories=("餐饮",),
        cities=(city,),
        enrollment_systems=("demo-enroll",),
        sales_org_code="synthetic-east-a",
        active=True,
    )


def _config(tmp_path: Path):
    return resolve_runtime_config(environ={}, data_dir=tmp_path / "data")


@pytest.mark.asyncio
async def test_typed_services_inject_repository_rules_and_eligibility(tmp_path: Path) -> None:
    runtime = await build_runtime(_config(tmp_path))
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="service-session",
            thread_id="service-thread",
            run_id="service-run",
        )
        bundle = load_demo_data()
        rules_service = PackageCampaignRuleService(bundle.rules)
        repository = _FakeMerchantRepository(
            (_record("demo-m001"), _record("demo-m002"), _record("demo-m006", city="北京"))
        )
        repository_contract: MerchantRepository = repository
        rules_contract: CampaignRuleService = rules_service
        merchant_service = DefaultMerchantService(
            repository_contract,
            EligibilityPolicy(),
            rules_contract,
        )
        merchant_contract: MerchantService = merchant_service

        result = await merchant_contract.eligible_merchants("demo-east-dining-v1", 1, ctx)

        assert result.rule_set_id == "demo-east-dining-v1"
        assert result.evaluated_count == 3
        assert [merchant.merchant_id for merchant in result.merchants] == ["demo-m001"]
        assert repository.contexts == [ctx]
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_campaign_rule_service_rejects_unknown_or_cross_tenant_rule_ids(
    tmp_path: Path,
) -> None:
    runtime = await build_runtime(_config(tmp_path))
    try:
        ctx = runtime.new_context(
            actor=local_operator(),
            executor=local_cli_executor(),
            session_id="rule-session",
            thread_id="rule-thread",
            run_id="rule-run",
        )
        service = PackageCampaignRuleService(load_demo_data().rules)

        with pytest.raises(LookupError, match="unavailable"):
            await service.get_rule_set("caller-invented-rules", ctx)
    finally:
        await runtime.aclose()

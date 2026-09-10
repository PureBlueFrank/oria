"""Human-readable presentation for initialized synthetic demo data."""

from __future__ import annotations

from oria.data import DataInitializationResult
from oria.domain.models import CampaignRuleSet, MerchantSeedSet
from oria.presentation.workflow import _table


def render_data_initialization(
    result: DataInitializationResult,
    *,
    merchants: MerchantSeedSet,
    rules: CampaignRuleSet,
) -> str:
    """Render initialization outcome and the verified resource contents."""

    merchant_rows = [
        (
            merchant.display_name,
            "、".join(merchant.cities),
            "、".join(merchant.categories),
            "是" if merchant.active else "否",
            merchant.internal_sales_org_code(),
        )
        for merchant in merchants.merchants
    ]
    merchant_table = _table(
        ("商家名称", "城市", "类目", "已启用", "销售组织"),
        merchant_rows,
        minimums=(14, 8, 8, 6, 14),
        long_text_columns=(0,),
    )

    confirmation = " → ".join(rules.confirmation_policy.ordered_steps)
    benefit = rules.benefit_policy
    rule_rows = (
        (
            "基础信息",
            f"模板 {rules.basic.template_ref}; 类型 {rules.basic.campaign_type}; "
            f"商品范围 {'、'.join(rules.basic.product_scope)}",
        ),
        (
            "招商范围",
            f"类目 {'、'.join(rules.recruitment_scope.categories)}; "
            f"城市 {'、'.join(rules.recruitment_scope.cities)}; "
            f"报名系统 {'、'.join(rules.recruitment_scope.enrollment_systems)}; "
            f"销售组织 {'、'.join(rules.recruitment_scope.internal_sales_org_scope())}",
        ),
        (
            "报名规则",
            f"模式 {rules.enrollment_policy.mode}; "
            f"圈品策略 {rules.enrollment_policy.product_circle_policy_ref}; "
            f"选品策略 {rules.enrollment_policy.assortment_policy_ref}",
        ),
        (
            "优惠档位",
            f"档位 {'、'.join(benefit.tiers)}; 计价 "
            f"{'、'.join(rule.funding_type for rule in benefit.tier_rules)}; "
            f"预算上限 {benefit.budget_cap} {benefit.currency}; 取整 {benefit.rounding}",
        ),
        ("确认链", f"{confirmation}; 超时处理 {rules.confirmation_policy.timeout_action}"),
        (
            "商家素材",
            f"标题 {rules.merchant_material.title}; 标签 {'、'.join(rules.merchant_material.tags)}",
        ),
    )
    rule_table = _table(
        ("规则类别", "关键内容"),
        rule_rows,
        minimums=(10, 30),
        long_text_columns=(1,),
    )

    return "\n".join(
        (
            "本地演示数据已初始化",
            f"数据集版本: {result.dataset_version}",
            f"数据库版本: 平台 {result.platform_revision}; 业务 {result.business_revision}",
            f"本次新增商家: {result.merchants_inserted} 家",
            "",
            f"商家数据 (共 {len(merchants.merchants)} 家)",
            merchant_table,
            "",
            "活动规则 (共 6 类)",
            rule_table,
        )
    )

"""Chinese display labels for public synthetic identifiers."""

from __future__ import annotations

from collections.abc import Iterable

_IDENTIFIER_LABELS = {
    "synthetic-summer-dining": "夏季餐饮活动模板",
    "merchant_recruitment": "商户招募",
    "eligible_dining_products": "合格餐饮商品",
    "demo-enroll": "演示报名系统",
    "other-demo": "其他演示系统",
    "synthetic-product-circle-policy": "商品圈选策略\uff08合成\uff09",
    "synthetic-link-rule-v1": "关联活动规则\uff08合成\uff09",
    "synthetic-assortment-policy": "选品策略\uff08合成\uff09",
    "synthetic-east-a": "华东一组\uff08合成\uff09",
    "synthetic-east-b": "华东二组\uff08合成\uff09",
    "synthetic-north": "北部\uff08合成\uff09",
    "base": "基础档",
    "boosted": "膨胀档",
    "hybrid": "混合模式",
    "fixed_amount": "固定金额",
    "stepped": "阶梯",
    "half_up": "四舍五入",
    "eligible_merchant_and_linked_campaign": "合格商家且关联活动",
    "merchant": "商家",
    "sales": "销售",
    "sales_manager": "销售经理",
    "reject": "拒绝",
    "async": "异步",
    "all_terminal": "全部完成后",
}


def display_identifier(value: str) -> str:
    """Return a Chinese label when known and preserve unknown identifiers."""

    return _IDENTIFIER_LABELS.get(value, value)


def display_identifiers(values: Iterable[str], *, separator: str = "、") -> str:
    """Render a sequence of identifiers with stable presentation labels."""

    return separator.join(display_identifier(value) for value in values)

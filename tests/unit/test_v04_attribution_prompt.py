"""V0.4-T03 attribution prompt version contracts."""

from __future__ import annotations

import pytest

from oria.prompts import PromptManager
from oria.prompts.registry import PromptError

pytestmark = pytest.mark.unit


def test_attribution_prompt_has_fixed_version_and_golden_render() -> None:
    prompts = PromptManager()

    assert prompts.list_versions("attribution_reasoning") == (1, 2, 3, 4)
    rendered = prompts.render(
        "attribution_reasoning",
        version=1,
        analysis_period="2026-08-01/2026-08-31",
    )

    assert "分析时间范围：2026-08-01/2026-08-31。" in rendered  # noqa: RUF001
    assert "根据已见的中间结果自主选择下一步工具与参数" in rendered
    assert "confidence" in rendered and "不是质量门禁" in rendered


def test_v2_preserves_causal_direction_and_rejects_failed_call_evidence() -> None:
    rendered = PromptManager().render("attribution_reasoning", version=2, analysis_period="period")

    assert "报名率稳定不能排除访问量下降" in rendered
    assert "共同发生时间和相同影响范围不能替代跨环节机制证据" in rendered
    assert "调用失败也不证明活动不存在" in rendered
    assert "不能引用外层 /data" in rendered
    assert "conflicting" in rendered and "insufficient" in rendered


def test_attribution_prompt_requires_version_and_exact_declared_variables() -> None:
    prompts = PromptManager()

    with pytest.raises(TypeError):
        prompts.render("attribution_reasoning", analysis_period="period")  # type: ignore[call-arg]
    with pytest.raises(PromptError, match="variables"):
        prompts.render("attribution_reasoning", version=1)
    with pytest.raises(PromptError, match="variables"):
        prompts.render(
            "attribution_reasoning",
            version=1,
            analysis_period="period",
            unexpected="value",
        )

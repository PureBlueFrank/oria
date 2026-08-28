"""V0.1-T07 prompt version contract tests."""

from __future__ import annotations

import pytest

from oria.prompts import PromptManager
from oria.prompts.registry import PromptError

pytestmark = pytest.mark.unit


def test_prompt_requires_explicit_existing_positive_version_and_exact_variables() -> None:
    prompts = PromptManager()

    assert prompts.list_versions("merchant_selection") == (1,)
    rendered = prompts.render(
        "merchant_selection",
        version=1,
        user_request="生成招商建议",
        effective_at="2026-07-15T00:00:00+08:00",
        max_candidates=10,
    )
    assert "生成招商建议" in rendered
    assert "最多推荐 10 家商户" in rendered

    for version in (0, -1, True, 2):
        with pytest.raises(PromptError):
            prompts.render(
                "merchant_selection",
                version=version,
                user_request="生成招商建议",
                effective_at="2026-07-15T00:00:00+08:00",
                max_candidates=10,
            )
    with pytest.raises(PromptError, match="variables"):
        prompts.render(
            "merchant_selection",
            version=1,
            user_request="生成招商建议",
            effective_at="2026-07-15T00:00:00+08:00",
        )
    with pytest.raises(PromptError, match="variables"):
        prompts.render(
            "merchant_selection",
            version=1,
            user_request="生成招商建议",
            effective_at="2026-07-15T00:00:00+08:00",
            max_candidates=10,
            surprise="not declared",
        )

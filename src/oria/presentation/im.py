"""Versioned, bounded IM views: authorized public summaries, never tool/state dumps."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from oria.core.types import ValueModel
from oria.guardrails.output import redact_output_content
from oria.ingress.feishu import CardAction


class IMSection(ValueModel):
    title: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)


class IMAction(ValueModel):
    label: str = Field(min_length=1, max_length=40)
    value: CardAction


class IMViewModel(ValueModel):
    version: Literal[1] = 1
    stage: Literal[
        "received",
        "confirmation",
        "progress",
        "result",
        "waiting_approval",
        "waiting_event",
        "failed",
    ]
    title: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=2000)
    sections: tuple[IMSection, ...] = Field(default=(), max_length=100)
    detail_ref: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    next_step: str = Field(min_length=1, max_length=500)
    actions: tuple[IMAction, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def actions_require_confirmation(self) -> IMViewModel:
        if self.actions and self.stage not in {"confirmation", "waiting_approval"}:
            raise ValueError("actions require confirmation or approval view")
        return self


def render_card(view: IMViewModel, *, page: int = 1, shared_update: bool = False) -> dict[str, Any]:
    """Render five sections per page; caller reauthorizes detail/pagination access."""
    pages = max(1, (len(view.sections) + 4) // 5)
    if not 1 <= page <= pages:
        raise ValueError("invalid page")

    def text(value: str) -> dict[str, str]:
        return {"tag": "plain_text", "content": str(redact_output_content(value))}

    elements: list[dict[str, Any]] = [{"tag": "div", "text": text(view.summary)}]
    for section in view.sections[(page - 1) * 5 : page * 5]:
        elements.append({"tag": "div", "text": text(f"{section.title}\n{section.text}")})
    elements.append({"tag": "div", "text": text(view.next_step)})
    elements.append(
        {"tag": "note", "elements": [text(f"第 {page}/{pages} 页 · 详情编号 {view.detail_ref}")]}
    )
    if view.actions:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {"tag": "button", "text": text(item.label), "value": item.value.model_dump()}
                    for item in view.actions
                ],
            }
        )
    config = {"wide_screen_mode": True}
    if shared_update:
        config["update_multi"] = True
    return {
        "config": config,
        "header": {"title": text(view.title)},
        "elements": elements,
    }

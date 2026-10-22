"""Versioned, bounded IM views: authorized public summaries, never tool/state dumps."""

from __future__ import annotations

import html
import re
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
    card_schema: Literal["1.0", "2.0"] = "1.0"
    page: int = Field(default=1, ge=1, le=20, strict=True)
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
    rejection_action: CardAction | None = None
    navigation: tuple[IMAction, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def actions_require_confirmation(self) -> IMViewModel:
        if (self.actions or self.rejection_action) and self.stage not in {
            "confirmation",
            "waiting_approval",
        }:
            raise ValueError("actions require confirmation or approval view")
        if self.rejection_action is not None and self.card_schema != "2.0":
            raise ValueError("rejection form requires card schema 2.0")
        return self


def render_card(
    view: IMViewModel, *, page: int | None = None, shared_update: bool = False
) -> dict[str, Any]:
    """Render five sections per page; caller reauthorizes detail/pagination access."""
    pages = max(1, (len(view.sections) + 4) // 5)
    page = view.page if page is None else page
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
    if view.actions or view.navigation:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": text(item.label),
                        "value": item.value.model_dump(exclude_none=True),
                    }
                    for item in (*view.actions, *view.navigation)
                ],
            }
        )
    config = {"wide_screen_mode": True}
    if shared_update:
        config["update_multi"] = True
    if view.card_schema == "2.0":
        # Form containers require JSON 2.0; keep the existing JSON 1.0 goldens intact.
        body: list[dict[str, Any]] = []
        for element in elements:
            if element["tag"] == "action":
                for button in element["actions"]:
                    body.append(
                        {
                            "tag": "button",
                            "text": button["text"],
                            "behaviors": [{"type": "callback", "value": button["value"]}],
                        }
                    )
            else:
                content = (element["elements"][0] if element["tag"] == "note" else element["text"])[
                    "content"
                ]
                content = re.sub(r"([\\`*_\[\]()!#|])", r"\\\1", html.escape(content, quote=False))
                body.append({"tag": "markdown", "content": content})
        if view.rejection_action is not None:
            body.append(
                {
                    "tag": "form",
                    "name": "approval_rejection",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "rejection_reason",
                            "required": True,
                            "input_type": "multiline_text",
                            "max_length": 1000,
                            "placeholder": text("请说明拒绝原因 (最多 1000 字, 请勿填写敏感信息)"),
                        },
                        {
                            "tag": "button",
                            "name": "reject_approval",
                            "text": text("拒绝并提交理由"),
                            "type": "danger",
                            "form_action_type": "submit",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": view.rejection_action.model_dump(exclude_none=True),
                                }
                            ],
                        },
                    ],
                }
            )
        return {
            "schema": "2.0",
            "config": {"update_multi": shared_update},
            "header": {"title": text(view.title)},
            "body": {"elements": body},
        }
    return {
        "config": config,
        "header": {"title": text(view.title)},
        "elements": elements,
    }

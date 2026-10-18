"""Build a clearly labelled walkthrough from verified real Feishu response records."""

# ruff: noqa: RUF001

from __future__ import annotations

import json
from pathlib import Path

from oria.chat.guided_demo import BOUNDARY
from oria.presentation.im import IMViewModel, render_card

REPORT = Path("reports/verification/v0.6/20261018-feishu-conversation-demo")
DESTINATION = Path("docs/demo/feishu")


def main() -> None:
    source = json.loads((REPORT / "real-conversation-views.json").read_text())
    frames = {frame["sequence"]: frame for frame in source["frames"]}

    def card(sequence: int, *, focus: str | tuple[str, ...] | None = None) -> dict[str, object]:
        frame = frames[sequence]
        view = IMViewModel.model_validate(frame["view"])
        wire = render_card(view, page=frame["canonical_input"]["page"])
        elements = [item["text"]["content"] for item in wire["elements"] if item["tag"] == "div"]
        sections = []
        full_sections = []
        for text in elements[1:-1]:
            title, _, body = text.partition("\n")
            full_sections.append({"title": title, "text": body})
            if focus is None or title == focus or (isinstance(focus, tuple) and title in focus):
                sections.append({"title": title, "text": body})
        aliases = {
            "help": "帮助",
            "start": "确认发起演示",
            "approve": "演示批准",
            "enroll": "模拟商家报名",
            "close": "模拟报名结束",
            "confirm": "演示确认",
            "selection": "模拟选品完成",
            "attribution": "归因示例",
            "conflict": "证据冲突示例",
            "insufficient": "证据不足示例",
            "rules": "规则",
            "page": "第2页",
        }
        command = frame["canonical_input"]
        return {
            "sourceSequence": sequence,
            "command": command["slots"] or aliases.get(command["kind"], command["kind"]),
            "title": wire["header"]["title"]["content"].removeprefix("【演示】"),
            "summary": elements[0].removeprefix(BOUNDARY + "\n\n"),
            "sections": sections,
            "fullSections": full_sections,
            "nextStep": elements[-1],
        }

    scenes = [
        {
            "label": "一览",
            "title": "在飞书里，走完一次招商流程",
            "text": "从需求追问到双审批、报名、动态确认、选品与投放通知，再到经营异常归因。",
            "fact": "真实群聊 · 20 条指令成功投递",
            "cards": [card(1)],
        },
        {
            "label": "01 · 需求澄清",
            "title": "先问清楚，再确认发起",
            "text": "实际对话逐项补齐类目、区域、报名模式与候选数量；确认后启动核心工作流。",
            "fact": "餐饮 / 华东 / 混合报名 / 10 家",
            "cards": [card(6)],
        },
        {
            "label": "02 · 招商审批",
            "title": "草案先停在审批边界",
            "text": (
                "规则检索与商家筛选后进入招商发布审批。演示审批由独立合成角色执行，保留职责分离。"
            ),
            "fact": "需求 → 草案 → 第一次审批",
            "cards": [card(7)],
        },
        {
            "label": "03 · 报名汇聚",
            "title": "把外部事件接回流程",
            "text": "模拟商家报名与自动圈品汇聚；报名结束事件恢复等待中的工作流。",
            "fact": "报名已接收 → 关窗后继续",
            "cards": [card(9), card(10, focus="动态确认链")],
        },
        {
            "label": "04 · 动态确认",
            "title": "按规则，逐级确认",
            "text": "本次合成规则生成商家、销售、销售经理三级确认链。每条指令只处理当前一级。",
            "fact": "商家 → 销售 → 销售经理",
            "cards": [
                card(10, focus="动态确认链"),
                card(11, focus="动态确认链"),
                card(12, focus="动态确认链"),
            ],
            "compact": True,
        },
        {
            "label": "05 · 选品与二次审批",
            "title": "选品完成，仍需投放审批",
            "text": "逐商品选品决定与完成事件恢复流程，券关联结果随方案进入 C 端发布审批。",
            "fact": "异步等待 → 选品完成 → 第二次审批",
            "cards": [card(14)],
        },
        {
            "label": "06 · 闭环",
            "title": "完成投放与商家通知",
            "text": (
                "本次核心流程已到 10/10。模拟投放 published，模拟商家通知 sent，"
                "两次审批均由不同于发起人的角色决定。"
            ),
            "fact": "10/10 完成 · 2 次审批职责分离",
            "cards": [card(15, focus=("模拟投放", "模拟商家通知"))],
        },
        {
            "label": "07 · 经营归因",
            "title": "让解释带着可回查证据",
            "text": (
                "同一群里切换到经营异常归因。受限只读工具提供调查数据，"
                "本片使用已审阅案例的 Mock 回放。"
            ),
            "fact": "有界调查 · 只读工具 · 引用证据",
            "cards": [card(16)],
        },
        {
            "label": "08 · 结论边界",
            "title": "保留冲突，证据不足则弃答",
            "text": "证据相互冲突时保留分歧；证据不足时明确弃答，帮助开发者识别 Agent 的能力边界。",
            "fact": "conflicting / insufficient",
            "cards": [
                card(17, focus=()),
                card(18, focus=()),
            ],
        },
        {
            "label": "09 · 规则复盘",
            "title": "流程结束后，回查规则版本",
            "text": (
                "实际群聊继续查询六类规则并翻页，查看生效信息与来源版本。此处展示实际回复节选。"
            ),
            "fact": "六类规则 · 生效信息 · 来源版本",
            "cards": [card(19, focus="基础信息"), card(20)],
            "rules": True,
        },
    ]
    result = {
        "version": 1,
        "source": source["source"],
        "sourceLabel": "真实飞书群聊运行记录 · Oria 回放界面",
        "boundary": "合成数据 · Mock 模型 / 业务系统 · 演示角色",
        "note": "画面按实际指令及机器人卡片响应重新排版；点击卡片可展开完整内容。",
        "scenes": scenes,
    }
    DESTINATION.mkdir(parents=True, exist_ok=True)
    (DESTINATION / "replay-data.js").write_text(
        "window.ORIA_FEISHU_REPLAY = " + json.dumps(result, ensure_ascii=False, indent=2) + ";\n",
        encoding="utf-8",
    )
    print(f"Generated {len(scenes)} source-linked scenes from {len(frames)} delivered commands.")


if __name__ == "__main__":
    main()

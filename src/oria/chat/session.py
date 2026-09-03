"""Dependency-free interactive session for the local Scenario A workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from oria.chat.router import (
    ActiveInterrupt,
    DecideApproval,
    DecideConfirmation,
    HelpAction,
    IntentRouter,
    LaunchCampaign,
    NewSessionAction,
    QueryStatus,
    QuitAction,
    RouterPrompt,
    UnsupportedInput,
)
from oria.config.models import ResolvedRuntimeConfig
from oria.orchestrator.local_executor import (
    LocalWorkflowResult,
    campaign_admin,
    decide_confirmation,
    decide_local_approval,
    inspect_local_workflow,
    start_local_workflow,
)
from oria.presentation.workflow import render_workflow

InputFunction = Callable[[str], str]
OutputFunction = Callable[[str], None]
IdFactory = Callable[[], str]


class ChatBackend(Protocol):
    """Narrow orchestration seam used by the input/output loop and its tests."""

    def start(
        self,
        config: ResolvedRuntimeConfig,
        *,
        thread_id: str,
        action: LaunchCampaign,
    ) -> LocalWorkflowResult: ...

    def status(self, config: ResolvedRuntimeConfig, *, thread_id: str) -> LocalWorkflowResult: ...

    def decide_approval(
        self,
        config: ResolvedRuntimeConfig,
        *,
        thread_id: str,
        action: DecideApproval,
    ) -> LocalWorkflowResult: ...

    def decide_confirmation(
        self,
        config: ResolvedRuntimeConfig,
        *,
        thread_id: str,
        action: DecideConfirmation,
    ) -> LocalWorkflowResult: ...


class LocalChatBackend:
    """Synchronous adapter over the existing async local executor functions."""

    def start(
        self,
        config: ResolvedRuntimeConfig,
        *,
        thread_id: str,
        action: LaunchCampaign,
    ) -> LocalWorkflowResult:
        return asyncio.run(
            start_local_workflow(
                config,
                thread_id=thread_id,
                campaign_id=action.request.draft.campaign_id,
                user_request=action.request.user_request,
                workflow_request=action.request,
            )
        )

    def status(self, config: ResolvedRuntimeConfig, *, thread_id: str) -> LocalWorkflowResult:
        return asyncio.run(inspect_local_workflow(config, thread_id=thread_id))

    def decide_approval(
        self,
        config: ResolvedRuntimeConfig,
        *,
        thread_id: str,
        action: DecideApproval,
    ) -> LocalWorkflowResult:
        # The chat identity is deliberately fixed to campaign_admin. The existing
        # ApprovalService performs the actual authorization and separation-of-duty check.
        return asyncio.run(
            decide_local_approval(
                config,
                thread_id=thread_id,
                approval_id=action.approval_id,
                decision=action.decision,
                reason="rejected from local chat" if action.decision == "reject" else None,
                decision_actor=campaign_admin(),
            )
        )

    def decide_confirmation(
        self,
        config: ResolvedRuntimeConfig,
        *,
        thread_id: str,
        action: DecideConfirmation,
    ) -> LocalWorkflowResult:
        return asyncio.run(
            decide_confirmation(
                config,
                thread_id=thread_id,
                confirmation_task_id=action.confirmation_task_id,
                decision=action.decision,
            )
        )


class ChatSession:
    """One local chat session with explicit thread and interrupt state."""

    def __init__(
        self,
        config: ResolvedRuntimeConfig,
        *,
        backend: ChatBackend | None = None,
        input_fn: InputFunction = input,
        output_fn: OutputFunction = print,
        id_factory: IdFactory | None = None,
    ) -> None:
        self.config = config
        self.backend = backend or LocalChatBackend()
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.id_factory = id_factory or (lambda: uuid4().hex)
        self.router = IntentRouter()
        self.session_id = f"chat-session-{self.id_factory()}"
        self.thread_id = ""
        self.campaign_id = ""
        self.active_interrupt: ActiveInterrupt | None = None
        self.has_workflow = False
        self._new_thread()

    def run(self) -> None:
        self._write_startup()
        while True:
            try:
                user_text = self.input_fn("oria> ")
            except KeyboardInterrupt:
                self.output_fn("\n已取消当前输入; 已提交的操作不会被撤销。")
                continue
            except EOFError:
                self.output_fn("\n输入已结束。")
                return

            stripped = user_text.strip()
            if stripped.startswith("/switch"):
                self._switch_thread(stripped)
                continue

            action = self.router.route(
                stripped,
                campaign_id=self.campaign_id,
                active_interrupt=self.active_interrupt,
            )
            if isinstance(action, QuitAction):
                self.output_fn("已退出 Oria chat。")
                return
            if isinstance(action, HelpAction):
                self.output_fn(self._help_text())
                continue
            if isinstance(action, NewSessionAction):
                self._new_thread()
                self.output_fn(f"已新建线程: {self.thread_id}")
                continue
            if isinstance(action, (RouterPrompt, UnsupportedInput)):
                self.output_fn(action.message)
                continue
            self._execute(action)

    def _execute(
        self,
        action: LaunchCampaign | QueryStatus | DecideApproval | DecideConfirmation,
    ) -> None:
        try:
            if isinstance(action, LaunchCampaign):
                if self.has_workflow:
                    self.output_fn("当前线程已有工作流; 请先输入 /new, 再发起新活动。")
                    return
                result = self.backend.start(self.config, thread_id=self.thread_id, action=action)
                self.has_workflow = True
            elif isinstance(action, QueryStatus):
                if not self.has_workflow:
                    self.output_fn(
                        "当前线程还没有工作流。请先发起招商活动, 或 /switch 到已有线程。"
                    )
                    return
                result = self.backend.status(self.config, thread_id=self.thread_id)
            elif isinstance(action, DecideApproval):
                result = self.backend.decide_approval(
                    self.config, thread_id=self.thread_id, action=action
                )
            else:
                result = self.backend.decide_confirmation(
                    self.config, thread_id=self.thread_id, action=action
                )
        except PermissionError:
            if isinstance(action, DecideApproval):
                self._write_approval_denial(action)
                self._render_current_status()
            else:
                self.output_fn("权限校验拒绝了本次操作; 当前工作流没有被推进。")
            return
        except (LookupError, RuntimeError, ValueError) as exc:
            self.output_fn(f"操作失败: {exc}")
            return

        self._accept_result(result)

    def _write_approval_denial(self, action: DecideApproval) -> None:
        role = (
            "launch_approver"
            if action.approval_kind == "launch_approval"
            else "consumer_publish_approver"
        )
        decision = "approve" if action.decision == "approve" else "reject"
        reason = "" if action.decision == "approve" else " --reason <reason>"
        self.output_fn(
            "审批被拒绝: 当前可信本地主体 local-campaign-admin (campaign_admin) "
            f"不具备 {role} 角色, chat 不会切换身份或冒充审批人。"
        )
        self.output_fn(
            "请由独立可信审批身份执行: "
            f"oria approval {decision} --thread-id {self.thread_id} "
            f"--approval-id {action.approval_id}{reason}"
        )

    def _render_current_status(self) -> None:
        try:
            result = self.backend.status(self.config, thread_id=self.thread_id)
        except (LookupError, PermissionError, RuntimeError, ValueError) as exc:
            self.output_fn(f"状态读取失败: {exc}")
            return
        self._accept_result(result)

    def _accept_result(self, result: LocalWorkflowResult) -> None:
        self.thread_id = result.thread_id
        self.has_workflow = True
        self.active_interrupt = self._active_from(result)
        self.output_fn(render_workflow(result.view))

    @staticmethod
    def _active_from(result: LocalWorkflowResult) -> ActiveInterrupt | None:
        if not result.interrupts:
            return None
        interruption = result.interrupts[0]
        kind = interruption.get("kind")
        if not isinstance(kind, str):
            return None
        key = {
            "launch_approval": "approval_id",
            "consumer_publish_approval": "approval_id",
            "business_confirmation": "confirmation_task_id",
            "enrollment_window": "wait_id",
            "selection_event": "wait_id",
        }.get(kind)
        identifier = interruption.get(key) if key is not None else None
        if not isinstance(identifier, str):
            return None
        try:
            return ActiveInterrupt.model_validate({"kind": kind, "identifier": identifier})
        except ValueError:
            return None

    def _switch_thread(self, command: str) -> None:
        parts = command.split(maxsplit=1)
        if len(parts) != 2 or not parts[1].strip():
            self.output_fn("用法: /switch <thread-id>")
            return
        candidate = parts[1].strip()
        try:
            result = self.backend.status(self.config, thread_id=candidate)
        except (LookupError, PermissionError, RuntimeError, ValueError) as exc:
            self.output_fn(f"无法切换线程: {exc}")
            return
        self.router.reset()
        self.thread_id = candidate
        self.campaign_id = f"chat-campaign-{self.id_factory()}"
        self._accept_result(result)

    def _new_thread(self) -> None:
        self.router.reset()
        self.thread_id = f"chat-thread-{self.id_factory()}"
        self.campaign_id = f"chat-campaign-{self.id_factory()}"
        self.active_interrupt = None
        self.has_workflow = False

    def _write_startup(self) -> None:
        self.output_fn("Oria chat 已启动。输入 /help 查看可用命令。")
        self.output_fn(f"会话: {self.session_id}")
        self.output_fn(f"线程: {self.thread_id}")
        self.output_fn(f"运行配置: {self.config.edition}+{self.config.runtime_profile}")
        self.output_fn(
            f"模型: {self.config.llm.profile_id} / {self.config.llm.provider} / "
            f"{self.config.llm.model}"
        )
        self.output_fn(
            f"Embedding: {self.config.embedding.profile_id} / {self.config.embedding.provider}"
        )
        self.output_fn(f"数据目录: {self.config.data_dir}")
        self.output_fn("可信本地主体: local-campaign-admin (campaign_admin)")

    def _help_text(self) -> str:
        return "\n".join(
            (
                "自然语言: 发起…招商活动 | 状态 | 批准/拒绝 | 确认/驳回",
                "会话命令: /help | /status | /new | /switch <thread-id> | /quit",
                "Mock 报名事件: oria mock enrollment --thread-id "
                f"{self.thread_id} --source-event-id <event-id>",
                "Mock 关窗事件: oria mock window-close --thread-id "
                f"{self.thread_id} --source-event-id <event-id>",
                "Mock 选品决定/完成: oria mock selection-decision ... / "
                "oria mock selection-complete ...",
                "审批必须由独立 launch_approver 或 consumer_publish_approver 身份执行。",
            )
        )


def run_chat(
    config: ResolvedRuntimeConfig,
    *,
    input_fn: InputFunction = input,
    output_fn: OutputFunction = print,
    backend: ChatBackend | None = None,
    id_factory: IdFactory | None = None,
) -> None:
    """Run the shared loop used by bare ``oria`` and ``oria chat``."""

    ChatSession(
        config,
        backend=backend,
        input_fn=input_fn,
        output_fn=output_fn,
        id_factory=id_factory,
    ).run()

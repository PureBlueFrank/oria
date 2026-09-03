from collections.abc import Iterator
from pathlib import Path

import pytest

import oria.chat.session as session_module
from oria.chat.router import DecideApproval, LaunchCampaign
from oria.chat.session import ChatSession, LocalChatBackend
from oria.config import resolve_runtime_config
from oria.orchestrator.local_executor import LocalWorkflowResult, default_request
from oria.presentation.workflow import MerchantMatches, WorkflowViewModel

pytestmark = pytest.mark.unit


def _result(
    thread_id: str,
    *,
    stage: int,
    interrupt: dict[str, object] | None = None,
    terminal: str | None = None,
) -> LocalWorkflowResult:
    names = (
        "生成规则与活动草案",
        "招商发布审批",
        "券批次物化与招商发布",
        "报名与商品圈选",
        "动态业务确认",
        "报名商品关联券批次",
        "提交并等待招后选品",
        "C 端发布审批",
        "C 端投放",
        "通知商家并闭环",
    )
    view = WorkflowViewModel(
        thread_id=thread_id,
        flow_name="招商活动自动化",
        stage_index=stage,
        stage_total=10,
        current_stage=names[stage - 1],
        completed_steps=names[: stage - 1],
        pending_action="等待当前操作。" if terminal is None else "全部步骤已完成。",
        next_command="oria approval approve ..." if interrupt else None,
        merchant_matches=MerchantMatches(matched_count=1, evaluated_count=1),
        terminal_outcome=terminal,
    )
    return LocalWorkflowResult(
        thread_id=thread_id,
        status="waiting" if interrupt else "completed",
        interrupts=(interrupt,) if interrupt else (),
        view=view,
    )


class _ConversationBackend:
    def __init__(self) -> None:
        self.current: LocalWorkflowResult | None = None
        self.approval_attempts = 0
        self.confirmation_attempts = 0

    def start(self, config: object, *, thread_id: str, action: object) -> LocalWorkflowResult:
        self.current = _result(
            thread_id,
            stage=2,
            interrupt={
                "kind": "launch_approval",
                "approval_id": "approval-launch-a",
                "interrupt_id": "interrupt-a",
            },
        )
        return self.current

    def status(self, config: object, *, thread_id: str) -> LocalWorkflowResult:
        if self.current is None or self.current.thread_id != thread_id:
            raise LookupError("workflow thread is unavailable")
        return self.current

    def decide_approval(
        self, config: object, *, thread_id: str, action: object
    ) -> LocalWorkflowResult:
        self.approval_attempts += 1
        raise PermissionError("separation of duty")

    def decide_confirmation(
        self, config: object, *, thread_id: str, action: object
    ) -> LocalWorkflowResult:
        self.confirmation_attempts += 1
        self.current = _result(thread_id, stage=10, terminal="completed")
        return self.current

    def advance_as_external_approver(self, thread_id: str) -> None:
        self.current = _result(
            thread_id,
            stage=5,
            interrupt={
                "kind": "business_confirmation",
                "confirmation_task_id": "confirmation-a",
                "interrupt_id": "interrupt-b",
            },
        )


def test_injectable_loop_routes_launch_denial_external_approval_and_confirmation(
    tmp_path: Path,
) -> None:
    backend = _ConversationBackend()
    outputs: list[str] = []
    commands: Iterator[str] = iter(
        (
            "发起华东餐饮招商活动, 报名模式 hybrid, 目标 10 家",
            "批准",
            "/status",
            "确认",
            "/quit",
        )
    )

    def scripted_input(prompt: str) -> str:
        value = next(commands)
        if value == "/status":
            backend.advance_as_external_approver(session.thread_id)
        return value

    session = ChatSession(
        resolve_runtime_config(data_dir=tmp_path / "data"),
        backend=backend,
        input_fn=scripted_input,
        output_fn=outputs.append,
        id_factory=iter(("session-id", "thread-id-1", "campaign-id-1")).__next__,
    )
    session.run()

    transcript = "\n".join(outputs)
    assert "可信本地主体: local-campaign-admin (campaign_admin)" in transcript
    assert "当前阶段: 招商发布审批" in transcript
    assert "审批被拒绝" in transcript
    assert "launch_approver" in transcript
    assert "chat 不会切换身份或冒充审批人" in transcript
    assert "当前阶段: 动态业务确认" in transcript
    assert "流程已完成" in transcript
    assert transcript.count("流程进度") >= 4
    assert backend.approval_attempts == 1
    assert backend.confirmation_attempts == 1


def test_local_backend_submits_approval_as_campaign_admin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_roles: tuple[str, ...] | None = None

    async def deny_approval(config: object, **values: object) -> LocalWorkflowResult:
        nonlocal observed_roles
        actor = values["decision_actor"]
        observed_roles = actor.roles  # type: ignore[union-attr]
        raise PermissionError("denied")

    monkeypatch.setattr(session_module, "decide_local_approval", deny_approval)
    request = default_request(campaign_id="campaign-a", user_request="request")
    action = DecideApproval(
        approval_id="approval-a",
        approval_kind="launch_approval",
        decision="approve",
    )

    with pytest.raises(PermissionError, match="denied"):
        LocalChatBackend().decide_approval(
            resolve_runtime_config(data_dir=tmp_path / "data"),
            thread_id="thread-a",
            action=action,
        )

    assert request.draft.campaign_id == "campaign-a"
    assert observed_roles == ("operator", "campaign_admin")


def test_ctrl_c_cancels_only_input_then_session_continues(tmp_path: Path) -> None:
    calls = iter((KeyboardInterrupt(), "/quit"))
    outputs: list[str] = []

    def interrupt_once(prompt: str) -> str:
        value = next(calls)
        if isinstance(value, BaseException):
            raise value
        return value

    ChatSession(
        resolve_runtime_config(data_dir=tmp_path / "data"),
        input_fn=interrupt_once,
        output_fn=outputs.append,
        id_factory=iter(("session", "thread", "campaign")).__next__,
    ).run()

    transcript = "\n".join(outputs)
    assert "已取消当前输入" in transcript
    assert "已提交的操作不会被撤销" in transcript
    assert "已退出 Oria chat" in transcript


def test_launch_action_schema_can_be_passed_to_backend(tmp_path: Path) -> None:
    request = default_request(campaign_id="campaign-a", user_request="request")
    action = LaunchCampaign(
        categories=("餐饮",),
        cities=("华东",),
        enrollment_mode="hybrid",
        target_count=10,
        request=request,
    )

    assert action.request is request

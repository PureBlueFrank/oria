import pytest

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

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("phrase", ["现在到哪了", "状态", "活动进行到哪一步了", "/status"])
def test_routes_status_phrases(phrase: str) -> None:
    assert isinstance(IntentRouter().route(phrase, campaign_id="campaign-a"), QueryStatus)


@pytest.mark.parametrize(
    ("phrase", "expected_type"),
    [("/help", HelpAction), ("/quit", QuitAction), ("/new", NewSessionAction)],
)
def test_routes_control_commands(phrase: str, expected_type: type[object]) -> None:
    assert isinstance(IntentRouter().route(phrase, campaign_id="campaign-a"), expected_type)


def test_launch_phrase_extracts_visible_slots_and_overrides_default_request() -> None:
    action = IntentRouter().route(
        "发起华东餐饮招商活动, 报名模式 hybrid, 目标 12 家",
        campaign_id="campaign-a",
    )

    assert isinstance(action, LaunchCampaign)
    assert action.categories == ("餐饮",)
    assert action.cities == ("华东",)
    assert action.enrollment_mode == "hybrid"
    assert action.target_count == 12
    assert action.request.draft.campaign_id == "campaign-a"
    assert action.request.max_candidates == 12
    assert action.request.enrollment_mode == "hybrid"
    assert action.request.placement_spec["region"] == "华东"


def test_create_phrase_prompts_for_each_missing_slot_without_guessing() -> None:
    router = IntentRouter()

    category = router.route("创建暑期招商活动", campaign_id="campaign-a")
    assert isinstance(category, RouterPrompt)
    assert category.missing_slot == "category"

    city = router.route("餐饮", campaign_id="campaign-a")
    assert isinstance(city, RouterPrompt)
    assert city.missing_slot == "city"

    mode = router.route("上海、杭州", campaign_id="campaign-a")
    assert isinstance(mode, RouterPrompt)
    assert mode.missing_slot == "enrollment_mode"

    still_mode = router.route("随便", campaign_id="campaign-a")
    assert isinstance(still_mode, RouterPrompt)
    assert still_mode.missing_slot == "enrollment_mode"

    count = router.route("merchant", campaign_id="campaign-a")
    assert isinstance(count, RouterPrompt)
    assert count.missing_slot == "target_count"

    action = router.route("8", campaign_id="campaign-a")
    assert isinstance(action, LaunchCampaign)
    assert action.cities == ("上海", "杭州")
    assert action.request.enrollment_mode == "merchant"
    assert action.request.max_candidates == 8


@pytest.mark.parametrize(
    ("phrase", "decision"), [("批准", "approve"), ("通过", "approve"), ("拒绝", "reject")]
)
def test_approval_decisions_require_and_bind_active_interrupt(phrase: str, decision: str) -> None:
    action = IntentRouter().route(
        phrase,
        campaign_id="campaign-a",
        active_interrupt=ActiveInterrupt(kind="launch_approval", identifier="approval-a"),
    )

    assert isinstance(action, DecideApproval)
    assert action.approval_id == "approval-a"
    assert action.decision == decision


@pytest.mark.parametrize(("phrase", "decision"), [("确认", "confirm"), ("驳回", "reject")])
def test_confirmation_decisions_bind_active_task(phrase: str, decision: str) -> None:
    action = IntentRouter().route(
        phrase,
        campaign_id="campaign-a",
        active_interrupt=ActiveInterrupt(kind="business_confirmation", identifier="confirmation-a"),
    )

    assert isinstance(action, DecideConfirmation)
    assert action.confirmation_task_id == "confirmation-a"
    assert action.decision == decision


def test_decisions_without_matching_interrupt_are_not_guessed() -> None:
    approval = IntentRouter().route("批准", campaign_id="campaign-a")
    confirmation = IntentRouter().route("确认", campaign_id="campaign-a")

    assert isinstance(approval, RouterPrompt)
    assert approval.missing_slot == "context"
    assert isinstance(confirmation, RouterPrompt)
    assert confirmation.missing_slot == "context"


def test_unknown_input_returns_help_instead_of_silent_fallback() -> None:
    action = IntentRouter().route("帮我随便处理一下", campaign_id="campaign-a")

    assert isinstance(action, UnsupportedInput)
    assert "/help" in action.message

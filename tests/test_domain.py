from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.enums import MessageEntityType
from aiogram.types import Chat, Message, MessageEntity
from aiogram.types import User as TelegramUser

from consulidate.atlant import AtlantAdapter, FakeAtlantAdapter
from consulidate.config import TOPIC_NAMES, ProjectContextConfig, load_project_context
from consulidate.models import (
    AuthorType,
    DecisionLevel,
    Issue,
    IssueStatus,
)
from consulidate.schemas import AgentPositionOutput, DecisionDraft, IssueDraft, PriorityFactors
from consulidate.service import (
    calculate_priority,
    can_auto_approve,
    format_issue_card,
    format_position,
    is_duplicate_title,
    select_agents,
    transition_issue,
)
from consulidate.telegram import AiogramPublisher, authentic_command
from tests.helpers import make_config


def decision(level: DecisionLevel = DecisionLevel.L1, **overrides) -> DecisionDraft:
    values = {
        "level": level,
        "summary": "Ограниченный тест",
        "rationale": ["Проверяем гипотезу."],
        "success_criteria": ["Есть измеримый результат."],
        "stop_conditions": ["Стоимость выросла."],
        "next_action": "Подготовить тест.",
        "deadline_reason": "Неделя на проверку.",
        "risk": 0.3,
        "reversibility": 0.8,
        "confidence": 0.7,
        "estimated_cost": 10,
    }
    values.update(overrides)
    return DecisionDraft(**values)


def test_priority_formula_and_clamping() -> None:
    factors = PriorityFactors(
        impact=1,
        urgency=0.5,
        inaction_risk=0.25,
        strategic_fit=0.8,
        new_information=0.4,
    )
    assert calculate_priority(factors) == 0.63
    assert calculate_priority({"impact": 2, "urgency": -1}) == 0.3


def test_autonomy_boundaries_and_l2_escalation() -> None:
    config = make_config()
    assert can_auto_approve(decision(), config)
    assert not can_auto_approve(decision(risk=0.301), config)
    assert not can_auto_approve(decision(level=DecisionLevel.L2, risk=0), config)
    assert not can_auto_approve(decision(level=DecisionLevel.L3, risk=0), config)


def test_atlant_structured_output_retries_without_network() -> None:
    config = make_config()
    adapter = AtlantAdapter(config)
    valid = {
        "command": "DECISION",
        **decision().model_dump(mode="json", exclude={"command"}),
    }
    responses = iter([{"command": "DECISION"}, valid])
    attempts = 0
    prompts: list[str] = []

    def request(*_args):
        nonlocal attempts
        attempts += 1
        prompts.append(_args[1])
        return next(responses)

    adapter._request = request  # type: ignore[method-assign]
    result = __import__("asyncio").run(adapter.generate("decide", "chief_of_staff", {}, DecisionDraft))
    assert result.level == DecisionLevel.L1 and attempts == 2
    assert "MineJust — общий контекст совета" in prompts[0]


def test_issue_status_machine() -> None:
    issue = Issue(
        human_id="MJ-001",
        organization_id="00000000-0000-0000-0000-000000000001",
        title="Тест",
        problem="Проблема",
        author_type=AuthorType.HUMAN,
        author_id="1",
        status=IssueStatus.PROPOSED,
        telegram_chat_id=-1,
        telegram_thread_id=1,
    )
    transition_issue(issue, IssueStatus.DEBATING)
    assert issue.status == IssueStatus.DEBATING
    with pytest.raises(ValueError):
        transition_issue(issue, IssueStatus.DRAFT)


def test_agent_selection_is_bounded_and_excludes_non_voters() -> None:
    selected = select_agents(
        "Технический риск, игроки, бюджет и сроки",
        ["chief_of_staff", "research", "coo", "cpo", "cto", "cfo", "risk", "growth"],
        maximum=5,
        author_agent_id="research",
    )
    assert 3 <= len(selected) <= 5
    assert "chief_of_staff" not in selected
    assert "research" not in selected
    assert "risk" in selected


def test_duplicate_detection() -> None:
    assert is_duplicate_title("Эксперимент новой модели AdShare", "Новая модель: эксперимент AdShare")
    assert not is_duplicate_title("Новая модель", "Турнир сообщества")


def test_owner_rights() -> None:
    config = make_config()
    assert 100 in config.file.telegram.owner_ids
    assert 101 not in config.file.telegram.owner_ids
    plain_quote = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=-1, type="supergroup"),
        from_user=TelegramUser(id=100, is_bot=False, first_name="Owner"),
        text="/approve",
    )
    real_command = plain_quote.model_copy(
        update={"entities": [MessageEntity(type=MessageEntityType.BOT_COMMAND, offset=0, length=8)]}
    )
    assert not authentic_command(plain_quote)
    assert authentic_command(real_command)


def test_issue_card_format() -> None:
    issue = Issue(
        human_id="MJ-007",
        organization_id="00000000-0000-0000-0000-000000000001",
        title="Проверка идеи",
        problem="Нет данных.",
        proposal="Провести тест.",
        expected_result="Получить данные.",
        author_type=AuthorType.AGENT,
        author_id="research",
        status=IssueStatus.PROPOSED,
        priority=0.72,
        telegram_chat_id=-1,
        telegram_thread_id=1,
    )
    card = format_issue_card(issue)
    assert "📌 MJ-007 · ПРОВЕРКА ИДЕИ" in card
    assert "Приоритет: 0.72" in card
    assert "ожидает отбора" in card


def test_public_position_keeps_full_bounded_details_and_need_data_requires_questions() -> None:
    position = AgentPositionOutput(
        verdict="revise",
        confidence=0.6,
        summary="Сводка " * 40,
        arguments=[f"Аргумент {number} " * 10 for number in range(4)],
        risks=[f"Риск {number} " * 10 for number in range(4)],
        questions=[f"Вопрос {number}?" for number in range(3)],
        conditions=[f"Условие {number}" for number in range(4)],
    )
    text = format_position("CTO", "⚙️", position)
    assert len(text) < 4_096
    assert "Аргумент 0" in text and "Аргумент 3" in text
    with pytest.raises(ValueError, match="NEED_DATA"):
        AgentPositionOutput(verdict="need_data", confidence=0.3, summary="Не хватает данных.")


def test_aiogram_publisher_uses_native_expandable_quote() -> None:
    class Bot:
        async def send_message(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs
            return SimpleNamespace(message_id=7)

    bot = Bot()
    message_id = __import__("asyncio").run(
        AiogramPublisher(bot).send(-1, 10, "Краткий вывод\n\nПолные детали", expandable=True)  # type: ignore[arg-type]
    )
    assert message_id == 7 and bot.args[1] == "Краткий вывод\n\nПолные детали"
    assert bot.kwargs["entities"][0].type == MessageEntityType.EXPANDABLE_BLOCKQUOTE


def test_topic_contract_and_round_limit() -> None:
    assert len(TOPIC_NAMES) == 9
    assert make_config().file.cycle.max_rounds == 3


def test_markdown_project_context_is_bounded_safe_and_injected(tmp_path) -> None:
    (tmp_path / "a.md").write_text("0123456789", encoding="utf-8")
    (tmp_path / "b.md").write_text("abcdefghij", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("must not be loaded", encoding="utf-8")
    (tmp_path / "outside.md").symlink_to(outside)
    config = make_config()
    file_config = config.file.model_copy(
        update={"project_context": ProjectContextConfig(directory=tmp_path, max_characters=12)}
    )
    config = config.model_copy(update={"file": file_config})

    documents = load_project_context(config)
    assert documents == [
        {"name": "a.md", "content": "0123456789"},
        {"name": "b.md", "content": "ab"},
    ]
    adapter = FakeAtlantAdapter(config)
    __import__("asyncio").run(adapter.generate("extract_issue", "chief_of_staff", {"text": "test"}, IssueDraft))
    assert adapter.contexts[0][2]["project_context"] == documents

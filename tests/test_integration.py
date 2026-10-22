from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from consulidate.atlant import CallBudget, FakeAtlantAdapter
from consulidate.cycle import CycleService
from consulidate.models import (
    AgentPosition,
    AuditLog,
    CyclePhase,
    CycleStatus,
    Decision,
    DecisionStatus,
    Evidence,
    HourlyCycle,
    Issue,
    IssueMessage,
    IssueStatus,
    Task,
)
from consulidate.service import CouncilService, IncomingMessage
from consulidate.telegram import ForumSetup, incoming
from tests.helpers import FakeForumBot, make_stack


def human_message(update_id: int = 1, text: str = "Проверить новую механику") -> IncomingMessage:
    return IncomingMessage(
        update_id=update_id,
        chat_id=-100123,
        thread_id=10,
        message_id=update_id + 100,
        from_user_id=100,
        username="owner",
        display_name="Owner",
        text=text,
    )


def test_setup_creates_and_persists_topics(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await make_stack(tmp_path)
        bot = FakeForumBot()
        setup = ForumSetup(stack.service, bot)
        topics = await setup.run(chat_id=-100123, owner_id=100)
        assert set(topics) == {
            "cabinet",
            "initiatives",
            "portfolio",
            "risks",
            "growth",
            "technical",
            "reports",
            "decisions",
            "archive",
        }
        assert len(bot.created) == 7  # cabinet/initiatives were already configured in DB.
        rebound = await setup.run(chat_id=-100123, owner_id=100, bind_key="risks", current_thread_id=999)
        assert rebound["risks"] == 999
        await stack.close()

    asyncio.run(scenario())


def test_issue_from_telegram_update_and_repeated_update(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await make_stack(tmp_path)
        tg_message = Message(
            message_id=55,
            date=datetime.now(UTC),
            chat=Chat(id=-100123, type="supergroup", is_forum=True),
            from_user=TelegramUser(id=100, is_bot=False, first_name="Owner"),
            message_thread_id=10,
            text="Нужен тест онбординга",
        )
        update = Update(update_id=500, message=tg_message)
        first, created = await stack.service.create_issue_from_message(incoming(tg_message, update))
        second, created_again = await stack.service.create_issue_from_message(incoming(tg_message, update))
        assert created and not created_again and first.id == second.id
        async with stack.service.sessions() as session:
            assert await session.scalar(select(func.count(Issue.id))) == 1
            assert (
                await session.scalar(select(func.count(IssueMessage.id)).where(IssueMessage.telegram_update_id == 500))
                == 1
            )
        await stack.close()

    asyncio.run(scenario())


def test_full_debate_auto_approves_l1_and_creates_task(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await make_stack(tmp_path)
        issue, _ = await stack.service.create_issue_from_message(human_message())
        result = await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        assert result.decision_id and result.positions == 15 and result.tasks_created == 1
        first_round_contexts = [
            context for operation, _role, context in stack.model.contexts if operation == "position"
        ]
        assert len(first_round_contexts) == 5
        assert all(context["public_positions"] == [] for context in first_round_contexts)
        assert any(message["expandable"] for message in stack.publisher.messages)
        calls_before = len(stack.model.calls)
        repeated = await stack.service.run_debate(issue.id, force=False, budget=CallBudget(20, 60_000))
        assert repeated.skipped and len(stack.model.calls) == calls_before
        async with stack.service.sessions() as session:
            decision = await session.get(Decision, result.decision_id)
            positions = (await session.scalars(select(AgentPosition).where(AgentPosition.issue_id == issue.id))).all()
            tasks = (await session.scalars(select(Task).where(Task.issue_id == issue.id))).all()
            assert decision and decision.status == DecisionStatus.APPROVED
            assert len(positions) == 15 and max(position.round for position in positions) == 3
            assert len(tasks) == 1
        await stack.close()

    asyncio.run(scenario())


def test_l2_always_requires_owner(tmp_path: Path) -> None:
    l2 = {
        "command": "DECISION",
        "level": "L2",
        "summary": "Существенно изменить продукт.",
        "rationale": ["Есть потенциал."],
        "dissent": [],
        "conditions": [],
        "success_criteria": ["Метрика выросла."],
        "stop_conditions": ["Метрика упала."],
        "next_action": "Подготовить план изменения.",
        "assignee_hint": None,
        "deadline_reason": "Нужен план владельца.",
        "review_hours": 168,
        "risk": 0.1,
        "reversibility": 1,
        "confidence": 1,
        "estimated_cost": 0,
    }

    async def scenario() -> None:
        stack = await make_stack(tmp_path, responses={"decide": [l2]})
        issue, _ = await stack.service.create_issue_from_message(human_message())
        result = await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        async with stack.service.sessions() as session:
            decision = await session.get(Decision, result.decision_id)
            assert decision and decision.status == DecisionStatus.PROPOSED and decision.requires_owner
            assert await session.scalar(select(func.count(Task.id)).where(Task.issue_id == issue.id)) == 0
        await stack.close()

    asyncio.run(scenario())


def test_human_reply_reaches_specific_agent(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await make_stack(tmp_path)
        issue, _ = await stack.service.create_issue_from_message(human_message())
        await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        async with stack.service.sessions() as session:
            position = await session.scalar(
                select(AgentPosition).where(
                    AgentPosition.issue_id == issue.id,
                    AgentPosition.agent_id == "cto",
                    AgentPosition.round == 1,
                )
            )
        assert position and position.telegram_message_id
        reply = human_message(2, "@CTO откуда эта оценка?")
        reply = IncomingMessage(**{**reply.__dict__, "reply_to_message_id": position.telegram_message_id})
        addressed = await stack.service.record_human_message(reply, issue)
        assert addressed == "cto"
        assert any("CTO" in message[1] for message in stack.model.calls if message[0] == "reply_to_human") is False
        assert ("reply_to_human", "cto") in stack.model.calls
        await stack.close()

    asyncio.run(scenario())


def test_need_data_pauses_debate_and_asks_owner_once(tmp_path: Path) -> None:
    need_data = {
        "command": "POSITION",
        "verdict": "need_data",
        "confidence": 0.4,
        "summary": "Неизвестна текущая базовая конверсия.",
        "arguments": [],
        "risks": ["Без базы эффект теста нельзя измерить."],
        "questions": ["Какова текущая конверсия онбординга за последние 30 дней?"],
        "conditions": [],
        "responds_to_agent_id": None,
    }
    approve = {
        "command": "POSITION",
        "verdict": "approve",
        "confidence": 0.8,
        "summary": "Готов обсуждать после уточнения метрики.",
        "arguments": ["Ограниченный тест обратим."],
        "risks": [],
        "questions": [],
        "conditions": ["Зафиксировать базу."],
        "responds_to_agent_id": None,
    }

    async def scenario() -> None:
        stack = await make_stack(tmp_path, responses={"position": [need_data, approve, approve, approve, approve]})
        issue, _ = await stack.service.create_issue_from_message(human_message())
        first = await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        assert first.decision_id is None and first.requires_owner and first.positions == 5
        assert ("decide", "chief_of_staff") not in stack.model.calls

        async with stack.service.sessions() as session:
            stored = await session.get(Issue, issue.id)
            request = await session.scalar(
                select(IssueMessage).where(IssueMessage.related_type == "owner_context_request")
            )
            assert stored and stored.status == IssueStatus.WAITING_FOR_OWNER
            assert request and "НУЖНЫ ДАННЫЕ ВЛАДЕЛЬЦА" in request.content
            request_message_id = request.telegram_message_id

        repeated = await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        assert repeated.decision_id is None
        async with stack.service.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.action == "owner_context_requested")
                )
                == 1
            )

        reply = human_message(2, "Текущая конверсия — 42% за последние 30 дней.")
        reply = IncomingMessage(**{**reply.__dict__, "reply_to_message_id": request_message_id})
        calls_before_reply = len(stack.model.calls)
        assert await stack.service.record_human_message(reply, issue) is None
        assert len(stack.model.calls) == calls_before_reply
        async with stack.service.sessions() as session:
            stored = await session.get(Issue, issue.id)
            assert stored and stored.status == IssueStatus.PROPOSED

        resumed = await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        assert resumed.decision_id is not None
        await stack.close()

    asyncio.run(scenario())


def test_cycle_silence_and_restart_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await make_stack(tmp_path)
        cycles = CycleService(stack.service)
        empty = await cycles.run(now=datetime(2026, 1, 1, 10, 0, tzinfo=UTC))
        assert empty.status == CycleStatus.COMPLETED and empty.debated == 0
        assert stack.publisher.messages == []
        duplicate_cycle = await cycles.run(now=datetime(2026, 1, 1, 10, 30, tzinfo=UTC))
        assert duplicate_cycle.id == empty.id
        forced_cycle = await cycles.run(
            trigger="manual-force",
            now=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            force_new=True,
        )
        assert forced_cycle.id != empty.id and forced_cycle.status == CycleStatus.COMPLETED
        reports_before = len(stack.publisher.messages)

        issue, _ = await stack.service.create_issue_from_message(human_message(20))
        debate_id = uuid.uuid4()
        async with stack.service.sessions() as session, session.begin():
            stored = await session.get(Issue, issue.id)
            org_id = stored.organization_id
            stored.status = IssueStatus.DEBATING
            stored.active_debate_id = debate_id
            stored.debate_stage = None
            stored.debate_round = 0
            cycle = HourlyCycle(
                organization_id=org_id,
                bucket_key="restart-test",
                status=CycleStatus.RUNNING,
                phase=CyclePhase.DISCUSS,
                main_issue_id=issue.id,
            )
            session.add(cycle)

        restarted_model = FakeAtlantAdapter(stack.config)
        restarted = CouncilService(stack.config, stack.service.sessions, restarted_model, stack.publisher)
        resumed = await CycleService(restarted).resume_incomplete()
        assert len(resumed) == 1 and resumed[0].status == CycleStatus.COMPLETED
        async with stack.service.sessions() as session:
            assert await session.scalar(select(func.count(Decision.id)).where(Decision.issue_id == issue.id)) == 1
        assert reports_before == len(stack.publisher.messages) or len(stack.publisher.messages) > reports_before
        await stack.close()

    asyncio.run(scenario())


def test_owner_approval_of_escalated_decision(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await make_stack(
            tmp_path,
            responses={
                "decide": [
                    {
                        "command": "DECISION",
                        "level": "L2",
                        "summary": "Owner decision",
                        "rationale": ["Material change"],
                        "dissent": [],
                        "conditions": [],
                        "success_criteria": ["Approved result"],
                        "stop_conditions": ["Owner stops"],
                        "next_action": "Write the implementation plan",
                        "assignee_hint": None,
                        "deadline_reason": "Assign after approval",
                        "review_hours": 24,
                        "risk": 0.4,
                        "reversibility": 0.5,
                        "confidence": 0.8,
                        "estimated_cost": 20,
                    }
                ]
            },
        )
        issue, _ = await stack.service.create_issue_from_message(human_message())
        result = await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        decision = await stack.service.approve_decision(issue.id, 100)
        assert decision.id == result.decision_id and decision.status == DecisionStatus.APPROVED
        async with stack.service.sessions() as session:
            assert await session.scalar(select(func.count(Task.id)).where(Task.issue_id == issue.id)) == 1
        await stack.close()

    asyncio.run(scenario())


def test_research_director_creates_complete_deduplicated_issue(tmp_path: Path) -> None:
    research = {
        "command": "RESEARCH",
        "create": True,
        "source": "Наблюдение из отчёта лидера",
        "project_connection": "Онбординг MineJust",
        "expected_benefit": "Больше завершённых регистраций",
        "minimum_test_cost": 1,
        "relevance_now": "Появился новый отчёт",
        "risks": ["Выборка может быть мала"],
        "confidence": 0.75,
        "issue": {
            "command": "ISSUE",
            "title": "Мини-тест нового онбординга",
            "problem": "Новички не завершают онбординг.",
            "proposal": "Проверить короткую подсказку.",
            "reason_now": "Получен свежий отчёт.",
            "expected_result": "Рост завершений.",
            "confidence": 0.75,
            "risk": 0.2,
            "priority_factors": {
                "impact": 0.8,
                "urgency": 0.7,
                "inaction_risk": 0.6,
                "strategic_fit": 0.8,
                "new_information": 1,
            },
        },
    }

    async def scenario() -> None:
        stack = await make_stack(tmp_path, responses={"research": [research]})
        cycle = await CycleService(stack.service).run(now=datetime(2026, 2, 1, 12, 0, tzinfo=UTC))
        assert cycle.status == CycleStatus.COMPLETED and cycle.new_signals == 1
        async with stack.service.sessions() as session:
            issue = await session.scalar(select(Issue).where(Issue.author_id == "research"))
            assert issue is not None
            evidence = await session.scalar(select(Evidence).where(Evidence.issue_id == issue.id))
            assert evidence and "Стоимость минимальной проверки: 1.0" in evidence.content
            assert await session.scalar(select(func.count(Issue.id)).where(Issue.author_id == "research")) == 1
        await stack.close()

    asyncio.run(scenario())


def test_overdue_task_returns_issue_to_review_agenda(tmp_path: Path) -> None:
    async def scenario() -> None:
        stack = await make_stack(tmp_path)
        issue, _ = await stack.service.create_issue_from_message(human_message())
        first = await stack.service.run_debate(issue.id, force=True, budget=CallBudget(20, 60_000))
        async with stack.service.sessions() as session, session.begin():
            task = await session.scalar(select(Task).where(Task.decision_id == first.decision_id))
            assert task is not None
            task.deadline = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
        cycle = await CycleService(stack.service).run(now=datetime(2026, 2, 1, 10, 0, tzinfo=UTC))
        assert cycle.status == CycleStatus.COMPLETED and cycle.reviewed == 1
        async with stack.service.sessions() as session:
            assert await session.scalar(select(func.count(Decision.id)).where(Decision.issue_id == issue.id)) == 2
        await stack.close()

    asyncio.run(scenario())

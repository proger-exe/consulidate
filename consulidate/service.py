from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from consulidate.atlant import BudgetExceeded, CallBudget, ModelAdapter, ModelCallError
from consulidate.config import AppConfig
from consulidate.models import (
    AgentPosition,
    AuditLog,
    AuthorType,
    DebateStage,
    Decision,
    DecisionLevel,
    DecisionStatus,
    Evidence,
    Issue,
    IssueMessage,
    IssueStatus,
    MessageKind,
    Organization,
    PositionVerdict,
    Task,
    TaskStatus,
    TelegramUpdateReceipt,
    User,
    UserRole,
    utcnow,
)
from consulidate.schemas import (
    AgentPositionOutput,
    AgentReply,
    ConflictMap,
    DecisionDraft,
    HumanMessageClassification,
    IssueDraft,
    ParticipantSelection,
    PriorityFactors,
)

logger = logging.getLogger(__name__)


class Publisher(Protocol):
    async def send(
        self,
        chat_id: int,
        thread_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        expandable: bool = False,
    ) -> int: ...


@dataclass(frozen=True)
class IncomingMessage:
    update_id: int
    chat_id: int
    thread_id: int
    message_id: int
    from_user_id: int
    username: str | None
    display_name: str
    text: str
    reply_to_message_id: int | None = None


@dataclass(frozen=True)
class DebateResult:
    issue_id: uuid.UUID
    decision_id: uuid.UUID | None
    positions: int
    tasks_created: int
    requires_owner: bool
    skipped: bool = False


PRIORITY_WEIGHTS = {
    "impact": 0.30,
    "urgency": 0.20,
    "inaction_risk": 0.20,
    "strategic_fit": 0.15,
    "new_information": 0.15,
}


def calculate_priority(factors: PriorityFactors | dict[str, float]) -> float:
    values = factors.model_dump() if isinstance(factors, PriorityFactors) else factors
    return round(
        sum(PRIORITY_WEIGHTS[name] * min(1.0, max(0.0, float(values.get(name, 0)))) for name in PRIORITY_WEIGHTS),
        4,
    )


def can_auto_approve(draft: DecisionDraft, config: AppConfig) -> bool:
    allowed = [DecisionLevel.L0, DecisionLevel.L1, DecisionLevel.L2, DecisionLevel.L3]
    max_level = DecisionLevel(config.file.autonomy.max_decision_level)
    if allowed.index(draft.level) > allowed.index(max_level):
        return False
    if draft.level == DecisionLevel.L0:
        return True
    if draft.level != DecisionLevel.L1:
        return False
    autonomy = config.file.autonomy
    return (
        draft.risk <= autonomy.maximum_risk
        and draft.reversibility >= autonomy.minimum_reversibility
        and draft.confidence >= autonomy.minimum_confidence
        and draft.estimated_cost <= autonomy.max_estimated_cost
    )


TRANSITIONS: dict[IssueStatus, set[IssueStatus]] = {
    IssueStatus.DRAFT: {IssueStatus.PROPOSED, IssueStatus.ARCHIVED},
    IssueStatus.PROPOSED: {
        IssueStatus.COLLECTING_CONTEXT,
        IssueStatus.DEBATING,
        IssueStatus.BLOCKED,
        IssueStatus.REJECTED,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.COLLECTING_CONTEXT: {
        IssueStatus.DEBATING,
        IssueStatus.WAITING_FOR_OWNER,
        IssueStatus.REJECTED,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.DEBATING: {
        IssueStatus.COLLECTING_CONTEXT,
        IssueStatus.WAITING_FOR_OWNER,
        IssueStatus.APPROVED,
        IssueStatus.REJECTED,
        IssueStatus.REVIEW,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.WAITING_FOR_OWNER: {
        IssueStatus.PROPOSED,
        IssueStatus.DEBATING,
        IssueStatus.APPROVED,
        IssueStatus.REJECTED,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.APPROVED: {
        IssueStatus.EXECUTING,
        IssueStatus.WAITING_FOR_OWNER,
        IssueStatus.REVIEW,
        IssueStatus.REJECTED,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.EXECUTING: {
        IssueStatus.BLOCKED,
        IssueStatus.REVIEW,
        IssueStatus.COMPLETED,
        IssueStatus.REJECTED,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.BLOCKED: {
        IssueStatus.DEBATING,
        IssueStatus.EXECUTING,
        IssueStatus.REVIEW,
        IssueStatus.REJECTED,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.REVIEW: {
        IssueStatus.DEBATING,
        IssueStatus.EXECUTING,
        IssueStatus.COMPLETED,
        IssueStatus.REJECTED,
        IssueStatus.ARCHIVED,
    },
    IssueStatus.COMPLETED: {IssueStatus.PROPOSED, IssueStatus.REVIEW, IssueStatus.ARCHIVED},
    IssueStatus.REJECTED: {IssueStatus.PROPOSED, IssueStatus.ARCHIVED},
    IssueStatus.ARCHIVED: {IssueStatus.PROPOSED},
}


def transition_issue(issue: Issue, target: IssueStatus) -> None:
    if target == issue.status:
        return
    if target not in TRANSITIONS[issue.status]:
        raise ValueError(f"invalid Issue transition: {issue.status.value} -> {target.value}")
    issue.status = target


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def title_similarity(left: str, right: str) -> float:
    suffixes = (
        "ого",
        "ему",
        "ами",
        "ями",
        "ая",
        "яя",
        "ое",
        "ее",
        "ой",
        "ей",
        "ам",
        "ям",
        "ах",
        "ях",
        "ы",
        "и",
        "а",
        "я",
        "ь",
    )

    def tokens(text: str) -> set[str]:
        result = set()
        for token in normalize_text(text).split():
            suffix = next((item for item in suffixes if token.endswith(item) and len(token) >= len(item) + 3), "")
            result.add(token[: -len(suffix)] if suffix else token)
        return result

    left_tokens, right_tokens = tokens(left), tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def is_duplicate_title(left: str, right: str, threshold: float = 0.75) -> bool:
    return normalize_text(left) == normalize_text(right) or title_similarity(left, right) >= threshold


def select_agents(
    text: str,
    available: Iterable[str],
    *,
    maximum: int = 5,
    author_agent_id: str | None = None,
) -> list[str]:
    candidates = [role for role in available if role not in {"chief_of_staff", author_agent_id}]
    keywords = {
        "cto": {"тех", "сервер", "код", "архит", "интеграц"},
        "cpo": {"игрок", "продукт", "фич", "пользоват"},
        "coo": {"срок", "команд", "задач", "ресурс", "исполн"},
        "cfo": {"стоим", "день", "час", "бюджет"},
        "community": {"сообщест", "нович", "конфликт", "удержан"},
        "growth": {"рост", "реклам", "конверс", "онборд", "контент"},
        "risk": {"риск", "откат", "инцидент", "безопас"},
        "research": {"исслед", "иде", "технолог", "эксперимент"},
    }
    normalized = normalize_text(text)
    ranked = sorted(
        candidates,
        key=lambda role: (-sum(token in normalized for token in keywords.get(role, set())), role),
    )
    mandatory = [role for role in ("risk", "coo", "cpo") if role in ranked]
    result = mandatory + [role for role in ranked if role not in mandatory]
    return result[:maximum]


def compact_text(text: str, limit: int) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else f"{value[: limit - 1].rstrip()}…"


def format_issue_card(issue: Issue) -> str:
    author = issue.author_id.replace("_", " ").title() if issue.author_type != AuthorType.HUMAN else issue.author_id
    return (
        f"📌 {issue.human_id} · {compact_text(issue.title, 180).upper()}\n\n"
        f"Статус: {issue.status.value}\n"
        f"Приоритет: {issue.priority:.2f}\n"
        f"Автор: {author}\n\n"
        f"Проблема:\n{compact_text(issue.problem, 500)}\n\n"
        f"Предложение:\n{compact_text(issue.proposal, 400) if issue.proposal else '—'}\n\n"
        f"Ожидаемый результат:\n{compact_text(issue.expected_result, 300) if issue.expected_result else '—'}\n\n"
        "Следующий шаг:\nожидает отбора в повестку."
    )


def format_position(role_name: str, emoji: str, position: AgentPositionOutput) -> str:
    visible = [
        f"{emoji} {role_name} · {position.verdict.value.upper()} · {position.confidence:.2f}",
        " ".join(position.summary.split()),
    ]
    details: list[str] = []
    for title, values in (
        ("Аргументы", position.arguments),
        ("Риски", position.risks),
        ("Вопросы", position.questions),
        ("Условия", position.conditions),
    ):
        if values:
            details.extend([f"{title}:", *(f"• {' '.join(value.split())}" for value in values)])
    return "\n".join([*visible, "", *details] if details else visible)


def format_decision(issue: Issue, decision: Decision) -> str:
    owner = "да" if decision.requires_owner else "нет"
    return (
        f"📜 {issue.human_id} · РЕШЕНИЕ {decision.level.value}\n\n"
        f"Статус: {decision.status.value}\nТребует владельца: {owner}\n\n"
        f"{compact_text(decision.summary, 360)}\n\n"
        f"Следующее действие:\n{compact_text(decision.next_action, 320)}\n\n"
        "Критерии успеха:\n"
        + "\n".join(f"• {compact_text(x, 220)}" for x in decision.success_criteria[:2])
        + "\n\nУсловия остановки:\n"
        + "\n".join(f"• {compact_text(x, 220)}" for x in decision.stop_conditions[:2])
    )


def format_owner_questions(issue: Issue, questions: list[str]) -> str:
    return (
        f"👤 НУЖНЫ ДАННЫЕ ВЛАДЕЛЬЦА · {issue.human_id}\n\n"
        "Совет не будет додумывать недостающие факты.\n\n"
        + "\n".join(f"{number}. {compact_text(question, 240)}" for number, question in enumerate(questions, 1))
        + f"\n\nОтветьте на это сообщение. После ответа: /review {issue.human_id}"
    )


class CouncilService:
    def __init__(
        self,
        config: AppConfig,
        sessions: async_sessionmaker[AsyncSession],
        model: ModelAdapter,
        publisher: Publisher | None = None,
    ):
        self.config = config
        self.sessions = sessions
        self.model = model
        self.publisher = publisher

    async def ensure_organization(self) -> Organization:
        org_config = self.config.file.organization
        async with self.sessions() as session, session.begin():
            org = await session.scalar(select(Organization).where(Organization.code == org_config.code))
            if org is None:
                org = Organization(name=org_config.name, code=org_config.code, timezone=org_config.timezone)
                session.add(org)
                await session.flush()
            else:
                org.name, org.timezone = org_config.name, org_config.timezone
            for owner_id in self.config.file.telegram.owner_ids:
                user = await session.scalar(
                    select(User).where(User.organization_id == org.id, User.telegram_user_id == owner_id)
                )
                if user is None:
                    session.add(
                        User(
                            organization_id=org.id,
                            telegram_user_id=owner_id,
                            display_name=str(owner_id),
                            role=UserRole.OWNER,
                        )
                    )
                else:
                    user.role = UserRole.OWNER
            return org

    async def organization(self) -> Organization:
        org = await self.ensure_organization()
        return org

    def is_owner(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.config.file.telegram.owner_ids

    async def ensure_user(self, message: IncomingMessage) -> User:
        org = await self.ensure_organization()
        async with self.sessions() as session, session.begin():
            user = await session.scalar(
                select(User).where(
                    User.organization_id == org.id,
                    User.telegram_user_id == message.from_user_id,
                )
            )
            role = UserRole.OWNER if self.is_owner(message.from_user_id) else UserRole.OBSERVER
            if user is None:
                user = User(
                    organization_id=org.id,
                    telegram_user_id=message.from_user_id,
                    username=message.username,
                    display_name=message.display_name,
                    role=role,
                )
                session.add(user)
                await session.flush()
            else:
                user.username, user.display_name = message.username, message.display_name
                if role == UserRole.OWNER:
                    user.role = role
            return user

    async def claim_update(self, update_id: int) -> bool:
        try:
            async with self.sessions() as session, session.begin():
                receipt = await session.get(TelegramUpdateReceipt, update_id)
                if receipt and receipt.status in {"processing", "done"}:
                    return False
                if receipt:
                    receipt.status, receipt.error = "processing", None
                else:
                    session.add(TelegramUpdateReceipt(update_id=update_id))
                await session.flush()
        except IntegrityError:
            return False
        return True

    async def finish_update(self, update_id: int, error: Exception | None = None) -> None:
        async with self.sessions() as session, session.begin():
            receipt = await session.get(TelegramUpdateReceipt, update_id)
            if receipt:
                receipt.status = "failed" if error else "done"
                receipt.error = str(error)[:1000] if error else None

    async def _audit(
        self,
        session: AsyncSession,
        org_id: uuid.UUID | None,
        action: str,
        actor_type: AuthorType,
        actor_id: str,
        entity_type: str,
        entity_id: str | uuid.UUID,
        payload: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditLog(
                organization_id=org_id,
                action=action,
                actor_type=actor_type,
                actor_id=actor_id,
                entity_type=entity_type,
                entity_id=str(entity_id),
                payload=payload or {},
            )
        )

    async def audit_model_error(self, issue: Issue | None, role: str, error: Exception) -> None:
        async with self.sessions() as session, session.begin():
            await self._audit(
                session,
                issue.organization_id if issue else None,
                "model_call_error",
                AuthorType.SYSTEM,
                role,
                "issue" if issue else "model",
                issue.id if issue else role,
                {"error_type": type(error).__name__, "message": str(error)[:500]},
            )

    async def _fallback_draft(self, text: str) -> IssueDraft:
        cleaned = text.strip() or "Новая инициатива"
        return IssueDraft(
            title=cleaned.splitlines()[0][:120],
            problem=cleaned,
            priority_factors=PriorityFactors(
                impact=0.5,
                urgency=0.5,
                inaction_risk=0.5,
                strategic_fit=0.5,
                new_information=1.0,
            ),
        )

    async def find_duplicate(self, org_id: uuid.UUID, project_id: uuid.UUID | None, title: str) -> Issue | None:
        async with self.sessions() as session:
            issues = (
                await session.scalars(
                    select(Issue).where(
                        Issue.organization_id == org_id,
                        Issue.project_id == project_id,
                        Issue.status.notin_([IssueStatus.REJECTED, IssueStatus.COMPLETED, IssueStatus.ARCHIVED]),
                    )
                )
            ).all()
            return next((issue for issue in issues if is_duplicate_title(title, issue.title)), None)

    async def create_issue_from_message(
        self,
        message: IncomingMessage,
        *,
        source_text: str | None = None,
        project_id: uuid.UUID | None = None,
    ) -> tuple[Issue, bool]:
        org = await self.ensure_organization()
        await self.ensure_user(message)
        async with self.sessions() as session:
            existing = await session.scalar(
                select(Issue)
                .join(IssueMessage, IssueMessage.issue_id == Issue.id)
                .where(IssueMessage.telegram_update_id == message.update_id)
            )
            if existing:
                return existing, False
        text = (source_text or message.text).strip()
        try:
            draft = await self.model.generate("extract_issue", "chief_of_staff", {"text": text}, IssueDraft)
        except (ModelCallError, BudgetExceeded, Exception) as exc:
            logger.warning("issue extraction fallback", extra={"event": "model.error"})
            draft = await self._fallback_draft(text)
            await self.audit_model_error(None, "chief_of_staff", exc)
        duplicate = await self.find_duplicate(org.id, project_id, draft.title)
        if duplicate:
            return duplicate, False
        factors = draft.priority_factors.model_dump()
        async with self.sessions() as session, session.begin():
            locked_org = await session.scalar(select(Organization).where(Organization.id == org.id).with_for_update())
            assert locked_org is not None
            human_id = f"{locked_org.code}-{locked_org.next_issue_number:03d}"
            locked_org.next_issue_number += 1
            issue = Issue(
                human_id=human_id,
                organization_id=org.id,
                project_id=project_id,
                title=draft.title,
                problem=draft.problem,
                proposal=draft.proposal,
                reason_now=draft.reason_now,
                expected_result=draft.expected_result,
                author_type=AuthorType.HUMAN,
                author_id=message.display_name,
                status=IssueStatus.PROPOSED,
                priority=calculate_priority(factors),
                priority_factors=factors,
                confidence=draft.confidence,
                risk=draft.risk,
                telegram_chat_id=message.chat_id,
                telegram_thread_id=message.thread_id,
            )
            session.add(issue)
            await session.flush()
            session.add(
                IssueMessage(
                    issue_id=issue.id,
                    telegram_update_id=message.update_id,
                    telegram_chat_id=message.chat_id,
                    telegram_thread_id=message.thread_id,
                    telegram_message_id=message.message_id,
                    reply_to_message_id=message.reply_to_message_id,
                    author_type=AuthorType.HUMAN,
                    author_id=str(message.from_user_id),
                    kind=MessageKind.ARGUMENT,
                    content=text,
                    related_type="issue_source",
                    related_id=str(issue.id),
                )
            )
            await self._audit(
                session,
                org.id,
                "issue_created",
                AuthorType.HUMAN,
                str(message.from_user_id),
                "issue",
                issue.id,
                {"human_id": human_id},
            )
        if self.publisher:
            try:
                root_id = await self.publisher.send(
                    issue.telegram_chat_id,
                    issue.telegram_thread_id,
                    format_issue_card(issue),
                    reply_to_message_id=message.message_id if message.thread_id == issue.telegram_thread_id else None,
                )
                async with self.sessions() as session, session.begin():
                    stored = await session.get(Issue, issue.id)
                    assert stored is not None
                    stored.telegram_root_message_id = root_id
                    session.add(
                        IssueMessage(
                            issue_id=issue.id,
                            telegram_chat_id=issue.telegram_chat_id,
                            telegram_thread_id=issue.telegram_thread_id,
                            telegram_message_id=root_id,
                            reply_to_message_id=message.message_id,
                            author_type=AuthorType.SYSTEM,
                            author_id="consulidate",
                            kind=MessageKind.SYSTEM,
                            content=format_issue_card(issue),
                            related_type="issue",
                            related_id=str(issue.id),
                        )
                    )
                    issue.telegram_root_message_id = root_id
            except Exception as exc:
                await self.audit_model_error(issue, "telegram", exc)
        return issue, True

    async def create_agent_issue(
        self,
        draft: IssueDraft,
        *,
        author_id: str,
        chat_id: int,
        thread_id: int,
        cycle_id: uuid.UUID | None = None,
    ) -> tuple[Issue, bool]:
        org = await self.ensure_organization()
        duplicate = await self.find_duplicate(org.id, None, draft.title)
        if duplicate:
            return duplicate, False
        async with self.sessions() as session, session.begin():
            locked_org = await session.scalar(select(Organization).where(Organization.id == org.id).with_for_update())
            assert locked_org is not None
            human_id = f"{locked_org.code}-{locked_org.next_issue_number:03d}"
            locked_org.next_issue_number += 1
            issue = Issue(
                human_id=human_id,
                organization_id=org.id,
                title=draft.title,
                problem=draft.problem,
                proposal=draft.proposal,
                reason_now=draft.reason_now,
                expected_result=draft.expected_result,
                author_type=AuthorType.AGENT,
                author_id=author_id,
                status=IssueStatus.PROPOSED,
                priority=calculate_priority(draft.priority_factors),
                priority_factors=draft.priority_factors.model_dump(),
                confidence=draft.confidence,
                risk=draft.risk,
                telegram_chat_id=chat_id,
                telegram_thread_id=thread_id,
            )
            session.add(issue)
            await session.flush()
            await self._audit(
                session,
                org.id,
                "issue_created",
                AuthorType.AGENT,
                author_id,
                "issue",
                issue.id,
                {"human_id": human_id, "cycle_id": str(cycle_id) if cycle_id else None},
            )
        if self.publisher:
            root = await self.publisher.send(chat_id, thread_id, format_issue_card(issue))
            async with self.sessions() as session, session.begin():
                stored = await session.get(Issue, issue.id)
                assert stored is not None
                stored.telegram_root_message_id = root
                session.add(
                    IssueMessage(
                        issue_id=issue.id,
                        telegram_chat_id=chat_id,
                        telegram_thread_id=thread_id,
                        telegram_message_id=root,
                        author_type=AuthorType.AGENT,
                        author_id=author_id,
                        kind=MessageKind.SYSTEM,
                        content=format_issue_card(issue),
                        related_type="issue",
                        related_id=str(issue.id),
                    )
                )
            issue.telegram_root_message_id = root
        return issue, True

    async def resolve_issue(
        self, chat_id: int, *, human_id: str | None = None, reply_to_message_id: int | None = None
    ) -> Issue | None:
        async with self.sessions() as session:
            if human_id:
                return await session.scalar(
                    select(Issue).where(
                        Issue.telegram_chat_id == chat_id, func.upper(Issue.human_id) == human_id.upper()
                    )
                )
            if reply_to_message_id is None:
                return None
            issue = await session.scalar(
                select(Issue).where(
                    Issue.telegram_chat_id == chat_id,
                    Issue.telegram_root_message_id == reply_to_message_id,
                )
            )
            if issue:
                return issue
            return await session.scalar(
                select(Issue)
                .join(IssueMessage, IssueMessage.issue_id == Issue.id)
                .where(
                    IssueMessage.telegram_chat_id == chat_id,
                    IssueMessage.telegram_message_id == reply_to_message_id,
                )
            )

    async def _context(self, issue_id: uuid.UUID) -> dict[str, Any]:
        async with self.sessions() as session:
            issue = await session.get(Issue, issue_id)
            assert issue is not None
            messages = (
                await session.scalars(
                    select(IssueMessage)
                    .where(IssueMessage.issue_id == issue_id)
                    .order_by(IssueMessage.created_at.desc())
                    .limit(20)
                )
            ).all()
            evidence = (
                await session.scalars(
                    select(Evidence).where(Evidence.issue_id == issue_id).order_by(Evidence.created_at.desc()).limit(20)
                )
            ).all()
            decisions = (
                await session.scalars(
                    select(Decision).where(Decision.issue_id == issue_id).order_by(Decision.created_at.desc()).limit(5)
                )
            ).all()
            return {
                "issue": {
                    "human_id": issue.human_id,
                    "title": issue.title,
                    "problem": issue.problem,
                    "proposal": issue.proposal,
                    "reason_now": issue.reason_now,
                    "expected_result": issue.expected_result,
                    "status": issue.status.value,
                    "priority": issue.priority,
                    "risk": issue.risk,
                },
                "human_messages": [
                    {"author": m.author_id, "kind": m.kind.value, "content": m.content} for m in reversed(messages)
                ],
                "evidence": [{"id": str(e.id), "source": e.source, "content": e.content} for e in evidence],
                "prior_decisions": [
                    {"level": d.level.value, "status": d.status.value, "summary": d.summary} for d in decisions
                ],
            }

    async def context_hash(self, issue_id: uuid.UUID) -> str:
        async with self.sessions() as session:
            issue = await session.get(Issue, issue_id)
            assert issue is not None
            messages = (
                await session.scalars(
                    select(IssueMessage)
                    .where(
                        IssueMessage.issue_id == issue_id,
                        IssueMessage.author_type == AuthorType.HUMAN,
                    )
                    .order_by(IssueMessage.created_at)
                )
            ).all()
            evidence = (
                await session.scalars(
                    select(Evidence).where(Evidence.issue_id == issue_id).order_by(Evidence.created_at)
                )
            ).all()
            tasks = (
                await session.scalars(select(Task).where(Task.issue_id == issue_id).order_by(Task.updated_at))
            ).all()
        context = {
            "issue": {
                "title": issue.title,
                "problem": issue.problem,
                "proposal": issue.proposal,
                "risk": issue.risk,
                "status": issue.status.value,
            },
            "human_messages": [(message.content, message.created_at) for message in messages],
            "evidence": [(item.source, item.content, item.created_at) for item in evidence],
            "tasks": [(task.status.value, task.deadline, task.updated_at) for task in tasks],
        }
        raw = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def _store_position(
        self,
        issue: Issue,
        debate_id: uuid.UUID,
        role_id: str,
        round_number: int,
        output: AgentPositionOutput,
    ) -> AgentPosition:
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(
                select(AgentPosition).where(
                    AgentPosition.debate_id == debate_id,
                    AgentPosition.agent_id == role_id,
                    AgentPosition.round == round_number,
                )
            )
            if existing:
                return existing
            previous_arguments = (
                await session.scalars(select(AgentPosition.arguments).where(AgentPosition.debate_id == debate_id))
            ).all()
            seen = {normalize_text(argument) for group in previous_arguments for argument in group}
            arguments = [arg for arg in output.arguments if normalize_text(arg) not in seen]
            responds_to = None
            if output.responds_to_agent_id:
                responds_to = await session.scalar(
                    select(AgentPosition)
                    .where(
                        AgentPosition.debate_id == debate_id,
                        AgentPosition.agent_id == output.responds_to_agent_id,
                    )
                    .order_by(AgentPosition.round.desc())
                )
            position = AgentPosition(
                debate_id=debate_id,
                agent_id=role_id,
                issue_id=issue.id,
                round=round_number,
                verdict=output.verdict,
                confidence=output.confidence,
                summary=output.summary,
                arguments=arguments,
                risks=output.risks,
                questions=output.questions,
                conditions=output.conditions,
                responds_to_position_id=responds_to.id if responds_to else None,
            )
            session.add(position)
            await session.flush()
            await self._audit(
                session,
                issue.organization_id,
                "agent_position",
                AuthorType.AGENT,
                role_id,
                "agent_position",
                position.id,
                {"round": round_number, "verdict": output.verdict.value},
            )
        if self.publisher:
            role = self.config.roles[role_id]
            position_text = format_position(role.name, role.emoji, output)
            message_id = await self.publisher.send(
                issue.telegram_chat_id,
                issue.telegram_thread_id,
                position_text,
                reply_to_message_id=issue.telegram_root_message_id,
                expandable=True,
            )
            async with self.sessions() as session, session.begin():
                stored = await session.get(AgentPosition, position.id)
                assert stored is not None
                stored.telegram_message_id = message_id
                session.add(
                    IssueMessage(
                        issue_id=issue.id,
                        telegram_chat_id=issue.telegram_chat_id,
                        telegram_thread_id=issue.telegram_thread_id,
                        telegram_message_id=message_id,
                        reply_to_message_id=issue.telegram_root_message_id,
                        author_type=AuthorType.AGENT,
                        author_id=role_id,
                        kind=MessageKind.AGENT_RESPONSE,
                        addressed_agent_id=role_id,
                        content=position_text,
                        related_type="agent_position",
                        related_id=str(position.id),
                    )
                )
                position.telegram_message_id = message_id
        return position

    async def _positions(self, debate_id: uuid.UUID, round_number: int | None = None) -> list[AgentPosition]:
        async with self.sessions() as session:
            query = select(AgentPosition).where(AgentPosition.debate_id == debate_id)
            if round_number is not None:
                query = query.where(AgentPosition.round == round_number)
            return list((await session.scalars(query.order_by(AgentPosition.round, AgentPosition.agent_id))).all())

    async def _await_owner_context(
        self,
        issue: Issue,
        debate_id: uuid.UUID,
        positions: list[AgentPosition],
    ) -> bool:
        context_requests = [
            position
            for position in positions
            if position.verdict == PositionVerdict.NEED_DATA
            or (position.questions and position.confidence < self.config.file.autonomy.minimum_confidence)
        ]
        revision_requests = [
            position for position in positions if position.verdict == PositionVerdict.REVISE and position.questions
        ]
        if not context_requests and len(revision_requests) >= 2:
            context_requests = revision_requests
        if not context_requests:
            return False

        async with self.sessions() as session:
            request = await session.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "owner_context_requested",
                    AuditLog.entity_id == str(debate_id),
                )
                .order_by(AuditLog.created_at.desc())
            )
            if request:
                human_reply = await session.scalar(
                    select(IssueMessage.id)
                    .where(
                        IssueMessage.issue_id == issue.id,
                        IssueMessage.author_type == AuthorType.HUMAN,
                        IssueMessage.created_at > request.created_at,
                    )
                    .limit(1)
                )
                new_evidence = await session.scalar(
                    select(Evidence.id)
                    .where(Evidence.issue_id == issue.id, Evidence.created_at > request.created_at)
                    .limit(1)
                )
                if human_reply or new_evidence:
                    return False

        questions: list[str] = []
        seen: set[str] = set()
        for position in context_requests:
            role = self.config.roles.get(position.agent_id)
            for question in position.questions:
                key = normalize_text(question)
                if key in seen:
                    continue
                seen.add(key)
                questions.append(f"{role.name if role else position.agent_id}: {question}")
                if len(questions) == 5:
                    break
            if len(questions) == 5:
                break
        if not questions:
            questions = ["Каких проверяемых исходных данных сейчас не хватает для решения?"]

        message_id: int | None = None
        text = format_owner_questions(issue, questions)
        if request is None and self.publisher:
            message_id = await self.publisher.send(
                issue.telegram_chat_id,
                issue.telegram_thread_id,
                text,
                reply_to_message_id=issue.telegram_root_message_id,
            )
        async with self.sessions() as session, session.begin():
            stored = await session.scalar(select(Issue).where(Issue.id == issue.id).with_for_update())
            assert stored is not None
            transition_issue(stored, IssueStatus.WAITING_FOR_OWNER)
            stored.debate_stage = DebateStage.QUESTION
            stored.debate_round = 1
            if request is None:
                await self._audit(
                    session,
                    issue.organization_id,
                    "owner_context_requested",
                    AuthorType.SYSTEM,
                    "chief_of_staff",
                    "debate",
                    debate_id,
                    {"questions": questions},
                )
                if message_id is not None:
                    session.add(
                        IssueMessage(
                            issue_id=issue.id,
                            telegram_chat_id=issue.telegram_chat_id,
                            telegram_thread_id=issue.telegram_thread_id,
                            telegram_message_id=message_id,
                            reply_to_message_id=issue.telegram_root_message_id,
                            author_type=AuthorType.SYSTEM,
                            author_id="chief_of_staff",
                            kind=MessageKind.QUESTION,
                            content=text,
                            related_type="owner_context_request",
                            related_id=str(debate_id),
                        )
                    )
        return True

    async def _set_stage(self, issue_id: uuid.UUID, stage: DebateStage, round_number: int) -> Issue:
        async with self.sessions() as session, session.begin():
            issue = await session.scalar(select(Issue).where(Issue.id == issue_id).with_for_update())
            assert issue is not None
            issue.debate_stage, issue.debate_round = stage, round_number
            await self._audit(
                session,
                issue.organization_id,
                "debate_stage",
                AuthorType.SYSTEM,
                "chief_of_staff",
                "issue",
                issue.id,
                {"stage": stage.value, "round": round_number},
            )
            return issue

    async def _generate_position(
        self,
        issue: Issue,
        role_id: str,
        operation: str,
        round_number: int,
        debate_id: uuid.UUID,
        budget: CallBudget | None,
        extra: dict[str, Any] | None = None,
    ) -> AgentPosition | None:
        existing = [p for p in await self._positions(debate_id, round_number) if p.agent_id == role_id]
        if existing:
            return existing[0]
        context = await self._context(issue.id)
        prior = await self._positions(debate_id)
        visible_positions = (
            [position for position in prior if position.round < round_number] if operation == "position" else prior
        )
        context["public_positions"] = [
            {
                "id": str(p.id),
                "agent_id": p.agent_id,
                "round": p.round,
                "verdict": p.verdict.value,
                "summary": p.summary,
                "arguments": p.arguments,
                "risks": p.risks,
                "questions": p.questions,
                "conditions": p.conditions,
            }
            for p in visible_positions
        ]
        if extra:
            context.update(extra)
        try:
            output = await self.model.generate(operation, role_id, context, AgentPositionOutput, budget=budget)
            return await self._store_position(issue, debate_id, role_id, round_number, output)
        except BudgetExceeded:
            raise
        except Exception as exc:
            await self.audit_model_error(issue, role_id, exc)
            return None

    async def _save_decision(
        self,
        issue: Issue,
        debate_id: uuid.UUID,
        draft: DecisionDraft,
        cycle_id: uuid.UUID | None,
    ) -> tuple[Decision, int]:
        key = f"debate:{debate_id}"
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(select(Decision).where(Decision.idempotency_key == key))
            if existing:
                task_exists = await session.scalar(select(func.count(Task.id)).where(Task.decision_id == existing.id))
                return existing, int(task_exists or 0)
            old = (
                await session.scalars(
                    select(Decision).where(
                        Decision.issue_id == issue.id,
                        Decision.status != DecisionStatus.SUPERSEDED,
                    )
                )
            ).all()
            for prior in old:
                prior.status = DecisionStatus.SUPERSEDED
                prior_task = await session.scalar(select(Task).where(Task.decision_id == prior.id))
                if prior_task and prior_task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED}:
                    prior_task.status = TaskStatus.CANCELLED
            auto = can_auto_approve(draft, self.config)
            decision = Decision(
                issue_id=issue.id,
                cycle_id=cycle_id,
                debate_id=debate_id,
                idempotency_key=key,
                level=draft.level,
                status=DecisionStatus.APPROVED if auto else DecisionStatus.PROPOSED,
                summary=draft.summary,
                rationale=draft.rationale,
                dissent=draft.dissent,
                conditions=draft.conditions,
                success_criteria=draft.success_criteria,
                stop_conditions=draft.stop_conditions,
                next_action=draft.next_action,
                assignee_hint=draft.assignee_hint,
                deadline_reason=draft.deadline_reason,
                risk=draft.risk,
                reversibility=draft.reversibility,
                confidence=draft.confidence,
                estimated_cost=draft.estimated_cost,
                requires_owner=not auto,
                review_at=utcnow() + timedelta(hours=draft.review_hours),
            )
            session.add(decision)
            await session.flush()
            stored_issue = await session.scalar(select(Issue).where(Issue.id == issue.id).with_for_update())
            assert stored_issue is not None
            transition_issue(stored_issue, IssueStatus.APPROVED if auto else IssueStatus.WAITING_FOR_OWNER)
            await self._audit(
                session,
                issue.organization_id,
                "decision_proposed",
                AuthorType.SYSTEM,
                "chief_of_staff",
                "decision",
                decision.id,
                {"level": draft.level.value, "auto_approved": auto},
            )
            if auto:
                await self._audit(
                    session,
                    issue.organization_id,
                    "decision_approved",
                    AuthorType.SYSTEM,
                    "autonomy_policy",
                    "decision",
                    decision.id,
                    {"level": draft.level.value},
                )
            task_count = 0
            if auto:
                task = Task(
                    issue_id=issue.id,
                    decision_id=decision.id,
                    title=draft.next_action[:500],
                    description=draft.next_action,
                    status=TaskStatus.OPEN,
                    completion_criteria=draft.success_criteria,
                )
                session.add(task)
                await session.flush()
                transition_issue(stored_issue, IssueStatus.WAITING_FOR_OWNER)
                await self._audit(
                    session,
                    issue.organization_id,
                    "task_created",
                    AuthorType.SYSTEM,
                    "chief_of_staff",
                    "task",
                    task.id,
                    {"unassigned": True},
                )
                task_count = 1
        if self.publisher:
            message_id = await self.publisher.send(
                issue.telegram_chat_id,
                issue.telegram_thread_id,
                format_decision(issue, decision),
                reply_to_message_id=issue.telegram_root_message_id,
            )
            async with self.sessions() as session, session.begin():
                stored = await session.get(Decision, decision.id)
                assert stored is not None
                stored.telegram_message_id = message_id
                session.add(
                    IssueMessage(
                        issue_id=issue.id,
                        telegram_chat_id=issue.telegram_chat_id,
                        telegram_thread_id=issue.telegram_thread_id,
                        telegram_message_id=message_id,
                        reply_to_message_id=issue.telegram_root_message_id,
                        author_type=AuthorType.SYSTEM,
                        author_id="chief_of_staff",
                        kind=MessageKind.DECISION,
                        content=format_decision(issue, decision),
                        related_type="decision",
                        related_id=str(decision.id),
                    )
                )
                decision.telegram_message_id = message_id
        return decision, task_count

    async def run_debate(
        self,
        issue_id: uuid.UUID,
        *,
        cycle_id: uuid.UUID | None = None,
        force: bool = False,
        requested_roles: list[str] | None = None,
        budget: CallBudget | None = None,
    ) -> DebateResult:
        current_hash = await self.context_hash(issue_id)
        async with self.sessions() as session, session.begin():
            issue = await session.scalar(select(Issue).where(Issue.id == issue_id).with_for_update())
            if issue is None:
                raise ValueError("issue not found")
            if (
                not force
                and issue.last_debate_context_hash == current_hash
                and issue.debate_stage == DebateStage.DECIDE
            ):
                return DebateResult(issue.id, None, 0, 0, False, skipped=True)
            existing_active_decision = None
            if issue.active_debate_id is not None:
                existing_active_decision = await session.scalar(
                    select(Decision).where(Decision.debate_id == issue.active_debate_id)
                )
            if issue.active_debate_id is None or (
                issue.debate_stage == DebateStage.DECIDE and existing_active_decision is not None
            ):
                issue.active_debate_id = uuid.uuid4()
                issue.debate_participants = []
                issue.debate_stage = DebateStage.PROPOSE
                issue.debate_round = 0
            debate_id = issue.active_debate_id
            if issue.status != IssueStatus.DEBATING:
                transition_issue(issue, IssueStatus.DEBATING)
            await self._audit(
                session,
                issue.organization_id,
                "debate_started",
                AuthorType.SYSTEM,
                "chief_of_staff",
                "issue",
                issue.id,
                {"debate_id": str(debate_id), "forced": force},
            )
        assert debate_id is not None
        async with self.sessions() as session:
            issue = await session.get(Issue, issue_id)
            assert issue is not None
        participants = list(issue.debate_participants or [])
        available = [role_id for role_id, role in self.config.roles.items() if role.enabled]
        if not participants:
            if requested_roles:
                participants = [r for r in requested_roles if r in available and r != "chief_of_staff"]
            else:
                context = await self._context(issue.id)
                candidates = [
                    r
                    for r in available
                    if r not in {"chief_of_staff", issue.author_id if issue.author_type == AuthorType.AGENT else None}
                ]
                try:
                    selection = await self.model.generate(
                        "select_participants",
                        "chief_of_staff",
                        {**context, "available_roles": candidates},
                        ParticipantSelection,
                        budget=budget,
                        cheap=True,
                    )
                    participants = [r for r in selection.roles if r in candidates]
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    await self.audit_model_error(issue, "chief_of_staff", exc)
                    participants = []
            fallback = select_agents(
                f"{issue.title} {issue.problem} {issue.proposal or ''}",
                available,
                maximum=self.config.file.cycle.max_participants,
                author_agent_id=issue.author_id if issue.author_type == AuthorType.AGENT else None,
            )
            participants = list(dict.fromkeys(participants + fallback))[: self.config.file.cycle.max_participants]
            if len(participants) < 3:
                raise ModelCallError("fewer than three eligible debate participants")
            async with self.sessions() as session, session.begin():
                stored = await session.get(Issue, issue.id)
                assert stored is not None
                stored.debate_participants = participants

        if issue.debate_round < 1:
            for role_id in participants:
                await self._generate_position(issue, role_id, "position", 1, debate_id, budget)
            issue = await self._set_stage(issue.id, DebateStage.QUESTION, 1)

        first_positions = await self._positions(debate_id, 1)
        if len(first_positions) < 3:
            async with self.sessions() as session, session.begin():
                stored = await session.get(Issue, issue.id)
                assert stored is not None
                transition_issue(stored, IssueStatus.COLLECTING_CONTEXT)
            return DebateResult(issue.id, None, len(first_positions), 0, True)

        if await self._await_owner_context(issue, debate_id, first_positions):
            return DebateResult(issue.id, None, len(first_positions), 0, True)

        conflict_payload: dict[str, Any]
        async with self.sessions() as session:
            conflict_log = await session.scalar(
                select(AuditLog)
                .where(
                    AuditLog.action == "conflict_map",
                    AuditLog.entity_id == str(debate_id),
                )
                .order_by(AuditLog.created_at.desc())
            )
        if conflict_log:
            conflict_payload = conflict_log.payload
        else:
            try:
                conflict = await self.model.generate(
                    "map_conflicts",
                    "chief_of_staff",
                    {
                        **(await self._context(issue.id)),
                        "public_positions": [
                            {
                                "agent_id": p.agent_id,
                                "verdict": p.verdict.value,
                                "summary": p.summary,
                                "arguments": p.arguments,
                                "risks": p.risks,
                            }
                            for p in first_positions
                        ],
                    },
                    ConflictMap,
                    budget=budget,
                    cheap=True,
                )
                conflict_payload = conflict.model_dump(mode="json")
            except BudgetExceeded:
                raise
            except Exception as exc:
                await self.audit_model_error(issue, "chief_of_staff", exc)
                conflict_payload = {"contradictions": [], "challenged_roles": [], "shared_ground": []}
            async with self.sessions() as session, session.begin():
                await self._audit(
                    session,
                    issue.organization_id,
                    "conflict_map",
                    AuthorType.AGENT,
                    "chief_of_staff",
                    "debate",
                    debate_id,
                    conflict_payload,
                )
        issue = await self._set_stage(issue.id, DebateStage.CHALLENGE, 1)

        if self.config.file.cycle.max_rounds >= 2 and issue.debate_round < 2:
            for role_id in participants:
                await self._generate_position(
                    issue, role_id, "challenge", 2, debate_id, budget, {"conflict_map": conflict_payload}
                )
            issue = await self._set_stage(issue.id, DebateStage.DEFEND, 2)
            issue = await self._set_stage(issue.id, DebateStage.REVISE, 2)

        if self.config.file.cycle.max_rounds >= 3 and issue.debate_round < 3:
            for role_id in participants:
                await self._generate_position(
                    issue, role_id, "revise", 3, debate_id, budget, {"conflict_map": conflict_payload}
                )
            issue = await self._set_stage(issue.id, DebateStage.DECIDE, 3)

        existing_positions = await self._positions(debate_id)
        async with self.sessions() as session:
            existing_decision = await session.scalar(
                select(Decision).where(Decision.idempotency_key == f"debate:{debate_id}")
            )
        if existing_decision:
            task_count = await self.task_count_for_decision(existing_decision.id)
            return DebateResult(
                issue.id, existing_decision.id, len(existing_positions), task_count, existing_decision.requires_owner
            )
        decision_context = await self._context(issue.id)
        decision_context["public_positions"] = [
            {
                "agent_id": p.agent_id,
                "round": p.round,
                "verdict": p.verdict.value,
                "summary": p.summary,
                "arguments": p.arguments,
                "risks": p.risks,
                "questions": p.questions,
                "conditions": p.conditions,
            }
            for p in existing_positions
        ]
        decision_context["conflict_map"] = conflict_payload
        try:
            draft = await self.model.generate(
                "decide", "chief_of_staff", decision_context, DecisionDraft, budget=budget
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            await self.audit_model_error(issue, "chief_of_staff", exc)
            async with self.sessions() as session, session.begin():
                stored = await session.get(Issue, issue.id)
                assert stored is not None
                transition_issue(stored, IssueStatus.COLLECTING_CONTEXT)
            return DebateResult(issue.id, None, len(existing_positions), 0, True)
        decision, task_count = await self._save_decision(issue, debate_id, draft, cycle_id)
        final_hash = await self.context_hash(issue.id)
        async with self.sessions() as session, session.begin():
            stored = await session.get(Issue, issue.id)
            assert stored is not None
            stored.last_debate_context_hash = final_hash
            stored.last_debated_at = utcnow()
        return DebateResult(issue.id, decision.id, len(existing_positions), task_count, decision.requires_owner)

    async def task_count_for_decision(self, decision_id: uuid.UUID) -> int:
        async with self.sessions() as session:
            return int(await session.scalar(select(func.count(Task.id)).where(Task.decision_id == decision_id)) or 0)

    async def latest_decision(self, issue_id: uuid.UUID) -> Decision | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(Decision).where(Decision.issue_id == issue_id).order_by(Decision.created_at.desc())
            )

    async def approve_decision(self, issue_id: uuid.UUID, owner_telegram_id: int) -> Decision:
        if not self.is_owner(owner_telegram_id):
            raise PermissionError("команда доступна только владельцу")
        async with self.sessions() as session, session.begin():
            decision = await session.scalar(
                select(Decision)
                .where(Decision.issue_id == issue_id)
                .order_by(Decision.created_at.desc())
                .with_for_update()
            )
            issue = await session.scalar(select(Issue).where(Issue.id == issue_id).with_for_update())
            if decision is None or issue is None:
                raise ValueError("решение не найдено")
            owner = await session.scalar(
                select(User).where(
                    User.organization_id == issue.organization_id, User.telegram_user_id == owner_telegram_id
                )
            )
            if owner is None:
                raise ValueError("владелец не инициализирован")
            if decision.status not in {DecisionStatus.PROPOSED, DecisionStatus.APPROVED}:
                raise ValueError("решение нельзя утвердить в текущем статусе")
            decision.status = DecisionStatus.APPROVED
            decision.requires_owner = False
            decision.approved_by = owner.id
            if issue.status != IssueStatus.APPROVED:
                transition_issue(issue, IssueStatus.APPROVED)
            task = await session.scalar(select(Task).where(Task.decision_id == decision.id))
            if task is None:
                task = Task(
                    issue_id=issue.id,
                    decision_id=decision.id,
                    title=decision.next_action[:500],
                    description=decision.next_action,
                    completion_criteria=decision.success_criteria,
                )
                session.add(task)
                await session.flush()
                await self._audit(
                    session,
                    issue.organization_id,
                    "task_created",
                    AuthorType.HUMAN,
                    str(owner_telegram_id),
                    "task",
                    task.id,
                )
            transition_issue(
                issue, IssueStatus.WAITING_FOR_OWNER if task.assignee_user_id is None else IssueStatus.EXECUTING
            )
            await self._audit(
                session,
                issue.organization_id,
                "decision_approved",
                AuthorType.HUMAN,
                str(owner_telegram_id),
                "decision",
                decision.id,
            )
            return decision

    async def set_decision_status(
        self, issue_id: uuid.UUID, owner_telegram_id: int, status: DecisionStatus
    ) -> Decision:
        if not self.is_owner(owner_telegram_id):
            raise PermissionError("команда доступна только владельцу")
        if status not in {DecisionStatus.REJECTED, DecisionStatus.VETOED}:
            raise ValueError("unsupported decision status")
        async with self.sessions() as session, session.begin():
            decision = await session.scalar(
                select(Decision)
                .where(Decision.issue_id == issue_id)
                .order_by(Decision.created_at.desc())
                .with_for_update()
            )
            issue = await session.scalar(select(Issue).where(Issue.id == issue_id).with_for_update())
            if decision is None or issue is None:
                raise ValueError("решение не найдено")
            decision.status = status
            if issue.status != IssueStatus.REJECTED:
                transition_issue(issue, IssueStatus.REJECTED)
            tasks = (await session.scalars(select(Task).where(Task.issue_id == issue.id))).all()
            for task in tasks:
                task.status = TaskStatus.CANCELLED
            await self._audit(
                session,
                issue.organization_id,
                "decision_vetoed" if status == DecisionStatus.VETOED else "decision_rejected",
                AuthorType.HUMAN,
                str(owner_telegram_id),
                "decision",
                decision.id,
            )
            return decision

    async def set_issue_status(self, issue_id: uuid.UUID, actor_telegram_id: int, status: IssueStatus) -> Issue:
        if not self.is_owner(actor_telegram_id):
            raise PermissionError("команда доступна только владельцу")
        async with self.sessions() as session, session.begin():
            issue = await session.scalar(select(Issue).where(Issue.id == issue_id).with_for_update())
            if issue is None:
                raise ValueError("инициатива не найдена")
            transition_issue(issue, status)
            if status == IssueStatus.PROPOSED:
                issue.active_debate_id = None
                issue.debate_stage = None
                issue.debate_round = 0
                issue.debate_participants = []
            await self._audit(
                session,
                issue.organization_id,
                "issue_status_changed",
                AuthorType.HUMAN,
                str(actor_telegram_id),
                "issue",
                issue.id,
                {"status": status.value},
            )
            return issue

    async def assign_task(
        self,
        issue_id: uuid.UUID,
        actor_telegram_id: int,
        assignee_telegram_id: int,
        title: str | None = None,
        deadline: datetime | None = None,
    ) -> Task:
        if not self.is_owner(actor_telegram_id):
            raise PermissionError("команда доступна только владельцу")
        async with self.sessions() as session, session.begin():
            issue = await session.scalar(select(Issue).where(Issue.id == issue_id).with_for_update())
            if issue is None:
                raise ValueError("инициатива не найдена")
            assignee = await session.scalar(
                select(User).where(
                    User.organization_id == issue.organization_id,
                    User.telegram_user_id == assignee_telegram_id,
                )
            )
            if assignee is None:
                raise ValueError("назначаемый пользователь ещё не известен боту")
            decision = await session.scalar(
                select(Decision).where(Decision.issue_id == issue.id).order_by(Decision.created_at.desc())
            )
            task = await session.scalar(select(Task).where(Task.issue_id == issue.id).order_by(Task.created_at.desc()))
            if task is None:
                task = Task(
                    issue_id=issue.id,
                    decision_id=decision.id if decision else None,
                    title=title or (decision.next_action if decision else issue.title),
                    description=title or (decision.next_action if decision else issue.problem),
                    completion_criteria=decision.success_criteria
                    if decision
                    else ["Выполнить согласованное действие."],
                )
                session.add(task)
            task.assignee_user_id = assignee.id
            task.deadline = deadline
            task.status = TaskStatus.OPEN
            if issue.status == IssueStatus.WAITING_FOR_OWNER:
                transition_issue(issue, IssueStatus.APPROVED)
            if issue.status == IssueStatus.APPROVED:
                transition_issue(issue, IssueStatus.EXECUTING)
            await session.flush()
            await self._audit(
                session,
                issue.organization_id,
                "task_changed",
                AuthorType.HUMAN,
                str(actor_telegram_id),
                "task",
                task.id,
            )
            return task

    def addressed_role(self, text: str) -> str | None:
        normalized = normalize_text(text)
        words = set(normalized.split())
        for role_id, role in self.config.roles.items():
            aliases = {
                normalize_text(role_id),
                normalize_text(role.name),
                *(normalize_text(alias) for alias in role.aliases),
            }
            if any(alias and (alias in normalized or alias in words) for alias in aliases):
                return role_id
        return None

    async def record_human_message(
        self, message: IncomingMessage, issue: Issue, *, explicit_role: str | None = None
    ) -> str | None:
        role_id = explicit_role or self.addressed_role(message.text)
        answers_owner_request = False
        if message.reply_to_message_id:
            async with self.sessions() as session:
                parent = await session.scalar(
                    select(IssueMessage).where(
                        IssueMessage.telegram_chat_id == message.chat_id,
                        IssueMessage.telegram_message_id == message.reply_to_message_id,
                    )
                )
                answers_owner_request = bool(parent and parent.related_type == "owner_context_request")
                if role_id is None:
                    role_id = parent.addressed_agent_id if parent else None
        if answers_owner_request:
            classification = HumanMessageClassification(
                kind=MessageKind.FACT,
                addressed_agent_id=None,
                summary=message.text[:500],
            )
        else:
            try:
                classification = await self.model.generate(
                    "classify_human_message",
                    "chief_of_staff",
                    {"text": message.text, "addressed_agent_id": role_id},
                    HumanMessageClassification,
                    cheap=True,
                )
            except Exception as exc:
                await self.audit_model_error(issue, "chief_of_staff", exc)
                classification = HumanMessageClassification(
                    kind=MessageKind.QUESTION if "?" in message.text else MessageKind.ARGUMENT,
                    addressed_agent_id=role_id,
                    summary=message.text[:300],
                )
        role_id = classification.addressed_agent_id or role_id
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(
                select(IssueMessage).where(IssueMessage.telegram_update_id == message.update_id)
            )
            if existing:
                return existing.addressed_agent_id
            session.add(
                IssueMessage(
                    issue_id=issue.id,
                    telegram_update_id=message.update_id,
                    telegram_chat_id=message.chat_id,
                    telegram_thread_id=message.thread_id,
                    telegram_message_id=message.message_id,
                    reply_to_message_id=message.reply_to_message_id,
                    author_type=AuthorType.HUMAN,
                    author_id=str(message.from_user_id),
                    kind=classification.kind,
                    addressed_agent_id=role_id,
                    content=message.text,
                )
            )
            if answers_owner_request:
                stored = await session.scalar(select(Issue).where(Issue.id == issue.id).with_for_update())
                assert stored is not None
                if stored.status == IssueStatus.WAITING_FOR_OWNER:
                    transition_issue(stored, IssueStatus.PROPOSED)
                await self._audit(
                    session,
                    issue.organization_id,
                    "owner_context_received",
                    AuthorType.HUMAN,
                    str(message.from_user_id),
                    "issue",
                    issue.id,
                )
        if role_id is None or role_id not in self.config.roles:
            return None
        try:
            response = await self.model.generate(
                "reply_to_human",
                role_id,
                {**(await self._context(issue.id)), "human_message": message.text},
                AgentReply,
            )
        except Exception as exc:
            await self.audit_model_error(issue, role_id, exc)
            return role_id
        if self.publisher:
            role = self.config.roles[role_id]
            text = f"{role.emoji} {role.name}\n\n{response.answer}"
            outgoing_id = await self.publisher.send(
                issue.telegram_chat_id,
                issue.telegram_thread_id,
                text,
                reply_to_message_id=message.message_id,
            )
            async with self.sessions() as session, session.begin():
                session.add(
                    IssueMessage(
                        issue_id=issue.id,
                        telegram_chat_id=issue.telegram_chat_id,
                        telegram_thread_id=issue.telegram_thread_id,
                        telegram_message_id=outgoing_id,
                        reply_to_message_id=message.message_id,
                        author_type=AuthorType.AGENT,
                        author_id=role_id,
                        kind=MessageKind.AGENT_RESPONSE,
                        addressed_agent_id=role_id,
                        content=text,
                        related_type="agent_reply",
                    )
                )
        return role_id

    async def add_report(self, message: IncomingMessage, issue: Issue) -> Evidence:
        async with self.sessions() as session, session.begin():
            stored = await session.scalar(select(Issue).where(Issue.id == issue.id).with_for_update())
            assert stored is not None
            evidence = Evidence(
                issue_id=issue.id,
                source=f"telegram:{message.from_user_id}",
                content=message.text,
                telegram_message_id=message.message_id,
            )
            session.add(evidence)
            if stored.status == IssueStatus.WAITING_FOR_OWNER:
                transition_issue(stored, IssueStatus.PROPOSED)
            await session.flush()
            await self._audit(
                session,
                issue.organization_id,
                "evidence_added",
                AuthorType.HUMAN,
                str(message.from_user_id),
                "evidence",
                evidence.id,
            )
            return evidence

    async def block_issue(self, message: IncomingMessage, issue: Issue) -> Issue:
        async with self.sessions() as session, session.begin():
            user = await session.scalar(
                select(User).where(
                    User.organization_id == issue.organization_id, User.telegram_user_id == message.from_user_id
                )
            )
            if not user or user.role not in {UserRole.OWNER, UserRole.PROJECT_LEAD}:
                raise PermissionError("блокировать может владелец или Project Lead")
            stored = await session.scalar(select(Issue).where(Issue.id == issue.id).with_for_update())
            assert stored is not None
            transition_issue(stored, IssueStatus.BLOCKED)
            tasks = (
                await session.scalars(
                    select(Task).where(
                        Task.issue_id == issue.id, Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS])
                    )
                )
            ).all()
            for task in tasks:
                task.status = TaskStatus.BLOCKED
            await self._audit(
                session,
                issue.organization_id,
                "issue_blocked",
                AuthorType.HUMAN,
                str(message.from_user_id),
                "issue",
                issue.id,
                {"reason": message.text[:500]},
            )
            return stored

    async def issue_status_text(self, issue_id: uuid.UUID) -> str:
        async with self.sessions() as session:
            issue = await session.get(Issue, issue_id)
            if issue is None:
                raise ValueError("инициатива не найдена")
            decision = await session.scalar(
                select(Decision).where(Decision.issue_id == issue_id).order_by(Decision.created_at.desc())
            )
            tasks = (await session.scalars(select(Task).where(Task.issue_id == issue_id))).all()
            return "\n".join(
                [
                    f"📌 {issue.human_id} · {issue.title}",
                    f"Статус: {issue.status.value}",
                    f"Приоритет: {issue.priority:.2f}",
                    f"Решение: {decision.status.value if decision else 'нет'}",
                    "Задачи: "
                    f"{sum(task.status not in {TaskStatus.DONE, TaskStatus.CANCELLED} for task in tasks)} "
                    f"открытых / {len(tasks)} всего",
                ]
            )

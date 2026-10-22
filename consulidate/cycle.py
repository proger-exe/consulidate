from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from consulidate.atlant import BudgetExceeded, CallBudget
from consulidate.models import (
    AuthorType,
    CyclePhase,
    CycleStatus,
    Decision,
    DecisionStatus,
    Evidence,
    HourlyCycle,
    Issue,
    IssueMessage,
    IssueStatus,
    Organization,
    Task,
    TaskStatus,
    utcnow,
)
from consulidate.schemas import ResearchInitiative
from consulidate.service import CouncilService, DebateResult

logger = logging.getLogger(__name__)


def cycle_bucket(now: datetime, interval_minutes: int) -> str:
    utc = now.astimezone(UTC)
    minute = (utc.minute // interval_minutes) * interval_minutes if interval_minutes <= 60 else 0
    if interval_minutes <= 60:
        bucket = utc.replace(minute=minute, second=0, microsecond=0)
    else:
        seconds = int(utc.timestamp())
        width = interval_minutes * 60
        bucket = datetime.fromtimestamp(seconds - seconds % width, UTC)
    return bucket.isoformat()


def format_cycle_report(cycle: HourlyCycle, issue: Issue | None, timezone: str) -> str:
    local = cycle.started_at.astimezone(ZoneInfo(timezone))
    lines = [
        f"🏛 CONSULIDATE · {local:%H:%M}",
        "",
        f"Новые сигналы: {cycle.new_signals}",
        f"Допущено к обсуждению: {cycle.debated}",
        f"Пересмотрено решений: {cycle.reviewed}",
        f"Создано задач: {cycle.tasks_created}",
        f"Требует владельца: {cycle.requires_owner}",
    ]
    if issue:
        lines.extend(["", "Главная тема:", f"{issue.human_id} · {issue.title}"])
    if cycle.summary:
        lines.extend(["", "Итог:", cycle.summary])
    return "\n".join(lines)


class CycleService:
    def __init__(self, council: CouncilService):
        self.council = council
        self.config = council.config
        self.sessions = council.sessions

    async def _get_or_create(
        self,
        org: Organization,
        now: datetime,
        trigger: str,
        *,
        force_new: bool = False,
    ) -> tuple[HourlyCycle, bool]:
        bucket = cycle_bucket(now, self.config.file.cycle.interval_minutes)
        if force_new:
            bucket = f"{bucket}:force:{uuid.uuid4().hex}"
        async with self.sessions() as session, session.begin():
            existing = await session.scalar(
                select(HourlyCycle).where(
                    HourlyCycle.organization_id == org.id,
                    HourlyCycle.bucket_key == bucket,
                )
            )
            if existing:
                return existing, False
            cycle = HourlyCycle(organization_id=org.id, bucket_key=bucket, trigger=trigger)
            session.add(cycle)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                async with self.sessions() as retry:
                    found = await retry.scalar(
                        select(HourlyCycle).where(
                            HourlyCycle.organization_id == org.id,
                            HourlyCycle.bucket_key == bucket,
                        )
                    )
                    assert found is not None
                    return found, False
            return cycle, True

    async def _update(self, cycle_id: uuid.UUID, **values) -> HourlyCycle:
        async with self.sessions() as session, session.begin():
            cycle = await session.get(HourlyCycle, cycle_id)
            assert cycle is not None
            for key, value in values.items():
                setattr(cycle, key, value)
            return cycle

    async def _signal_count(self, org_id: uuid.UUID, since: datetime) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count(IssueMessage.id))
                    .join(Issue, Issue.id == IssueMessage.issue_id)
                    .where(
                        Issue.organization_id == org_id,
                        IssueMessage.author_type == AuthorType.HUMAN,
                        IssueMessage.created_at >= since,
                    )
                )
                or 0
            )

    async def _research(self, cycle: HourlyCycle, org: Organization, budget: CallBudget) -> int:
        if self.config.file.cycle.max_new_initiatives == 0 or org.telegram_chat_id is None:
            return 0
        async with self.sessions() as session:
            projects = (
                await session.scalars(
                    select(Issue.title)
                    .where(Issue.organization_id == org.id)
                    .order_by(Issue.updated_at.desc())
                    .limit(20)
                )
            ).all()
        try:
            result = await self.council.model.generate(
                "research",
                "research",
                {
                    "organization": {"name": org.name, "code": org.code},
                    "recent_issue_titles": list(projects),
                    "constraint": "Use only supplied observations; no web or external tools.",
                },
                ResearchInitiative,
                budget=budget,
                cheap=True,
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            await self.council.audit_model_error(None, "research", exc)
            return 0
        if not result.create or result.issue is None:
            return 0
        thread = int(org.telegram_topics.get("initiatives", 0))
        if not thread:
            return 0
        issue, created = await self.council.create_agent_issue(
            result.issue,
            author_id="research",
            chat_id=org.telegram_chat_id,
            thread_id=thread,
            cycle_id=cycle.id,
        )
        if created:
            async with self.sessions() as session, session.begin():
                session.add(
                    Evidence(
                        issue_id=issue.id,
                        source=result.source or "Research Director observation",
                        content=(
                            f"Связь с проектами: {result.project_connection}\n"
                            f"Ожидаемая польза: {result.expected_benefit}\n"
                            f"Стоимость минимальной проверки: {result.minimum_test_cost}\n"
                            f"Почему сейчас: {result.relevance_now}\n"
                            f"Риски: {', '.join(result.risks)}\n"
                            f"Уверенность: {result.confidence}"
                        ),
                    )
                )
        return int(created)

    async def _observe_candidates(self, org: Organization, now: datetime) -> tuple[list[Issue], list[Issue]]:
        async with self.sessions() as session, session.begin():
            new = list(
                (
                    await session.scalars(
                        select(Issue)
                        .where(
                            Issue.organization_id == org.id,
                            Issue.status == IssueStatus.PROPOSED,
                            Issue.priority >= self.config.file.cycle.debate_threshold,
                        )
                        .order_by(Issue.priority.desc(), Issue.created_at)
                    )
                ).all()
            )
            due_decision_issue_ids = set(
                (
                    await session.scalars(
                        select(Decision.issue_id).where(
                            Decision.status == DecisionStatus.APPROVED,
                            Decision.review_at.is_not(None),
                            Decision.review_at <= now,
                        )
                    )
                ).all()
            )
            overdue_tasks = list(
                (
                    await session.scalars(
                        select(Task).where(
                            Task.status.in_([TaskStatus.OPEN, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
                            or_(Task.status == TaskStatus.BLOCKED, Task.deadline <= now),
                        )
                    )
                ).all()
            )
            incomplete_debate_ids = set(
                (
                    await session.scalars(
                        select(Issue.id).where(
                            Issue.organization_id == org.id,
                            Issue.status == IssueStatus.DEBATING,
                            Issue.active_debate_id.is_not(None),
                        )
                    )
                ).all()
            )
            review_ids = due_decision_issue_ids | {task.issue_id for task in overdue_tasks} | incomplete_debate_ids
            if review_ids:
                review = list(
                    (
                        await session.scalars(
                            select(Issue).where(
                                Issue.organization_id == org.id,
                                Issue.id.in_(review_ids),
                                Issue.status.notin_([IssueStatus.ARCHIVED, IssueStatus.REJECTED]),
                            )
                        )
                    ).all()
                )
                for issue in review:
                    if issue.status in {IssueStatus.EXECUTING, IssueStatus.COMPLETED}:
                        issue.status = IssueStatus.REVIEW
            else:
                review = []
        return new, review

    async def _execute(
        self,
        cycle: HourlyCycle,
        org: Organization,
        now: datetime,
        force_issue_id: uuid.UUID | None,
    ) -> HourlyCycle:
        settings = self.config.file.cycle
        budget = CallBudget(
            settings.max_agent_calls,
            settings.max_tokens,
            calls=cycle.agent_calls,
            estimated_tokens=cycle.estimated_tokens,
        )
        cycle = await self._update(cycle.id, phase=CyclePhase.OBSERVE)
        last_completed_at = now.replace(microsecond=0)
        async with self.sessions() as session:
            previous = await session.scalar(
                select(HourlyCycle)
                .where(
                    HourlyCycle.organization_id == org.id,
                    HourlyCycle.status == CycleStatus.COMPLETED,
                    HourlyCycle.id != cycle.id,
                )
                .order_by(HourlyCycle.finished_at.desc())
            )
            if previous and previous.finished_at:
                last_completed_at = previous.finished_at
            elif cycle.started_at:
                last_completed_at = cycle.started_at
        signals = await self._signal_count(org.id, last_completed_at)
        research_created = await self._research(cycle, org, budget)
        signals += research_created
        new, review = await self._observe_candidates(org, now)
        cycle = await self._update(
            cycle.id,
            new_signals=signals,
            phase=CyclePhase.SELECT,
            agent_calls=budget.calls,
            estimated_tokens=budget.estimated_tokens,
        )

        if force_issue_id:
            async with self.sessions() as session:
                forced = await session.get(Issue, force_issue_id)
            new = [forced] if forced else []
            review = []
        else:
            new = new[: settings.max_new_debates]
            review = review[: settings.max_review_debates]

        debated = reviewed = tasks = owner = 0
        main_issue_id: uuid.UUID | None = None
        summaries: list[str] = []
        cycle = await self._update(cycle.id, phase=CyclePhase.DISCUSS)
        for issue, is_review in [(item, False) for item in new] + [(item, True) for item in review]:
            if issue is None:
                continue
            result: DebateResult = await self.council.run_debate(
                issue.id,
                cycle_id=cycle.id,
                force=bool(force_issue_id or is_review),
                budget=budget,
            )
            if result.skipped:
                continue
            main_issue_id = main_issue_id or issue.id
            debated += int(not is_review)
            reviewed += int(is_review)
            tasks += result.tasks_created
            owner += int(result.requires_owner)
            if result.decision_id:
                async with self.sessions() as session:
                    decision = await session.get(Decision, result.decision_id)
                    if decision:
                        summaries.append(decision.summary)
            cycle = await self._update(
                cycle.id,
                phase=CyclePhase.DECIDE,
                main_issue_id=main_issue_id,
                debated=debated,
                reviewed=reviewed,
                tasks_created=tasks,
                requires_owner=owner,
                agent_calls=budget.calls,
                estimated_tokens=budget.estimated_tokens,
            )

        cycle = await self._update(cycle.id, phase=CyclePhase.ASSIGN)
        summary = summaries[0] if summaries else ("Новых тем для полного обсуждения нет." if signals else None)
        cycle = await self._update(
            cycle.id,
            phase=CyclePhase.MONITOR,
            summary=summary,
            agent_calls=budget.calls,
            estimated_tokens=budget.estimated_tokens,
        )
        meaningful = any((signals, debated, reviewed, tasks, owner))
        if meaningful and self.council.publisher and org.telegram_chat_id:
            cabinet = int(org.telegram_topics.get("cabinet", 0))
            if cabinet:
                async with self.sessions() as session:
                    main_issue = await session.get(Issue, main_issue_id) if main_issue_id else None
                await self.council.publisher.send(
                    org.telegram_chat_id,
                    cabinet,
                    format_cycle_report(cycle, main_issue, org.timezone),
                )
        return await self._update(
            cycle.id,
            phase=CyclePhase.DONE,
            status=CycleStatus.COMPLETED,
            finished_at=utcnow(),
        )

    async def run(
        self,
        *,
        trigger: str = "scheduled",
        now: datetime | None = None,
        force_issue_id: uuid.UUID | None = None,
        force_new: bool = False,
    ) -> HourlyCycle:
        now = now or utcnow()
        org = await self.council.ensure_organization()
        cycle, created = await self._get_or_create(org, now, trigger, force_new=force_new)
        if not created and cycle.status in {CycleStatus.COMPLETED, CycleStatus.BUDGET_EXHAUSTED}:
            return cycle
        try:
            async with asyncio.timeout(self.config.file.cycle.timeout_seconds):
                return await self._execute(cycle, org, now, force_issue_id)
        except (BudgetExceeded, TimeoutError) as exc:
            return await self._update(
                cycle.id,
                status=CycleStatus.BUDGET_EXHAUSTED,
                summary=f"Цикл остановлен безопасно: {type(exc).__name__}.",
                finished_at=utcnow(),
            )
        except Exception as exc:
            logger.exception("hourly cycle failed", extra={"event": "cycle.failed"})
            await self.council.audit_model_error(None, "cycle", exc)
            return await self._update(
                cycle.id,
                status=CycleStatus.FAILED,
                summary=f"Ошибка цикла: {type(exc).__name__}.",
                finished_at=utcnow(),
            )

    async def resume_incomplete(self) -> list[HourlyCycle]:
        async with self.sessions() as session:
            cycles = list(
                (await session.scalars(select(HourlyCycle).where(HourlyCycle.status == CycleStatus.RUNNING))).all()
            )
        results = []
        for cycle in cycles:
            async with self.sessions() as session:
                org = await session.get(Organization, cycle.organization_id)
            if org:
                results.append(await self._execute(cycle, org, utcnow(), cycle.main_issue_id))
        return results

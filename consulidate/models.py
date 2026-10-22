from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from consulidate.db import Base

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrEnum(str, enum.Enum):
    pass


class UserRole(StrEnum):
    OWNER = "owner"
    PROJECT_LEAD = "project_lead"
    OBSERVER = "observer"


class AuthorType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class IssueStatus(StrEnum):
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    COLLECTING_CONTEXT = "COLLECTING_CONTEXT"
    DEBATING = "DEBATING"
    WAITING_FOR_OWNER = "WAITING_FOR_OWNER"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    BLOCKED = "BLOCKED"
    REVIEW = "REVIEW"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class PositionVerdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"
    NEED_DATA = "need_data"


class DecisionLevel(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    VETOED = "vetoed"
    SUPERSEDED = "superseded"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class DebateStage(StrEnum):
    PROPOSE = "PROPOSE"
    QUESTION = "QUESTION"
    CHALLENGE = "CHALLENGE"
    DEFEND = "DEFEND"
    REVISE = "REVISE"
    DECIDE = "DECIDE"


class CyclePhase(StrEnum):
    OBSERVE = "OBSERVE"
    SELECT = "SELECT"
    DISCUSS = "DISCUSS"
    DECIDE = "DECIDE"
    ASSIGN = "ASSIGN"
    MONITOR = "MONITOR"
    DONE = "DONE"


class CycleStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"


class MessageKind(StrEnum):
    ARGUMENT = "argument"
    QUESTION = "question"
    FACT = "fact"
    DECISION = "decision"
    COMMAND = "command"
    AGENT_RESPONSE = "agent_response"
    SYSTEM = "system"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(10), unique=True)
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_topics: Mapped[dict[str, int]] = mapped_column(JSON_TYPE, default=dict)
    next_issue_number: Mapped[int] = mapped_column(Integer, default=1)


class User(Base, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("organization_id", "telegram_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.OBSERVER)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProjectMember(Base):
    __tablename__ = "project_members"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(100), default="member")


class Issue(Base, TimestampMixin):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("organization_id", "human_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    human_id: Mapped[str] = mapped_column(String(32), index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(500))
    problem: Mapped[str] = mapped_column(Text)
    proposal: Mapped[str | None] = mapped_column(Text)
    reason_now: Mapped[str | None] = mapped_column(Text)
    expected_result: Mapped[str | None] = mapped_column(Text)
    author_type: Mapped[AuthorType] = mapped_column(Enum(AuthorType))
    author_id: Mapped[str] = mapped_column(String(200))
    status: Mapped[IssueStatus] = mapped_column(Enum(IssueStatus), default=IssueStatus.PROPOSED)
    priority: Mapped[float] = mapped_column(Float, default=0)
    priority_factors: Mapped[dict[str, float]] = mapped_column(JSON_TYPE, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float)
    risk: Mapped[float | None] = mapped_column(Float)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_thread_id: Mapped[int] = mapped_column(BigInteger)
    telegram_root_message_id: Mapped[int | None] = mapped_column(BigInteger)
    active_debate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    debate_participants: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    debate_stage: Mapped[DebateStage | None] = mapped_column(Enum(DebateStage))
    debate_round: Mapped[int] = mapped_column(Integer, default=0)
    last_debate_context_hash: Mapped[str | None] = mapped_column(String(64))
    last_debated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IssueMessage(Base):
    __tablename__ = "issue_messages"
    __table_args__ = (UniqueConstraint("telegram_chat_id", "telegram_message_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("issues.id"), index=True)
    telegram_update_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    telegram_thread_id: Mapped[int] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    author_type: Mapped[AuthorType] = mapped_column(Enum(AuthorType))
    author_id: Mapped[str] = mapped_column(String(200))
    kind: Mapped[MessageKind] = mapped_column(Enum(MessageKind))
    addressed_agent_id: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    related_type: Mapped[str | None] = mapped_column(String(50))
    related_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("issues.id"), index=True)
    source: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentPosition(Base):
    __tablename__ = "agent_positions"
    __table_args__ = (UniqueConstraint("debate_id", "agent_id", "round"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    debate_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    agent_id: Mapped[str] = mapped_column(String(100))
    issue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("issues.id"), index=True)
    round: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[PositionVerdict] = mapped_column(Enum(PositionVerdict))
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    arguments: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    risks: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    questions: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    conditions: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    responds_to_position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_positions.id"))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HourlyCycle(Base):
    __tablename__ = "hourly_cycles"
    __table_args__ = (UniqueConstraint("organization_id", "bucket_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"))
    bucket_key: Mapped[str] = mapped_column(String(100))
    trigger: Mapped[str] = mapped_column(String(20), default="scheduled")
    status: Mapped[CycleStatus] = mapped_column(Enum(CycleStatus), default=CycleStatus.RUNNING)
    phase: Mapped[CyclePhase] = mapped_column(Enum(CyclePhase), default=CyclePhase.OBSERVE)
    main_issue_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("issues.id"))
    new_signals: Mapped[int] = mapped_column(Integer, default=0)
    debated: Mapped[int] = mapped_column(Integer, default=0)
    reviewed: Mapped[int] = mapped_column(Integer, default=0)
    tasks_created: Mapped[int] = mapped_column(Integer, default=0)
    requires_owner: Mapped[int] = mapped_column(Integer, default=0)
    agent_calls: Mapped[int] = mapped_column(Integer, default=0)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("issues.id"), index=True)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("hourly_cycles.id"))
    debate_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    level: Mapped[DecisionLevel] = mapped_column(Enum(DecisionLevel))
    status: Mapped[DecisionStatus] = mapped_column(Enum(DecisionStatus))
    summary: Mapped[str] = mapped_column(Text)
    rationale: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    dissent: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    conditions: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    success_criteria: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    stop_conditions: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    next_action: Mapped[str] = mapped_column(Text)
    assignee_hint: Mapped[str | None] = mapped_column(String(200))
    deadline_reason: Mapped[str] = mapped_column(Text)
    risk: Mapped[float] = mapped_column(Float)
    reversibility: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    estimated_cost: Mapped[float] = mapped_column(Float)
    requires_owner: Mapped[bool] = mapped_column(Boolean, default=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    issue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("issues.id"), index=True)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decisions.id"), unique=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text)
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.OPEN)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_criteria: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    actor_type: Mapped[AuthorType] = mapped_column(Enum(AuthorType))
    actor_id: Mapped[str] = mapped_column(String(200))
    entity_type: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TelegramUpdateReceipt(Base):
    __tablename__ = "telegram_update_receipts"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

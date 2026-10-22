from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from consulidate.models import DecisionLevel, MessageKind, PositionVerdict

CompactText = Annotated[str, Field(min_length=1, max_length=400)]
BulletText = Annotated[str, Field(min_length=1, max_length=200)]


class PriorityFactors(BaseModel):
    impact: float = Field(ge=0, le=1)
    urgency: float = Field(ge=0, le=1)
    inaction_risk: float = Field(ge=0, le=1)
    strategic_fit: float = Field(ge=0, le=1)
    new_information: float = Field(ge=0, le=1)


class IssueDraft(BaseModel):
    command: Literal["ISSUE"] = "ISSUE"
    title: str = Field(min_length=1, max_length=500)
    problem: str = Field(min_length=1, max_length=1600)
    proposal: str | None = Field(default=None, max_length=1200)
    reason_now: str | None = Field(default=None, max_length=800)
    expected_result: str | None = Field(default=None, max_length=800)
    confidence: float | None = Field(default=None, ge=0, le=1)
    risk: float | None = Field(default=None, ge=0, le=1)
    priority_factors: PriorityFactors


class ParticipantSelection(BaseModel):
    command: Literal["SELECT"] = "SELECT"
    roles: list[str] = Field(min_length=3, max_length=5)
    rationale: CompactText


class AgentPositionOutput(BaseModel):
    command: Literal["POSITION"] = "POSITION"
    verdict: PositionVerdict
    confidence: float = Field(ge=0, le=1)
    summary: CompactText
    arguments: list[BulletText] = Field(default_factory=list, max_length=4)
    risks: list[BulletText] = Field(default_factory=list, max_length=4)
    questions: list[BulletText] = Field(default_factory=list, max_length=3)
    conditions: list[BulletText] = Field(default_factory=list, max_length=4)
    responds_to_agent_id: str | None = None

    @model_validator(mode="after")
    def questions_required_for_need_data(self) -> AgentPositionOutput:
        if self.verdict == PositionVerdict.NEED_DATA and not self.questions:
            raise ValueError("NEED_DATA requires at least one concrete owner question")
        return self


class ConflictMap(BaseModel):
    command: Literal["CONFLICTS"] = "CONFLICTS"
    contradictions: list[BulletText] = Field(default_factory=list, max_length=5)
    challenged_roles: list[str] = Field(default_factory=list, max_length=5)
    shared_ground: list[BulletText] = Field(default_factory=list, max_length=5)


class DecisionDraft(BaseModel):
    command: Literal["DECISION"] = "DECISION"
    level: DecisionLevel
    summary: CompactText
    rationale: list[BulletText] = Field(min_length=1, max_length=5)
    dissent: list[BulletText] = Field(default_factory=list, max_length=5)
    conditions: list[BulletText] = Field(default_factory=list, max_length=5)
    success_criteria: list[BulletText] = Field(min_length=1, max_length=5)
    stop_conditions: list[BulletText] = Field(min_length=1, max_length=5)
    next_action: CompactText
    assignee_hint: str | None = Field(default=None, max_length=200)
    deadline_reason: CompactText
    review_hours: int = Field(default=168, ge=1, le=8760)
    risk: float = Field(ge=0, le=1)
    reversibility: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    estimated_cost: float = Field(ge=0)


class HumanMessageClassification(BaseModel):
    command: Literal["CLASSIFY"] = "CLASSIFY"
    kind: MessageKind
    addressed_agent_id: str | None = None
    summary: CompactText


class AgentReply(BaseModel):
    command: Literal["REPLY"] = "REPLY"
    answer: str = Field(min_length=1, max_length=800)
    assumptions: list[BulletText] = Field(default_factory=list, max_length=4)
    evidence_refs: list[BulletText] = Field(default_factory=list, max_length=4)


class ResearchInitiative(BaseModel):
    command: Literal["RESEARCH"] = "RESEARCH"
    create: bool
    source: str | None = Field(default=None, max_length=500)
    project_connection: str | None = Field(default=None, max_length=500)
    expected_benefit: str | None = Field(default=None, max_length=500)
    minimum_test_cost: float | None = Field(default=None, ge=0)
    relevance_now: str | None = Field(default=None, max_length=500)
    risks: list[BulletText] = Field(default_factory=list, max_length=5)
    confidence: float | None = Field(default=None, ge=0, le=1)
    issue: IssueDraft | None = None

    @model_validator(mode="after")
    def complete_when_created(self) -> ResearchInitiative:
        if self.create:
            required = (
                self.source,
                self.project_connection,
                self.expected_benefit,
                self.minimum_test_cost,
                self.relevance_now,
                self.confidence,
                self.issue,
            )
            if any(value is None for value in required) or not self.risks:
                raise ValueError("created research initiative is missing required evidence or issue fields")
        return self

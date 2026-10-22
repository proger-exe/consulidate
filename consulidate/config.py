from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TOPIC_NAMES = {
    "cabinet": "🏛 Кабинет",
    "initiatives": "📥 Инициативы",
    "portfolio": "📊 Портфель проектов",
    "risks": "🚨 Риски и инциденты",
    "growth": "📈 Рост и сообщество",
    "technical": "⚙️ Технический комитет",
    "reports": "🧾 Отчёты лидеров",
    "decisions": "📜 Реестр решений",
    "archive": "🗄 Архив",
}


class OrganizationConfig(BaseModel):
    name: str
    code: str = Field(pattern=r"^[A-Z0-9]{2,10}$")
    timezone: str = "UTC"


class TelegramConfig(BaseModel):
    owner_ids: list[int] = Field(default_factory=list)
    chat_id: int | None = None
    topics: dict[str, int | None] = Field(default_factory=dict)

    @field_validator("topics")
    @classmethod
    def known_topics(cls, value: dict[str, int | None]) -> dict[str, int | None]:
        unknown = set(value) - set(TOPIC_NAMES)
        if unknown:
            raise ValueError(f"unknown Telegram topics: {sorted(unknown)}")
        return value


class CycleConfig(BaseModel):
    interval_minutes: int = Field(default=60, ge=1)
    max_new_debates: int = Field(default=1, ge=0)
    max_review_debates: int = Field(default=2, ge=0)
    max_new_initiatives: int = Field(default=1, ge=0)
    debate_threshold: float = Field(default=0.65, ge=0, le=1)
    max_participants: int = Field(default=5, ge=3, le=5)
    max_rounds: int = Field(default=3, ge=1, le=3)
    max_agent_calls: int = Field(default=20, ge=1)
    max_tokens: int = Field(default=60_000, ge=1_000)
    timeout_seconds: int = Field(default=600, ge=10)


class AutonomyConfig(BaseModel):
    max_decision_level: Literal["L0", "L1", "L2", "L3"] = "L1"
    max_estimated_cost: float = Field(default=10, ge=0)
    minimum_confidence: float = Field(default=0.70, ge=0, le=1)
    maximum_risk: float = Field(default=0.30, ge=0, le=1)
    minimum_reversibility: float = Field(default=0.80, ge=0, le=1)


class ProjectContextConfig(BaseModel):
    directory: Path = Path("project_context")
    max_characters: int = Field(default=12_000, ge=1, le=200_000)


class FileConfig(BaseModel):
    organization: OrganizationConfig
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    cycle: CycleConfig = Field(default_factory=CycleConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    project_context: ProjectContextConfig = Field(default_factory=ProjectContextConfig)


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        default="postgresql+asyncpg://consulidate:consulidate@localhost:55433/consulidate",
        validation_alias="DATABASE_URL",
    )
    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")
    config_path: Path = Field(default=Path("config/consulidate.yaml"), validation_alias="CONSULIDATE_CONFIG")
    model_provider: Literal["atlant", "fake"] = Field(default="fake", validation_alias="CONSULIDATE_MODEL_PROVIDER")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")


class RoleConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    emoji: str
    mandate: str
    incentives: list[str]
    blind_spots: list[str]
    decision_policy: list[str]
    style: str
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True


class AppConfig(BaseModel):
    runtime: RuntimeSettings
    file: FileConfig
    roles: dict[str, RoleConfig]
    corporate_policy: str


def load_project_context(config: AppConfig) -> list[dict[str, str]]:
    root = config.file.project_context.directory.expanduser()
    if not root.is_absolute():
        root = config.runtime.config_path.resolve().parent.parent / root
    root = root.resolve()
    if not root.is_dir():
        return []
    remaining = config.file.project_context.max_characters
    documents: list[dict[str, str]] = []
    for path in sorted(root.glob("*.md")):
        resolved = path.resolve()
        if resolved.parent != root or not resolved.is_file():
            continue
        content = resolved.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            continue
        documents.append({"name": path.name, "content": content[:remaining]})
        remaining -= len(documents[-1]["content"])
        if remaining <= 0:
            break
    return documents


def load_config(runtime: RuntimeSettings | None = None) -> AppConfig:
    runtime = runtime or RuntimeSettings()
    path = runtime.config_path.resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    file_config = FileConfig.model_validate(raw)
    role_dir = path.parent / "agents"
    roles: dict[str, RoleConfig] = {}
    for role_path in sorted(role_dir.glob("*.yaml")):
        role = RoleConfig.model_validate(yaml.safe_load(role_path.read_text(encoding="utf-8")))
        if role.id in roles:
            raise ValueError(f"duplicate role id: {role.id}")
        roles[role.id] = role
    required = {"chief_of_staff", "coo", "cpo", "cto", "cfo", "community", "growth", "risk", "research"}
    missing = required - set(roles)
    if missing:
        raise ValueError(f"missing role configs: {sorted(missing)}")
    policy = (path.parent / "corporate_policy.md").read_text(encoding="utf-8").strip()
    return AppConfig(runtime=runtime, file=file_config, roles=roles, corporate_policy=policy)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config()

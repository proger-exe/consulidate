from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from consulidate.atlant import FakeAtlantAdapter
from consulidate.config import AppConfig, TelegramConfig, load_config
from consulidate.db import Base, build_session_factory
from consulidate.models import Organization
from consulidate.service import CouncilService


class FakePublisher:
    def __init__(self):
        self.next_message_id = 1_000
        self.messages: list[dict[str, Any]] = []

    async def send(
        self,
        chat_id: int,
        thread_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        expandable: bool = False,
    ) -> int:
        self.next_message_id += 1
        self.messages.append(
            {
                "message_id": self.next_message_id,
                "chat_id": chat_id,
                "thread_id": thread_id,
                "text": text,
                "reply_to_message_id": reply_to_message_id,
                "expandable": expandable,
            }
        )
        return self.next_message_id


class FakeForumBot:
    def __init__(self):
        self.next_thread_id = 100
        self.created: list[str] = []

    async def get_chat(self, chat_id: int):
        return SimpleNamespace(id=chat_id, type="supergroup", is_forum=True)

    async def create_forum_topic(self, chat_id: int, name: str):
        del chat_id
        self.next_thread_id += 1
        self.created.append(name)
        return SimpleNamespace(message_thread_id=self.next_thread_id)


def make_config() -> AppConfig:
    config = load_config()
    telegram = TelegramConfig(owner_ids=[100], chat_id=None, topics={})
    file_config = config.file.model_copy(update={"telegram": telegram})
    return config.model_copy(update={"file": file_config})


@dataclass
class TestStack:
    config: AppConfig
    engine: AsyncEngine
    service: CouncilService
    model: FakeAtlantAdapter
    publisher: FakePublisher

    async def close(self) -> None:
        await self.engine.dispose()


async def make_stack(
    tmp_path: Path,
    *,
    responses: dict[str, list[Any]] | None = None,
    filename: str = "test.db",
) -> TestStack:
    config = make_config()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / filename}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = build_session_factory(engine)
    model = FakeAtlantAdapter(config, responses)
    publisher = FakePublisher()
    service = CouncilService(config, sessions, model, publisher)
    org = await service.ensure_organization()
    async with sessions() as session, session.begin():
        stored = await session.get(Organization, org.id)
        assert stored is not None
        stored.telegram_chat_id = -100123
        stored.telegram_topics = {"initiatives": 10, "cabinet": 11}
    return TestStack(config, engine, service, model, publisher)

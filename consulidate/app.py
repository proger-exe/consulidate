from __future__ import annotations

import json
import logging
import sys
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from consulidate.atlant import AtlantAdapter, FakeAtlantAdapter
from consulidate.config import AppConfig, get_config
from consulidate.cycle import CycleService
from consulidate.db import build_engine, build_session_factory
from consulidate.service import CouncilService
from consulidate.telegram import AiogramPublisher, ForumSetup, build_router

logger = logging.getLogger(__name__)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        event = getattr(record, "event", None)
        if event:
            payload["event"] = event
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)


def make_model(config: AppConfig):
    return AtlantAdapter(config) if config.runtime.model_provider == "atlant" else FakeAtlantAdapter(config)


async def run(config: AppConfig | None = None) -> None:
    config = config or get_config()
    configure_logging(config.runtime.log_level)
    if not config.runtime.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    engine = build_engine(config.runtime.database_url)
    sessions = build_session_factory(engine)
    bot = Bot(config.runtime.telegram_bot_token)
    publisher = AiogramPublisher(bot)
    council = CouncilService(config, sessions, make_model(config), publisher)
    cycles = CycleService(council)
    setup = ForumSetup(council, bot)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(council, cycles, setup))

    @dispatcher.error()
    async def errors(event: ErrorEvent) -> bool:
        logger.exception("telegram update failed", exc_info=event.exception, extra={"event": "telegram.error"})
        update = event.update
        if update.message:
            text = str(event.exception)
            if isinstance(event.exception, PermissionError):
                prefix = "⛔"
            elif isinstance(event.exception, ValueError):
                prefix = "⚠️"
            else:
                prefix, text = "❌", "Внутренняя ошибка сохранена в журнале."
            await update.message.answer(f"{prefix} {text}")
        return True

    scheduler = AsyncIOScheduler(timezone=config.file.organization.timezone)
    scheduler.add_job(
        cycles.run,
        "interval",
        minutes=config.file.cycle.interval_minutes,
        id="consulidate-hourly-cycle",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    try:
        await council.ensure_organization()
        await cycles.resume_incomplete()
        await bot.set_my_commands(
            [
                BotCommand(command=name, description=description)
                for name, description in (
                    ("setup", "Настроить Telegram forum"),
                    ("new", "Создать инициативу"),
                    ("discuss", "Запустить дебаты"),
                    ("summon", "Вызвать роли"),
                    ("status", "Статус инициативы"),
                    ("decide", "Сформировать решение"),
                    ("approve", "Утвердить решение"),
                    ("reject", "Отклонить решение"),
                    ("veto", "Наложить вето"),
                    ("assign", "Назначить задачу"),
                    ("report", "Добавить отчёт"),
                    ("block", "Зафиксировать блокер"),
                    ("review", "Пересмотреть решение"),
                    ("reopen", "Переоткрыть инициативу"),
                    ("archive", "Архивировать инициативу"),
                    ("cycle", "Запустить цикл вручную"),
                    ("help", "Справка"),
                )
            ]
        )
        scheduler.start()
        logger.info("Consulidate started", extra={"event": "app.started"})
        await dispatcher.start_polling(bot, handle_signals=True)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()
        logger.info("Consulidate stopped", extra={"event": "app.stopped"})

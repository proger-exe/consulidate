from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import MessageEntityType
from aiogram.filters import Command
from aiogram.types import Message, ReplyParameters, TelegramObject, Update
from aiogram.utils.formatting import ExpandableBlockQuote, Text

from consulidate.atlant import CallBudget
from consulidate.config import TOPIC_NAMES
from consulidate.cycle import CycleService
from consulidate.models import DecisionStatus, Issue, IssueStatus, Organization
from consulidate.service import CouncilService, IncomingMessage, Publisher

logger = logging.getLogger(__name__)
HUMAN_ID_RE = re.compile(r"^[A-Za-z0-9]{2,10}-\d+$")


class ForumBot(Protocol):
    async def get_chat(self, chat_id: int) -> Any: ...

    async def create_forum_topic(self, chat_id: int, name: str) -> Any: ...


class AiogramPublisher(Publisher):
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(
        self,
        chat_id: int,
        thread_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        expandable: bool = False,
    ) -> int:
        entities = None
        if expandable:
            visible, separator, details = text.partition("\n\n")
            if separator and details:
                text, entities = Text(visible, separator, ExpandableBlockQuote(details)).render()
        message = await self.bot.send_message(
            chat_id,
            text,
            entities=entities,
            message_thread_id=thread_id or None,
            reply_parameters=(
                ReplyParameters(message_id=reply_to_message_id) if reply_to_message_id is not None else None
            ),
        )
        return message.message_id


class ForumSetup:
    def __init__(self, service: CouncilService, bot: ForumBot):
        self.service = service
        self.bot = bot

    async def run(
        self,
        *,
        chat_id: int,
        owner_id: int,
        bind_key: str | None = None,
        current_thread_id: int | None = None,
    ) -> dict[str, int]:
        if not self.service.is_owner(owner_id):
            raise PermissionError("команда доступна только владельцу")
        chat = await self.bot.get_chat(chat_id)
        if getattr(chat, "type", None) != "supergroup" or not getattr(chat, "is_forum", False):
            raise ValueError("/setup нужно запустить в supergroup с включённым форумом")
        configured_chat = self.service.config.file.telegram.chat_id
        if configured_chat is not None and configured_chat != chat_id:
            raise ValueError("этот чат не совпадает с telegram.chat_id в конфигурации")
        if bind_key:
            if bind_key not in TOPIC_NAMES:
                raise ValueError(f"неизвестный topic key: {bind_key}")
            if not current_thread_id:
                raise ValueError("/setup bind нужно вызвать внутри forum topic")
        org = await self.service.ensure_organization()
        async with self.service.sessions() as session, session.begin():
            stored = await session.get(Organization, org.id)
            assert stored is not None
            if stored.telegram_chat_id not in {None, chat_id}:
                raise ValueError("организация уже привязана к другому Telegram-чату")
            stored.telegram_chat_id = chat_id
            topics = dict(stored.telegram_topics or {})
            if bind_key:
                assert current_thread_id is not None
                topics[bind_key] = int(current_thread_id)
            for key, topic_id in self.service.config.file.telegram.topics.items():
                if topic_id and key not in topics:
                    topics[key] = int(topic_id)
            stored.telegram_topics = topics
        if bind_key:
            return topics
        for key, name in TOPIC_NAMES.items():
            if key in topics:
                continue
            topic = await self.bot.create_forum_topic(chat_id, name)
            topics[key] = int(topic.message_thread_id)
            async with self.service.sessions() as session, session.begin():
                stored = await session.get(Organization, org.id)
                assert stored is not None
                stored.telegram_topics = dict(topics)
        return topics


class IdempotencyMiddleware(BaseMiddleware):
    def __init__(self, service: CouncilService):
        self.service = service

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = data.get("event_update")
        if not isinstance(update, Update):
            return await handler(event, data)
        if not await self.service.claim_update(update.update_id):
            return None
        try:
            if isinstance(event, Message) and event.from_user is not None:
                await self.service.ensure_user(incoming(event, update))
            result = await handler(event, data)
        except Exception as exc:
            await self.service.finish_update(update.update_id, exc)
            raise
        await self.service.finish_update(update.update_id)
        return result


def incoming(message: Message, update: Update, *, text: str | None = None) -> IncomingMessage:
    if message.from_user is None:
        raise ValueError("Telegram update has no sender")
    return IncomingMessage(
        update_id=update.update_id,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id or 0,
        message_id=message.message_id,
        from_user_id=message.from_user.id,
        username=message.from_user.username,
        display_name=message.from_user.full_name,
        text=text if text is not None else (message.text or message.caption or ""),
        reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
    )


def command_args(message: Message) -> str:
    text = message.text or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""


def split_issue_arg(args: str) -> tuple[str | None, str]:
    if not args:
        return None, ""
    first, *rest = args.split(maxsplit=1)
    if HUMAN_ID_RE.match(first):
        return first.upper(), rest[0] if rest else ""
    return None, args


def authentic_command(message: Message) -> bool:
    first_entity = message.entities[0] if message.entities else None
    return bool(
        first_entity is not None
        and first_entity.type == MessageEntityType.BOT_COMMAND
        and first_entity.offset == 0
        and message.forward_origin is None
    )


async def require_chat(service: CouncilService, message: Message) -> Organization:
    org = await service.ensure_organization()
    if org.telegram_chat_id is None:
        raise ValueError("сначала владелец должен выполнить /setup")
    if org.telegram_chat_id != message.chat.id:
        raise PermissionError("бот настроен для другого чата")
    return org


async def resolve_command_issue(service: CouncilService, message: Message, args: str) -> tuple[Issue, str]:
    human_id, rest = split_issue_arg(args)
    issue = await service.resolve_issue(
        message.chat.id,
        human_id=human_id,
        reply_to_message_id=message.reply_to_message.message_id if message.reply_to_message else None,
    )
    if issue is None:
        raise ValueError("ответьте на сообщение инициативы или укажите ID, например MJ-001")
    return issue, rest


def build_router(service: CouncilService, cycles: CycleService, setup: ForumSetup) -> Router:
    router = Router(name="consulidate")
    router.message.outer_middleware(IdempotencyMiddleware(service))

    async def owner(message: Message) -> int:
        if not authentic_command(message) or message.from_user is None or not service.is_owner(message.from_user.id):
            raise PermissionError("команда доступна только владельцу")
        return message.from_user.id

    @router.message(Command("setup"))
    async def setup_handler(message: Message) -> None:
        owner_id = await owner(message)
        args = command_args(message).split()
        bind_key = args[1] if len(args) == 2 and args[0] == "bind" else None
        if args and bind_key is None:
            raise ValueError("формат: /setup или /setup bind <topic-key>")
        topics = await setup.run(
            chat_id=message.chat.id,
            owner_id=owner_id,
            bind_key=bind_key,
            current_thread_id=message.message_thread_id,
        )
        await message.answer(
            "✅ Forum настроен:\n" + "\n".join(f"• {key}: {value}" for key, value in sorted(topics.items()))
        )

    @router.message(Command("new"))
    async def new_handler(message: Message, event_update: Update) -> None:
        await require_chat(service, message)
        args = command_args(message)
        replied = message.reply_to_message
        source = "\n\n".join(
            part for part in (args, (replied.text or replied.caption or "") if replied else "") if part
        )
        if not source:
            raise ValueError("добавьте описание после /new или ответьте командой на сообщение")
        issue, created = await service.create_issue_from_message(incoming(message, event_update, text=source))
        await message.answer(f"{'✅ Создана' if created else '♻️ Уже существует'} инициатива {issue.human_id}.")

    @router.message(Command("discuss"))
    async def discuss_handler(message: Message) -> None:
        await owner(message)
        await require_chat(service, message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        limits = service.config.file.cycle
        result = await service.run_debate(
            issue.id,
            force=True,
            budget=CallBudget(limits.max_agent_calls, limits.max_tokens),
        )
        if result.decision_id:
            await message.answer(f"Дебаты {issue.human_id} завершены: позиций {result.positions}, решение создано.")
        else:
            await message.answer(f"Дебаты {issue.human_id} приостановлены: нужны данные владельца.")

    @router.message(Command("summon"))
    async def summon_handler(message: Message) -> None:
        await owner(message)
        await require_chat(service, message)
        issue, rest = await resolve_command_issue(service, message, command_args(message))
        roles = [role.casefold() for role in rest.split()]
        unknown = [role for role in roles if role not in service.config.roles]
        if unknown or not 3 <= len(set(roles)) <= 5:
            raise ValueError("укажите 3–5 известных ролей, например: /summon cto cfo risk")
        limits = service.config.file.cycle
        result = await service.run_debate(
            issue.id,
            force=True,
            requested_roles=list(dict.fromkeys(roles)),
            budget=CallBudget(limits.max_agent_calls, limits.max_tokens),
        )
        suffix = " Решение создано." if result.decision_id else " Ожидаются данные владельца."
        await message.answer(f"Вызваны: {', '.join(roles)}. Публичных позиций: {result.positions}.{suffix}")

    @router.message(Command("status"))
    async def status_handler(message: Message) -> None:
        await require_chat(service, message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        await message.answer(await service.issue_status_text(issue.id))

    @router.message(Command("decide"))
    async def decide_handler(message: Message) -> None:
        await owner(message)
        await require_chat(service, message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        existing = await service.latest_decision(issue.id)
        if existing and issue.debate_stage and issue.debate_stage.value == "DECIDE":
            await message.answer(f"У {issue.human_id} уже есть решение: {existing.status.value}.")
            return
        limits = service.config.file.cycle
        result = await service.run_debate(
            issue.id,
            force=True,
            budget=CallBudget(limits.max_agent_calls, limits.max_tokens),
        )
        await message.answer("Решение сформировано." if result.decision_id else "Недостаточно данных для решения.")

    @router.message(Command("approve"))
    async def approve_handler(message: Message) -> None:
        owner_id = await owner(message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        decision = await service.approve_decision(issue.id, owner_id)
        await message.answer(f"✅ Решение {decision.level.value} утверждено владельцем.")

    @router.message(Command("reject"))
    async def reject_handler(message: Message) -> None:
        owner_id = await owner(message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        await service.set_decision_status(issue.id, owner_id, DecisionStatus.REJECTED)
        await message.answer(f"❌ {issue.human_id} отклонена.")

    @router.message(Command("veto"))
    async def veto_handler(message: Message) -> None:
        owner_id = await owner(message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        await service.set_decision_status(issue.id, owner_id, DecisionStatus.VETOED)
        await message.answer(f"⛔ На решение {issue.human_id} наложено вето.")

    @router.message(Command("reopen"))
    async def reopen_handler(message: Message) -> None:
        owner_id = await owner(message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        await service.set_issue_status(issue.id, owner_id, IssueStatus.PROPOSED)
        await message.answer(f"🔄 {issue.human_id} переоткрыта.")

    @router.message(Command("archive"))
    async def archive_handler(message: Message) -> None:
        owner_id = await owner(message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        await service.set_issue_status(issue.id, owner_id, IssueStatus.ARCHIVED)
        await message.answer(f"🗄 {issue.human_id} отправлена в архив.")

    @router.message(Command("assign"))
    async def assign_handler(message: Message) -> None:
        owner_id = await owner(message)
        issue, rest = await resolve_command_issue(service, message, command_args(message))
        parts = rest.split(maxsplit=2)
        if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
            raise ValueError("формат: /assign [MJ-001] <telegram-id> <YYYY-MM-DD|none> [задача]")
        assignee_id = int(parts[0])
        deadline = None if parts[1] == "none" else datetime.fromisoformat(parts[1]).replace(tzinfo=UTC)
        title = parts[2] if len(parts) == 3 else None
        task = await service.assign_task(issue.id, owner_id, assignee_id, title, deadline)
        await message.answer(f"✅ Задача назначена: {task.title}")

    @router.message(Command("report"))
    async def report_handler(message: Message, event_update: Update) -> None:
        await require_chat(service, message)
        issue, rest = await resolve_command_issue(service, message, command_args(message))
        if not rest:
            raise ValueError("добавьте текст отчёта")
        await service.add_report(incoming(message, event_update, text=rest), issue)
        await message.answer("🧾 Отчёт сохранён как Evidence.")

    @router.message(Command("block"))
    async def block_handler(message: Message, event_update: Update) -> None:
        await require_chat(service, message)
        issue, rest = await resolve_command_issue(service, message, command_args(message))
        if not rest:
            raise ValueError("укажите причину блокировки")
        await service.block_issue(incoming(message, event_update, text=rest), issue)
        await message.answer(f"🚧 {issue.human_id} заблокирована и вернётся в повестку.")

    @router.message(Command("review"))
    async def review_handler(message: Message) -> None:
        await owner(message)
        issue, _ = await resolve_command_issue(service, message, command_args(message))
        limits = service.config.file.cycle
        result = await service.run_debate(
            issue.id,
            force=True,
            budget=CallBudget(limits.max_agent_calls, limits.max_tokens),
        )
        suffix = "решение создано" if result.decision_id else "нужны данные владельца"
        await message.answer(f"Пересмотр {issue.human_id}: {suffix}; позиций {result.positions}.")

    @router.message(Command("cycle"))
    async def cycle_handler(message: Message) -> None:
        await owner(message)
        await require_chat(service, message)
        args = command_args(message).split()
        force = bool(args and args[0].casefold() == "force")
        if args and not force:
            raise ValueError("формат: /cycle или /cycle force [MJ-001]")
        if len(args) > 2:
            raise ValueError("формат: /cycle или /cycle force [MJ-001]")
        issue = None
        if force and (len(args) == 2 or message.reply_to_message):
            issue_arg = args[1] if len(args) == 2 else ""
            issue, _ = await resolve_command_issue(service, message, issue_arg)
        cycle = await cycles.run(
            trigger="manual-force" if force else "manual",
            force_issue_id=issue.id if issue else None,
            force_new=force,
        )
        await message.answer(f"Цикл: {cycle.status.value}; вызовов {cycle.agent_calls}.")

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        await message.answer(
            "Consulidate commands:\n"
            "/setup · /new · /discuss · /summon · /status · /decide\n"
            "/approve · /reject · /veto · /assign · /report · /block\n"
            "/review · /reopen · /archive · /cycle [force] · /help\n\n"
            "Для команд над инициативой ответьте на её сообщение или укажите MJ-001."
        )

    @router.message(F.reply_to_message)
    async def contextual_reply_handler(message: Message, event_update: Update) -> None:
        if message.text and message.text.startswith("/"):
            return
        await require_chat(service, message)
        parent = message.reply_to_message
        assert parent is not None
        issue = await service.resolve_issue(message.chat.id, reply_to_message_id=parent.message_id)
        if issue:
            role = await service.record_human_message(incoming(message, event_update), issue)
            if role:
                await message.answer(f"Сообщение передано роли {role}.")
            else:
                await message.answer(
                    f"Контекст для {issue.human_id} сохранён. Для продолжения: /review {issue.human_id}"
                )

    @router.message(F.text)
    async def initiative_topic_handler(message: Message, event_update: Update) -> None:
        if message.text and message.text.startswith("/"):
            return
        org = await require_chat(service, message)
        initiatives_thread = int((org.telegram_topics or {}).get("initiatives", 0))
        if initiatives_thread and message.message_thread_id == initiatives_thread:
            issue, created = await service.create_issue_from_message(incoming(message, event_update))
            if not created:
                await message.answer(f"♻️ Похоже на существующую инициативу {issue.human_id}.")

    return router

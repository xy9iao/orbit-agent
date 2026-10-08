"""Telegram channel: long polling, owner-only, optional proxy."""

from __future__ import annotations

from loguru import logger
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler as TgMessageHandler,
    filters,
)

from .base import Incoming, MessageHandler

MAX_LEN = 4000


def _chunks(text: str, size: int = MAX_LEN) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, owner_chat_id: int, proxy: str | None = None):
        self.token = token
        self.owner_chat_id = owner_chat_id
        self.proxy = proxy
        self.app: Application | None = None
        self._handler: MessageHandler | None = None

    def default_chat_id(self) -> str | None:
        return str(self.owner_chat_id)

    async def start(self, handler: MessageHandler) -> None:
        self._handler = handler
        builder = ApplicationBuilder().token(self.token)
        if self.proxy:
            builder = builder.proxy(self.proxy).get_updates_proxy(self.proxy)
        self.app = builder.build()
        self.app.add_handler(CommandHandler("start", self._on_start))
        self.app.add_handler(CommandHandler("id", self._on_start))
        self.app.add_handler(TgMessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("telegram: polling started (owner chat id {})", self.owner_chat_id)

    async def stop(self) -> None:
        if not self.app:
            return
        if self.app.updater:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        self.app = None

    async def send(self, chat_id: str, text: str) -> bool:
        if not self.app:
            return False
        for part in _chunks(text):
            await self.app.bot.send_message(chat_id=int(chat_id), text=part)
        return True

    # ---- handlers ----------------------------------------------------------
    def _is_owner(self, update: Update) -> bool:
        return bool(update.effective_chat) and update.effective_chat.id == self.owner_chat_id

    async def _on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_chat:
            return
        cid = update.effective_chat.id
        if self._is_owner(update):
            await update.message.reply_text("Orbit 在线。直接说话就行。")
        else:
            await update.message.reply_text(
                f"未授权。你的 chat id 是 {cid}，把它填进 .env 的 TELEGRAM_OWNER_CHAT_ID 后重启。"
            )

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text or not update.effective_chat:
            return
        if not self._is_owner(update):
            logger.warning("telegram: ignoring message from chat {}", update.effective_chat.id)
            return
        assert self._handler is not None
        await update.effective_chat.send_action(ChatAction.TYPING)
        reply = await self._handler(
            Incoming(channel=self.name, chat_id=str(update.effective_chat.id), text=update.message.text)
        )
        for part in _chunks(reply):
            await update.message.reply_text(part)

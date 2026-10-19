"""Telegram channel: long polling, owner-only, optional proxy. Text and photo messages."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

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


def save_photo(media_dir: Path, data: bytes, name: str, now: datetime | None = None) -> Path:
    """Write a received photo to media_dir/YYYY-MM-DD/HHMMSS_<name>.jpg and return its path."""
    now = now or datetime.now()
    day_dir = media_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{now.strftime('%H%M%S')}_{name}.jpg"
    path.write_bytes(data)
    return path


def photo_text(caption: str | None, path: Path, home: Path | None) -> str:
    """The text half of a photo message: the caption plus where the photo was saved (relative to ORBIT_HOME)."""
    shown = path.relative_to(home).as_posix() if home and path.is_relative_to(home) else path.as_posix()
    return f"{(caption or '').strip()}\n[照片已保存：{shown}]".strip()


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        token: str,
        owner_chat_id: int,
        proxy: str | None = None,
        media_dir: Path | None = None,
        home: Path | None = None,
    ):
        self.token = token
        self.owner_chat_id = owner_chat_id
        self.proxy = proxy
        self.media_dir = media_dir or Path("data/media")
        self.home = home
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
        self.app.add_handler(TgMessageHandler(filters.PHOTO, self._on_photo))
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

    async def _on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo (caption optional): save the largest size, then pass it on with the caption."""
        if not update.message or not update.message.photo or not update.effective_chat:
            return
        if not self._is_owner(update):
            logger.warning("telegram: ignoring photo from chat {}", update.effective_chat.id)
            return
        assert self._handler is not None
        await update.effective_chat.send_action(ChatAction.TYPING)
        largest = update.message.photo[-1]
        file = await largest.get_file()
        data = bytes(await file.download_as_bytearray())
        path = save_photo(self.media_dir, data, largest.file_unique_id)
        logger.info("telegram: photo saved to {} ({} bytes)", path, len(data))
        reply = await self._handler(
            Incoming(
                channel=self.name,
                chat_id=str(update.effective_chat.id),
                text=photo_text(update.message.caption, path, self.home),
                images=[path],
            )
        )
        for part in _chunks(reply):
            await update.message.reply_text(part)

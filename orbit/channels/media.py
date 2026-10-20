"""Photos received on a channel: saved under data/media/YYYY-MM-DD/, or kept in memory when incognito."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from loguru import logger

from .base import Incoming


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


def prepare(
    incoming: Incoming, text: str, media_dir: Path, home: Path | None, *, save: bool
) -> tuple[str, list[Path | bytes]]:
    """The text and images to give the agent. save=False (incognito): nothing touches the disk."""
    images: list[Path | bytes] = list(incoming.images)
    if not incoming.photos:
        return text, images
    if not save:
        return text or "（一张图片）", [*images, *(data for _, data in incoming.photos)]
    for name, data in incoming.photos:
        path = save_photo(media_dir, data, name)
        logger.info("photo saved to {} ({} bytes)", path, len(data))
        text = photo_text(text, path, home)
        images.append(path)
    return text, images

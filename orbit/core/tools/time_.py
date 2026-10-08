from __future__ import annotations

from datetime import datetime

from tzlocal import get_localzone

from .registry import tool


def now_text() -> str:
    tz = get_localzone()
    now = datetime.now(tz)
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {now.strftime('%A')} (timezone {tz.key})"


@tool
def current_time() -> str:
    """Get the current local date, time, weekday and timezone. Call this before computing any relative time such as "in 10 minutes" or "tomorrow 9am"."""
    return now_text()

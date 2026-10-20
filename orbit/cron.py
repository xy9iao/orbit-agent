"""Standard crontab expressions for APScheduler.

APScheduler 3 numbers the day-of-week field from Monday = 0, so `CronTrigger.from_crontab("0 19 * * 1,3")`
fires on Tuesday and Thursday instead of Monday and Wednesday, and `0` means Monday instead of Sunday.
`cron_trigger` rewrites numeric weekdays the crontab way (0 and 7 = Sunday) into day names first.
"""

from __future__ import annotations

import re
from datetime import tzinfo

from apscheduler.triggers.cron import CronTrigger

DAY_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")  # crontab order: 0 (and 7) = Sunday
_NUMERIC = re.compile(r"^(\d+)(?:-(\d+))?(?:/(\d+))?$")


def crontab_weekdays(field: str) -> str:
    """A crontab day-of-week field with numbers replaced by day names ("1,3" -> "mon,wed", "0" -> "sun")."""
    parts: list[str] = []
    for part in field.split(","):
        m = _NUMERIC.match(part)
        if not m:  # "*", "*/2", "mon-fri": APScheduler reads these the same way crontab does
            parts.append(part)
            continue
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) is not None else start
        step = int(m.group(3)) if m.group(3) is not None else 1
        if m.group(3) is not None and m.group(2) is None:  # "1/2" means from 1 to the end of the week
            end = 6
        if not (0 <= start <= 7 and 0 <= end <= 7 and start <= end and step > 0):
            raise ValueError(f"invalid day-of-week {part!r}")
        days = {DAY_NAMES[d % 7]: None for d in range(start, end + 1, step)}  # 7 is Sunday again; keep order
        parts.extend(days)
    return ",".join(dict.fromkeys(parts))


def cron_trigger(expr: str, timezone: tzinfo | str | None = None) -> CronTrigger:
    """CronTrigger for a 5-field crontab expression, with crontab weekday numbering."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"wrong number of fields; got {len(fields)}, expected 5")
    fields[4] = crontab_weekdays(fields[4])
    return CronTrigger.from_crontab(" ".join(fields), timezone=timezone)

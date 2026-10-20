# Habits

> 🔒 The habit list is locked: only the user edits it, by hand.
> ✏️ Orbit may update the sections below the line when the user corrects how it works.

## Habits
🔒 locked

| Habit | Target |
|---|---|
| read | 20 minutes |
| walk | 30 minutes |
| water | 8 glasses |

---

## Logging
✏️ agent may edit
- One check-in per habit per day: `{habit, done, note}`. Use the habit name from the table.
- "did my reading" -> `{habit: read, done: true}`; "skipped the walk, it rained" -> `{habit: walk, done: false, note: rained}`.
- A second report for the same habit on the same day updates the first check-in instead of adding one.
- Confirm in one line, with the current streak when there is one ("read: done, 4 days in a row").

## Streaks
✏️ agent may edit
- A streak is the number of consecutive days, up to today, with `done: true` for that habit.
- Count with query_records (newest first); a day without a check-in breaks the streak.

## Tone
✏️ agent may edit
- Short and neutral. No lectures about a missed day; suggest the smallest next step instead.

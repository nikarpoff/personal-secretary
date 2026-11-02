"""Bounded Telegram presentation of a task; no delivery or persistence concerns."""

import html

from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import aware_utc


def format_task_reminder(task: dict[str, Any], tz: ZoneInfo) -> str:
    # Clip raw text before escaping: never truncate an HTML tag or entity.
    title = html.escape(str(task.get("title", "Задача"))[:200])
    lines = [f"🔔 <b>{title}</b>"]
    due_at = aware_utc(task.get("due_at"))
    if due_at is not None:
        lines.append(f"Срок: {due_at.astimezone(tz):%d.%m.%Y %H:%M %Z}")
    source = str(task.get("source", ""))
    escaped_source = html.escape(source)
    if source.startswith("obsidian://open?") and len(escaped_source) <= 2200:
        # Bot API does not guarantee arbitrary URI schemes in HTML hrefs.
        lines.append(f"Obsidian (скопировать ссылку): <code>{escaped_source}</code>")
    return "\n".join(lines)

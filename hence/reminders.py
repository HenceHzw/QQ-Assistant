"""Shared time zone and validation for single reminders."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')


def validate_reminder(title, due, now):
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 300 or not isinstance(due, str):
        raise ValueError('invalid title or due')
    due = datetime.fromisoformat(due)
    if due.tzinfo is None or due.utcoffset() != timedelta(hours=8) or due <= now:
        raise ValueError('invalid due time')
    return title.strip(), int(due.timestamp())

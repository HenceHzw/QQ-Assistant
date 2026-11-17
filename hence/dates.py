"""Validation and display for date-based reminders (all times are Beijing time)."""
import re
from datetime import date, datetime, time, timedelta

from .reminders import ZONE


def validate_date_reminder(title, day, show_days, alerts, now):
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 300:
        raise ValueError('invalid title')
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('invalid date')
    event_day = date.fromisoformat(day)
    if event_day < now.astimezone(ZONE).date():
        raise ValueError('past date')
    if type(show_days) is not int or not 0 <= show_days <= 3660:
        raise ValueError('invalid display window')
    if not isinstance(alerts, list) or not 1 <= len(alerts) <= 20:
        raise ValueError('invalid alerts')
    times = []
    for alert in alerts:
        if not isinstance(alert, dict) or set(alert) != {'days', 'time'}:
            raise ValueError('invalid alert')
        days, clock = alert['days'], alert['time']
        if type(days) is not int or not 0 <= days <= 3660 or not isinstance(clock, str) or not re.fullmatch(r'\d{2}:\d{2}', clock):
            raise ValueError('invalid alert')
        hour, minute = map(int, clock.split(':'))
        alert_time = datetime.combine(event_day - timedelta(days=days), time(hour, minute), ZONE)
        times.append(int(alert_time.timestamp()))
    if len(set(times)) != len(times):
        raise ValueError('duplicate alerts')
    start = datetime.combine(event_day - timedelta(days=show_days), time.min, ZONE)
    end = datetime.combine(event_day + timedelta(days=1), time.min, ZONE)
    due = datetime.combine(event_day, time(9), ZONE)
    future = sorted(ts for ts in times if ts > now.timestamp())
    skipped = sorted(ts for ts in times if ts <= now.timestamp())
    return title.strip(), int(due.timestamp()), int(start.timestamp()), int(end.timestamp()), future, skipped


def parse_alerts(text):
    parts = text.split(',')
    if len(parts) > 20:
        raise ValueError('too many alerts')
    alerts = []
    for part in parts:
        match = re.fullmatch(r'(\d{1,4})@(\d{2}:\d{2})', part)
        if not match:
            raise ValueError('invalid alert format')
        alerts.append({'days': int(match[1]), 'time': match[2]})
    return alerts

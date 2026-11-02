"""Calendar days in the store's own timezone (pure Python, no database)."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Kolkata"
PERIOD_DAYS = {"today": 1, "7d": 7, "30d": 30}
PERIOD_LABELS = {"today": "today", "7d": "the last 7 days", "30d": "the last 30 days"}


def store_zone(name):
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def local_midnight(day, zone):
    return datetime.combine(day, time.min, tzinfo=zone)


def period_bounds(period, now, zone):
    """
    The period and the one before it, of equal length, as whole local calendar days.
    "7d" is today and the 6 days before it. "today" runs from midnight until now, and is
    compared with all of yesterday. Each range is [start, end) as timezone-aware datetimes.
    """
    days = PERIOD_DAYS[period]
    today = now.astimezone(zone).date()
    first = today - timedelta(days=days - 1)
    previous_first = first - timedelta(days=days)
    return {
        "days": days,
        "first_day": first,
        "last_day": today,
        "previous_first_day": previous_first,
        "previous_last_day": first - timedelta(days=1),
        "start": local_midnight(first, zone),
        "end": local_midnight(today + timedelta(days=1), zone),
        "previous_start": local_midnight(previous_first, zone),
        "previous_end": local_midnight(first, zone),
    }

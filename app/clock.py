"""Single source of time for the whole app.

All timestamps are stored in UTC (naive datetimes that mean UTC) and shown in IST by the UI.
Tests can move the clock forward (e.g. to simulate a missed IPC interval) instead of sleeping.
"""
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

_offset = timedelta(0)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0) + _offset


def plant_today() -> date:
    """Today's date at the plant (IST). Used for expiry and calibration checks."""
    return (utcnow() + timedelta(hours=5, minutes=30)).date()


def iso(dt: datetime | None) -> str | None:
    """Serialise a stored UTC datetime as ISO-8601 with a Z suffix."""
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


def ist(dt: datetime | None) -> str:
    """Human-readable IST string for reports."""
    if not dt:
        return ""
    return (dt.replace(tzinfo=timezone.utc).astimezone(IST)).strftime("%d-%b-%Y %H:%M:%S IST")


# --- test helpers ---------------------------------------------------------
def advance(minutes: float):
    global _offset
    _offset += timedelta(minutes=minutes)


def reset():
    global _offset
    _offset = timedelta(0)

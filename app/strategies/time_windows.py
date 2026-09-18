"""US-session windows that deserve a more careful entry.

Two windows account for the entries that keep getting stopped: the data release
at 08:30 ET and the first hour after the equity open, where a release spike or an
opening drive is entered at market just as it finishes and the reversal takes the
stop out. The window is expressed in US Eastern time, so daylight saving is
resolved once here rather than in every caller - the underlying schedule is fixed
in New York and only its UTC equivalent moves.

No client clock is involved: the server knows real UTC, so nothing has to be
collected from the EA for this to work.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# 08:30 ET is the regular US data release; 09:30-10:30 ET is the opening drive
# and its first reversal. Both are in the same window, so one range covers them.
DEFAULT_WINDOW_START = time(8, 30)
DEFAULT_WINDOW_END = time(10, 30)

# The last three hours before the US equity close. Both strategies lost money in
# this band - win rates of 14% to 32% across it - because volume thins out, the
# ATR contracts, and the stop that follows the ATR tightens with it until ordinary
# noise takes it out. No new entries are taken here; positions already open keep
# their protective levels.
DEFAULT_LATE_WINDOW_START = time(13, 0)
DEFAULT_LATE_WINDOW_END = time(16, 0)

_US_EASTERN_STANDARD = timedelta(hours=-5)
_US_EASTERN_DAYLIGHT = timedelta(hours=-4)


def now_utc() -> datetime:
    """The current instant in UTC.

    Indirected so a caller - or a test - can decide what "now" means without
    touching the clock, which keeps a strategy run reproducible.
    """
    return datetime.now(timezone.utc)


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    # weekday(): Monday is 0, Sunday is 6.
    offset = (6 - first.weekday()) % 7
    return date(year, month, 1 + offset + (n - 1) * 7)


def us_eastern_offset(now_utc: datetime) -> timedelta:
    """The US Eastern offset in force at this UTC instant.

    Daylight saving runs from 02:00 local on the second Sunday of March, i.e.
    07:00 UTC, to 02:00 local on the first Sunday of November, i.e. 06:00 UTC.
    The rule is written out rather than taken from a timezone database so it
    behaves the same on the server and on a developer machine without tzdata.
    """
    moment = now_utc.astimezone(timezone.utc)
    starts = datetime.combine(_nth_sunday(moment.year, 3, 2), time(7, 0), tzinfo=timezone.utc)
    ends = datetime.combine(_nth_sunday(moment.year, 11, 1), time(6, 0), tzinfo=timezone.utc)
    if starts <= moment < ends:
        return _US_EASTERN_DAYLIGHT
    return _US_EASTERN_STANDARD


def in_us_entry_window(
    now_utc: datetime,
    *,
    start: time = DEFAULT_WINDOW_START,
    end: time = DEFAULT_WINDOW_END,
    enabled: bool = True,
) -> bool:
    """Whether the cautious-entry window is open right now."""
    return _within(now_utc, start=start, end=end, enabled=enabled)


def in_us_late_window(
    now_utc: datetime,
    *,
    start: time = DEFAULT_LATE_WINDOW_START,
    end: time = DEFAULT_LATE_WINDOW_END,
    enabled: bool = True,
) -> bool:
    """Whether the quiet end of the US session is in force right now."""
    return _within(now_utc, start=start, end=end, enabled=enabled)


def _within(now_utc: datetime, *, start: time, end: time, enabled: bool) -> bool:
    if not enabled:
        return False
    eastern = now_utc.astimezone(timezone.utc) + us_eastern_offset(now_utc)
    return start <= eastern.time() < end


def clock_time(value: Any, fallback: time) -> time:
    """Parse an "HH:MM" deployment setting, falling back when it is unusable."""
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        hour, _, minute = text.partition(":")
        return time(int(hour), int(minute or 0))
    except (TypeError, ValueError):
        return fallback


def guard_enabled(config: dict[str, Any], key: str) -> bool:
    """Whether a deployment has left a guard switched on."""
    return config.get(key) not in (False, 0, "0", "false", "off", "no")


def late_window(config: dict[str, Any]) -> tuple[time, time] | None:
    """The quiet end of the US session a deployment closes, or None when off."""
    if not guard_enabled(config, "late_session_guard"):
        return None
    start = clock_time(config.get("late_window_start"), DEFAULT_LATE_WINDOW_START)
    end = clock_time(config.get("late_window_end"), DEFAULT_LATE_WINDOW_END)
    if start >= end:
        return None
    return start, end

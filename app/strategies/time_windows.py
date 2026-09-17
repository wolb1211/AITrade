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

_US_EASTERN_STANDARD = timedelta(hours=-5)
_US_EASTERN_DAYLIGHT = timedelta(hours=-4)


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
    if not enabled:
        return False
    eastern = now_utc.astimezone(timezone.utc) + us_eastern_offset(now_utc)
    return start <= eastern.time() < end

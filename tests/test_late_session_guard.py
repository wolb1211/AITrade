"""The quiet end of the US session is closed to new entries.

Measured over recent history both strategies lost money in the last three hours
before the US equity close: win rates of 14% to 38% across the band, and the hours
cost more than the good ones earned. Volume thins out there, the ATR contracts,
and because the stop follows the ATR it tightens with it until ordinary noise
takes it out. The window is written in Eastern time so daylight saving is handled
once, and it applies to add-ons as well because an add-on is a new entry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.strategies import time_windows


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_the_late_window_follows_daylight_saving() -> None:
    """14:00-16:00 New York is 18:00-20:00 UTC in summer, 19:00-21:00 in winter.

    20:30 UTC is therefore past the close in summer and still inside it in winter;
    a window written in UTC would be an hour wrong for half the year.
    """
    assert time_windows.in_us_late_window(_utc("2026-07-15T18:00:00")) is True   # 14:00 ET
    assert time_windows.in_us_late_window(_utc("2026-07-15T20:30:00")) is False  # 16:30 ET
    assert time_windows.in_us_late_window(_utc("2026-01-15T18:00:00")) is True   # 13:00 ET
    assert time_windows.in_us_late_window(_utc("2026-01-15T20:30:00")) is True   # 15:30 ET
    # Morning in New York is never in it.
    assert time_windows.in_us_late_window(_utc("2026-07-15T12:30:00")) is False  # 08:30 ET


def test_the_late_guard_is_configurable_and_can_be_switched_off() -> None:
    assert time_windows.late_window({}) == (time_windows.DEFAULT_LATE_WINDOW_START, time_windows.DEFAULT_LATE_WINDOW_END)
    assert time_windows.late_window({"late_session_guard": False}) is None
    assert time_windows.late_window({"late_session_guard": "off"}) is None
    start, end = time_windows.late_window({"late_window_start": "14:30", "late_window_end": "15:30"})
    assert (start.hour, start.minute) == (14, 30)
    assert (end.hour, end.minute) == (15, 30)
    # An unusable or inverted range falls back rather than closing a strange window.
    assert time_windows.late_window({"late_window_start": "16:00", "late_window_end": "14:00"}) is None


def test_the_trend_strategy_refuses_to_open_in_the_late_window(monkeypatch) -> None:
    from app.strategies import turtle_agent

    monkeypatch.setattr(time_windows, "now_utc", lambda: _utc("2026-07-15T18:00:00"))
    request = SimpleNamespace(
        request_id="late-window-001", symbol="GBPUSD.c", timeframe="M15",
        bid=1.3, ask=1.3002, candles=[],
    )

    decision = turtle_agent.TurtleTrendStrategy().evaluate_open(request, {"config": {}})

    assert decision.status == "HOLD"
    assert "尾盘" in decision.reason


def test_the_price_action_strategy_refuses_to_open_in_the_late_window(monkeypatch) -> None:
    from app.strategies import pa_agent_lite

    monkeypatch.setattr(time_windows, "now_utc", lambda: _utc("2026-07-15T18:00:00"))
    request = SimpleNamespace(
        request_id="late-window-002", symbol="XAUUSD.c", timeframe="M5",
        bid=4300.0, ask=4300.2, candles=[],
    )

    decision = pa_agent_lite.PaAgentLiteStrategy().evaluate_open(request, {"config": {}})

    assert decision.status == "HOLD"
    assert "尾盘" in decision.reason

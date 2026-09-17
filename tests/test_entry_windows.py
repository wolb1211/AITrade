"""The two "the move just finished" conditions, for both strategies.

A release spike or an opening drive entered at market lands at the end of the
move. Both strategies now wait for a pullback in that case - and in the US
data/open window - so this file checks the trigger, the level it waits at, and
that the window follows daylight saving rather than a fixed UTC hour.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.strategies import pa_agent_lite, time_windows, turtle_agent

_INSIDE_WINDOW = datetime(2026, 7, 15, 13, 0, tzinfo=timezone.utc)  # 09:00 ET


def _bar(high: float, low: float) -> SimpleNamespace:
    return SimpleNamespace(high=high, low=low, open=(high + low) / 2, close=(high + low) / 2, volume=1.0)


def _gl(**overrides: object) -> tuple[bool, float, str]:
    values: dict[str, object] = {
        "candles": [_bar(4301.0, 4299.0)],
        "direction": "buy",
        "upper": 4300.0,
        "lower": 4290.0,
        "donchian_fired": True,
        "atr": 5.0,
        "config": {},
    }
    values.update(overrides)
    return turtle_agent._pending_entry_conditions(**values)  # type: ignore[arg-type]


def test_a_gl_spike_waits_at_the_middle_of_the_spike() -> None:
    """The newest move's midpoint wins over the channel it broke.

    It is the level a retracement of that bar would reach and it sits nearer the
    market, so the pending order is more likely to be filled.
    """
    force, anchor, reason = _gl(candles=[_bar(4312.0, 4298.0)])  # 14 wide vs ATR 5

    assert force is True
    assert anchor == pytest.approx(4305.0)
    assert "尖峰" in reason


def test_a_gl_spike_without_a_channel_waits_at_the_middle() -> None:
    force, anchor, _reason = _gl(candles=[_bar(4312.0, 4298.0)], donchian_fired=False)

    assert force is True
    assert anchor == pytest.approx(4305.0)


def test_gl_does_not_wait_on_an_ordinary_bar() -> None:
    assert _gl() == (False, 0.0, "")


def test_the_gl_window_forces_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time_windows, "now_utc", lambda: _INSIDE_WINDOW)

    force, anchor, reason = _gl()

    assert force is True
    assert anchor == pytest.approx(4300.0)
    assert "窗口" in reason


def test_a_gl_deployment_can_switch_the_window_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time_windows, "now_utc", lambda: _INSIDE_WINDOW)

    assert _gl(config={"us_window_guard": False}) == (False, 0.0, "")


def test_the_pa_window_forces_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time_windows, "now_utc", lambda: _INSIDE_WINDOW)
    request = SimpleNamespace(candles=[_bar(4301.0, 4299.0)], symbol="XAUUSD", timeframe="M5")

    force, price, reason = pa_agent_lite._cautious_entry_conditions(
        request=request, config={}, atr=5.0
    )

    assert force is True
    assert price is None  # falls back to the level the server wanted
    assert "窗口" in reason


def test_the_pa_spike_waits_at_the_middle_of_the_bar() -> None:
    request = SimpleNamespace(candles=[_bar(4312.0, 4298.0)], symbol="XAUUSD", timeframe="M5")

    force, price, reason = pa_agent_lite._cautious_entry_conditions(
        request=request, config={"us_window_guard": False}, atr=5.0
    )

    assert force is True
    assert price == pytest.approx(4305.0)
    assert "尖峰" in reason

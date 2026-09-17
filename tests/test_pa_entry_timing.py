"""A market entry that has already run away from the setup is entered late.

Production showed the price-action strategy taking every entry at market: the
model's order_type never reached the server, so a prompt asking for a pullback
entry still executed at the signal price - the end of the move - and the first
retracement stopped it out. The entry is now re-priced as a pending order at the
level the server had in mind, and a deployment may instead stand aside.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.strategies import pa_agent_lite, time_windows


def _entry(**overrides: object) -> tuple[str, float | None, str]:
    values: dict[str, object] = {
        "order_type": "market",
        "ai_entry": None,
        "market_entry": 4310.0,
        "ideal_entry": 4300.0,
        "atr": 4.0,
        "config": {},
    }
    values.update(overrides)
    return pa_agent_lite._avoid_chasing_entry(**values)  # type: ignore[arg-type]


def test_a_chased_market_order_becomes_a_pending_one() -> None:
    order_type, entry, note = _entry()

    assert order_type == "limit"
    assert entry == pytest.approx(4300.0)
    assert "追离" in note


def test_a_market_order_near_the_ideal_entry_is_left_alone() -> None:
    order_type, entry, note = _entry(market_entry=4301.0)

    assert order_type == "market"
    assert entry is None
    assert note == ""


def test_a_deployment_may_stand_aside_instead_of_waiting() -> None:
    order_type, _entry_price, note = _entry(config={"chase_fallback": "reject"})

    assert order_type == ""
    assert "不追单" in note


def test_a_pending_order_the_model_chose_is_its_own_call() -> None:
    order_type, entry, note = _entry(order_type="limit", ai_entry=4295.0)

    assert order_type == "limit"
    assert entry == pytest.approx(4295.0)
    assert note == ""


def test_the_chase_limit_can_be_switched_off() -> None:
    order_type, _entry_price, note = _entry(config={"max_market_chase_atr": 0})

    assert order_type == "market"
    assert note == ""


def test_without_an_atr_nothing_is_re_priced() -> None:
    """A missing ATR means the distance cannot be judged, so nothing changes."""
    order_type, _entry_price, note = _entry(atr=0.0)

    assert order_type == "market"
    assert note == ""


def test_a_forced_window_converts_even_a_near_entry() -> None:
    """The US release/opening window does not wait for a chase to develop."""
    order_type, entry, note = _entry(
        market_entry=4300.5,
        force_pending=True,
        pending_price=4298.0,
        force_reason="美盘数据/开盘窗口内不追单",
    )

    assert order_type == "limit"
    assert entry == pytest.approx(4298.0)
    assert "开盘窗口" in note


def test_a_forced_conversion_falls_back_to_the_ideal_entry() -> None:
    """Without a spike midpoint the level the server wanted is used."""
    order_type, entry, _note = _entry(force_pending=True, pending_price=None)

    assert order_type == "limit"
    assert entry == pytest.approx(4300.0)


def _us(now: str) -> datetime:
    return datetime.fromisoformat(now).replace(tzinfo=timezone.utc)


def test_the_us_window_follows_daylight_saving() -> None:
    """08:30 New York is 12:30 UTC in summer and 13:30 UTC in winter.

    The schedule is fixed in New York, so the same UTC hour falls inside the
    window in one season and outside it in the other; a window written in UTC
    would be an hour wrong for half the year.
    """
    # 12:30 UTC: 08:30 ET in July (inside), 07:30 ET in January (outside).
    assert time_windows.in_us_entry_window(_us("2026-07-15T12:30:00")) is True
    assert time_windows.in_us_entry_window(_us("2026-01-15T12:30:00")) is False
    # 13:30 UTC: 09:30 ET in July, 08:30 ET in January - inside both.
    assert time_windows.in_us_entry_window(_us("2026-07-15T13:30:00")) is True
    assert time_windows.in_us_entry_window(_us("2026-01-15T13:30:00")) is True
    # 15:30 UTC is 11:30 ET in July and 10:30 ET in January - past the end.
    assert time_windows.in_us_entry_window(_us("2026-07-15T15:30:00")) is False
    assert time_windows.in_us_entry_window(_us("2026-01-15T15:30:00")) is False


def test_the_us_window_can_be_switched_off() -> None:
    assert time_windows.in_us_entry_window(_us("2026-07-15T12:30:00"), enabled=False) is False


def test_a_spike_bar_forces_the_entry_to_wait_for_a_pullback() -> None:
    """A bar far wider than the ATR is the move, already finished."""
    bar = SimpleNamespace(high=4310.0, low=4290.0, open=4308.0, close=4292.0, volume=1.0)
    request = SimpleNamespace(candles=[bar], symbol="GBPUSD.c", timeframe="M15")

    force, price, reason = pa_agent_lite._cautious_entry_conditions(
        request=request, config={}, atr=5.0
    )

    assert force is True
    assert price == pytest.approx(4300.0)  # the middle of the spike bar
    assert "尖峰" in reason


def test_an_ordinary_bar_does_not_force_anything() -> None:
    bar = SimpleNamespace(high=4301.0, low=4299.0, open=4300.0, close=4300.5, volume=1.0)
    request = SimpleNamespace(candles=[bar], symbol="GBPUSD.c", timeframe="M15")

    force, price, reason = pa_agent_lite._cautious_entry_conditions(
        request=request, config={"us_window_guard": False}, atr=5.0
    )

    assert force is False
    assert price is None
    assert reason == ""


def test_the_spike_limit_can_be_tuned_or_switched_off() -> None:
    bar = SimpleNamespace(high=4310.0, low=4300.0, open=4305.0, close=4301.0, volume=1.0)
    request = SimpleNamespace(candles=[bar], symbol="GBPUSD.c", timeframe="M15")

    # 10 wide against ATR 5 is 2.0x: inside the default, outside a looser limit.
    assert pa_agent_lite._cautious_entry_conditions(
        request=request, config={"us_window_guard": False, "spike_bar_atr": 3}, atr=5.0
    )[0] is False
    assert pa_agent_lite._cautious_entry_conditions(
        request=request, config={"us_window_guard": False, "spike_bar_atr": 1.5}, atr=5.0
    )[0] is True
    assert pa_agent_lite._cautious_entry_conditions(
        request=request, config={"us_window_guard": False, "spike_bar_atr": 0}, atr=5.0
    )[0] is False

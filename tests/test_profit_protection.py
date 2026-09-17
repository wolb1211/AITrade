"""Protecting a basket's profit once it has been made.

Two live cases drove this: a four-unit basket peaked well in profit and gave all
of it back because nothing looked at the give-back, and a young basket's unified
stop sat below where it opened, so an ordinary pullback stopped the whole basket
at a loss. Both are mechanical - neither depends on the AI noticing.
"""

from __future__ import annotations

import pytest

from app.models import AccountIdentity, Candle, PositionEvaluateRequest, PositionSnapshot
from app.strategies import turtle_agent


def _bar(timestamp: int, high: float, low: float) -> Candle:
    middle = (high + low) / 2
    return Candle(timestamp=timestamp, open=middle, high=high, low=low, close=middle, volume=1.0)


def _snapshot(
    ticket: str = "1",
    *,
    side: str = "BUY",
    entry: float = 4300.0,
    opened_at: int = 1000,
) -> PositionSnapshot:
    return PositionSnapshot(
        ticket=ticket,
        symbol="XAUUSD",
        side=side,
        volume=0.10,
        open_price=entry,
        current_price=entry,
        profit=0.0,
        open_time=opened_at,
    )


def _request(positions: list[PositionSnapshot], *, bid: float, ask: float) -> PositionEvaluateRequest:
    return PositionEvaluateRequest(
        deployment_key="gl_profit_protection",
        request_id="gl-profit-protection-001",
        account=AccountIdentity(login="10001"),
        bar_time=1100,
        symbol="XAUUSD",
        timeframe="M15",
        bid=bid,
        ask=ask,
        spread_points=0.2,
        positions=positions,
    )


def test_the_basket_stop_floor_defaults_to_the_first_entry() -> None:
    positions = [_snapshot(entry=4300.0), _snapshot("2", entry=4310.0)]

    assert turtle_agent._basket_stop_floor(
        positions=positions, side="BUY", config={}
    ) == pytest.approx(4300.0)
    assert turtle_agent._basket_stop_floor(
        positions=positions, side="BUY", config={"basket_stop_floor": "farthest_entry"}
    ) == pytest.approx(4310.0)
    assert turtle_agent._basket_stop_floor(
        positions=positions, side="BUY", config={"basket_stop_floor": "off"}
    ) == 0.0
    # A sell basket mirrors it: the entry it must not go beyond is the highest.
    sells = [_snapshot(entry=4300.0, side="SELL"), _snapshot("2", entry=4310.0, side="SELL")]
    assert turtle_agent._basket_stop_floor(
        positions=sells, side="SELL", config={}
    ) == pytest.approx(4310.0)


def test_an_add_keeps_the_basket_stop_at_the_first_entry() -> None:
    """With adds 0.5 ATR apart the unified stop lands under the first entry.

    At the default 1.0 ATR spacing the stop lands at or above it, so a deployment
    that adds more tightly is where the floor does its work.
    """
    positions = [_snapshot(entry=4300.0), _snapshot("2", entry=4305.0)]
    request = _request(positions, bid=4310.0, ask=4310.2)
    config = {"allow_add": True, "max_positions": 4, "add_step_atr": 0.5}

    decision, _reason = turtle_agent._maybe_add(request, config, 10.0)

    assert decision is not None
    # Without the floor: 4310.2 - 2 x 10 = 4290.2, under where the basket opened.
    assert decision.sl == pytest.approx(4300.0)


def test_the_floor_can_be_switched_off() -> None:
    positions = [_snapshot(entry=4300.0), _snapshot("2", entry=4305.0)]
    request = _request(positions, bid=4310.0, ask=4310.2)
    config = {"allow_add": True, "max_positions": 4, "add_step_atr": 0.5, "basket_stop_floor": "off"}

    decision, _reason = turtle_agent._maybe_add(request, config, 10.0)

    assert decision is not None
    assert decision.sl == pytest.approx(4290.2)


def test_a_basket_that_gave_back_half_its_peak_is_taken_off() -> None:
    positions = [_snapshot(entry=4300.0), _snapshot("2", entry=4310.0)]
    candles = [_bar(1100, 4340.0, 4300.0)]  # peak 40 above the first entry
    request = _request(positions, bid=4312.0, ask=4312.2)  # back to 12

    decision = turtle_agent._give_back_decision(request, positions, {}, 10.0, candles)

    assert decision is not None
    assert decision.action == "CLOSE"
    assert "回吐" in decision.reason


def test_holding_most_of_the_peak_leaves_the_basket_alone() -> None:
    positions = [_snapshot(entry=4300.0), _snapshot("2", entry=4310.0)]
    candles = [_bar(1100, 4340.0, 4300.0)]
    request = _request(positions, bid=4325.0, ask=4325.2)  # 25 of 40 kept

    assert turtle_agent._give_back_decision(request, positions, {}, 10.0, candles) is None


def test_a_peak_not_worth_protecting_is_ignored() -> None:
    """Early noise must not close a basket that has barely moved."""
    positions = [_snapshot(entry=4300.0)]
    candles = [_bar(1100, 4310.0, 4300.0)]  # peak 10 = 1.0 ATR
    request = _request(positions, bid=4301.0, ask=4301.2)

    assert turtle_agent._give_back_decision(request, positions, {}, 10.0, candles) is None


def test_the_give_back_rule_can_be_switched_off_and_tuned() -> None:
    positions = [_snapshot(entry=4300.0), _snapshot("2", entry=4310.0)]
    candles = [_bar(1100, 4340.0, 4300.0)]
    request = _request(positions, bid=4312.0, ask=4312.2)

    assert turtle_agent._give_back_decision(
        request, positions, {"give_back_ratio": 0}, 10.0, candles
    ) is None
    # A looser ratio keeps the basket: 12 of 40 is above 0.25 x 40.
    assert turtle_agent._give_back_decision(
        request, positions, {"give_back_ratio": 0.25}, 10.0, candles
    ) is None
    # A tighter one takes it: 12 of 40 is under 0.75 x 40.
    assert turtle_agent._give_back_decision(
        request, positions, {"give_back_ratio": 0.75}, 10.0, candles
    ) is not None


def test_without_an_open_time_the_rule_stands_down() -> None:
    """A peak measured over bars from before the basket would fire far too early.

    The trailing stop covers this case, so refusing to act is the safe answer.
    """
    positions = [_snapshot(entry=4300.0, opened_at=0)]
    candles = [_bar(1100, 4390.0, 4300.0)]  # a peak from someone else's move
    request = _request(positions, bid=4312.0, ask=4312.2)

    assert turtle_agent._give_back_decision(request, positions, {}, 10.0, candles) is None


def test_a_sell_basket_is_measured_the_same_way() -> None:
    positions = [_snapshot(entry=4310.0, side="SELL"), _snapshot("2", entry=4300.0, side="SELL")]
    candles = [_bar(1100, 4310.0, 4260.0)]  # peak 50 below the first entry
    request = _request(positions, bid=4296.0, ask=4296.2)  # back to 14

    decision = turtle_agent._give_back_decision(request, positions, {}, 10.0, candles)

    assert decision is not None
    assert decision.action == "CLOSE"

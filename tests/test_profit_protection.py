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
    current: float | None = None,
) -> PositionSnapshot:
    return PositionSnapshot(
        ticket=ticket,
        symbol="XAUUSD",
        side=side,
        volume=0.10,
        open_price=entry,
        current_price=entry if current is None else current,
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


def test_a_single_unit_add_keeps_the_two_atr_stop() -> None:
    """The floor must not raise the stop to the entry when there is one unit.

    Production saw a long basket whose add-on carried a stop above the market:
    it had been raised to the entry price itself, only a fraction of an ATR from
    the new entry, and the broker refused every order as an invalid stop.
    """
    positions = [_snapshot(entry=1.33477)]
    # The add needs the price 1.0 ATR beyond the entry, so the entry sits 1.2 ATR
    # below the market - exactly where the floor used to take over.
    request = _request(positions, bid=1.33610, ask=1.33620)
    config = {"allow_add": True, "max_positions": 4, "add_step_atr": 1.0}

    decision, _reason = turtle_agent._maybe_add(request, config, 0.0011)

    assert decision is not None
    assert decision.sl == pytest.approx(1.33620 - 2 * 0.0011)
    # The entry is nowhere near the stop: the old behaviour put it at the entry.
    assert decision.sl < positions[0].open_price


def test_a_stale_quote_stops_the_server_acting() -> None:
    """One client sent a quote about forty points from its own live price.

    Both values come from the same payload, so a gap that large means the quote
    is stale and anything computed from it - like a basket stop - is worthless.
    """
    positions = [_snapshot(entry=1.33477, current=1.33700)]
    stale = _request(positions, bid=1.34120, ask=1.34140)

    reason = turtle_agent._quote_divergence(stale, {}, 0.0011)
    assert reason is not None
    assert "数据过期" in reason
    # A quote that agrees with the positions is accepted.
    assert turtle_agent._quote_divergence(
        _request(positions, bid=1.33700, ask=1.33710), {}, 0.0011
    ) is None
    # And the check can be switched off.
    assert turtle_agent._quote_divergence(stale, {"max_quote_divergence_atr": 0}, 0.0011) is None


def test_the_whole_basket_goes_to_break_even_together() -> None:
    """The newest unit is the one a pullback kills while the older keeps its profit.

    Live case: two units at +83 and +22 points, the first past its own ATR and the
    second nowhere near, so a pullback stopped the second at a loss. Once the
    basket as a whole is ahead, every unit moves to the weighted average entry.
    """
    positions = [
        _snapshot(entry=4300.0, current=4308.0),        # well ahead of its own entry
        _snapshot("2", entry=4306.0, current=4308.0),   # barely ahead
    ]
    request = _request(positions, bid=4308.0, ask=4308.2)

    decision = turtle_agent._protection_batch_decision(
        request,
        {"break_even_atr": 1.0, "trailing_start_atr": 1.5, "trailing_distance_atr": 1.0},
        4.0,
    )

    assert decision is not None
    levels = {item["ticket"]: item["sl"] for item in decision.metadata["batch_actions"]}
    # The second unit has no target of its own yet, so the basket level protects it:
    # weighted average entry = (4300 + 4306) / 2 = 4303, and the basket is 5 points
    # ahead of it, over the 1.0 ATR trigger at ATR 4.
    assert levels["2"] == pytest.approx(4303.0)
    # The first unit is already 2 ATR ahead, so its own trailing stop is tighter and
    # wins: 4308 - 1.0 ATR = 4304. Nothing is loosened.
    assert levels["1"] == pytest.approx(4304.0)


def test_a_basket_barely_ahead_is_left_alone() -> None:
    positions = [
        _snapshot(entry=4300.0, current=4301.0),
        _snapshot("2", entry=4300.0, current=4301.0),
    ]
    request = _request(positions, bid=4301.0, ask=4301.2)

    assert turtle_agent._protection_batch_decision(request, {}, 4.0) is None


def test_a_single_unit_does_not_use_the_basket_rule() -> None:
    """With one unit the per-unit rules already cover it."""
    request = _request([_snapshot(entry=4300.0, current=4310.0)], bid=4310.0, ask=4310.2)

    assert turtle_agent._basket_break_even_level(request, {}, 4.0) is None


def test_the_basket_rule_can_be_switched_off_and_tuned() -> None:
    positions = [
        _snapshot(entry=4300.0, current=4308.0),
        _snapshot("2", entry=4306.0, current=4308.0),
    ]
    request = _request(positions, bid=4308.0, ask=4308.2)

    assert turtle_agent._basket_break_even_level(request, {"basket_breakeven_atr": 0}, 4.0) is None
    # 1.25 ATR ahead: inside the default 1.0, outside a tightened 1.5.
    assert turtle_agent._basket_break_even_level(
        request, {"basket_breakeven_atr": 1.5}, 4.0
    ) is None
    assert turtle_agent._basket_break_even_level(
        request, {"basket_breakeven_atr": 1.2}, 4.0
    ) is not None


def test_a_sell_basket_mirrors_it() -> None:
    positions = [
        _snapshot(entry=4310.0, side="SELL", current=4304.0),
        _snapshot("2", entry=4308.0, side="SELL", current=4304.0),
    ]
    request = _request(positions, bid=4304.0, ask=4304.2)

    level, favorable = turtle_agent._basket_break_even_level(request, {}, 4.0)

    assert level == pytest.approx(4309.0)    # weighted average entry
    assert favorable == pytest.approx(1.2)   # (4309 - 4304.2) / 4, measured from the ask


def test_a_sell_basket_is_measured_the_same_way() -> None:
    positions = [_snapshot(entry=4310.0, side="SELL"), _snapshot("2", entry=4300.0, side="SELL")]
    candles = [_bar(1100, 4310.0, 4260.0)]  # peak 50 below the first entry
    request = _request(positions, bid=4296.0, ask=4296.2)  # back to 14

    decision = turtle_agent._give_back_decision(request, positions, {}, 10.0, candles)

    assert decision is not None
    assert decision.action == "CLOSE"

"""The broker's minimum stop distance has to be respected.

MT5 answers a stop that sits closer to the market than the symbol's
``SYMBOL_TRADE_STOPS_LEVEL`` with "Invalid S/L or T/P" (retcode 10016), so the
position keeps whatever stop it already had. A live gold basket looked
untrailed for exactly that reason while both the server and the EA were doing
their job: the server asked, the EA forwarded, the broker refused.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.strategies import pa_agent_lite, turtle_agent
from app.strategies.stop_rules import min_stop_distance, respect_min_stop, stop_is_placeable


def test_min_stop_distance_prefers_the_broker_figure() -> None:
    assert min_stop_distance({"point": 0.01, "stops_level": 250}, {}) == pytest.approx(2.5)
    assert min_stop_distance(
        {"point": 0.01}, {"min_stop_distance_points": 100}
    ) == pytest.approx(1.0)
    assert min_stop_distance(
        {"point": 0.01}, {"min_stop_distance_price": 3.0}
    ) == pytest.approx(3.0)
    # Nothing reported and nothing configured leaves every stop unchanged, which
    # is the behaviour every deployment had before this existed.
    assert min_stop_distance({}, {}) == 0.0


def test_a_buy_stop_is_pulled_below_the_broker_minimum() -> None:
    info = {"point": 0.01, "stops_level": 250}  # 2.50 in price

    level, enforced = respect_min_stop(4300.0, side="BUY", bid=4301.0, ask=4301.2, info=info)
    assert level == pytest.approx(4298.5)
    assert enforced == pytest.approx(2.5)

    # Far enough away: returned untouched, and nothing is recorded.
    level, enforced = respect_min_stop(4290.0, side="BUY", bid=4301.0, ask=4301.2, info=info)
    assert level == pytest.approx(4290.0)
    assert enforced == 0.0


def test_a_sell_stop_is_pushed_above_the_broker_minimum() -> None:
    info = {"point": 0.01, "stops_level": 250}  # 2.50 in price

    # Ask 4301.2, so a sell stop may not sit below 4303.7.
    level, enforced = respect_min_stop(4302.0, side="SELL", bid=4301.0, ask=4301.2, info=info)
    assert level == pytest.approx(4303.7)
    assert enforced == pytest.approx(2.5)

    level, enforced = respect_min_stop(4310.0, side="SELL", bid=4301.0, ask=4301.2, info=info)
    assert level == pytest.approx(4310.0)
    assert enforced == 0.0


def _position(**overrides: object) -> SimpleNamespace:
    values = {
        "side": "BUY",
        "ticket": "1001",
        "open_price": 4300.0,
        "sl": None,
        "tp": None,
        "volume": 0.10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _request(**overrides: object) -> SimpleNamespace:
    values = {
        "request_id": "req-stop-distance",
        "symbol": "XAUUSD",
        "bid": 4310.0,
        "ask": 4310.2,
        "symbol_info": {"point": 0.01, "stops_level": 250},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_gl_trailing_pulls_back_to_a_level_the_broker_accepts() -> None:
    """A target the broker would refuse is moved to the closest usable level."""
    # 1.0 ATR trailing distance with ATR 2.0 would sit at 4308.0, only 2.0 from
    # the market, so the broker minimum of 2.50 wins.
    decision = turtle_agent._trailing_stop_decision(_request(), _position(), {}, 2.0)

    assert decision is not None
    assert decision.sl == pytest.approx(4307.5)
    assert decision.metadata["min_stop_distance_enforced"] == pytest.approx(2.5)
    # The decision records what the server resolved, so a client that reports a
    # stops level can be told apart from one that does not.
    assert decision.metadata["min_stop_distance_used"] == pytest.approx(2.5)


def test_gl_trailing_is_unchanged_when_the_target_is_already_far_enough() -> None:
    decision = turtle_agent._trailing_stop_decision(_request(), _position(), {}, 6.0)

    assert decision is not None
    assert decision.sl == pytest.approx(4304.0)
    assert decision.metadata["min_stop_distance_enforced"] == 0.0


def test_pa_trailing_pulls_back_to_a_level_the_broker_accepts() -> None:
    """PA trails at a fixed 0.5 ATR, which is tighter than some brokers allow."""
    position = _position(sl=4300.0)
    # ATR 4.0: break-even triggers at +2, the trailing leg sits 0.5 ATR (2.0)
    # behind the market, and the broker minimum of 2.50 is wider than that.
    decision = pa_agent_lite._atr_protective_stop(_request(), position, atr=4.0)

    assert decision is not None
    assert decision.sl == pytest.approx(4307.5)


def test_a_stop_the_market_has_already_passed_is_not_placeable() -> None:
    """The broker rejects a stop that is not on the market's correct side.

    This symbol reported no minimum distance at all, so the side is the only
    thing left to check - and it is the case a level computed a moment earlier
    runs into when the market moves before the request is submitted.
    """
    assert stop_is_placeable(4299.0, side="BUY", bid=4301.0, ask=4301.2) is True
    assert stop_is_placeable(4301.5, side="BUY", bid=4301.0, ask=4301.2) is False
    assert stop_is_placeable(4301.0, side="SELL", bid=4301.0, ask=4301.2) is False
    assert stop_is_placeable(4302.0, side="SELL", bid=4301.0, ask=4301.2) is True
    # A zero stop is "no stop", which is never sent as a modification.
    assert stop_is_placeable(0.0, side="BUY", bid=4301.0, ask=4301.2) is False


def test_a_client_without_a_stops_level_still_gets_a_floor() -> None:
    """No reported level and no override still leaves a minimum in force.

    A quarter ATR is small enough that it cannot widen a strategy's stop - every
    rule here places a stop at least one ATR from the market - so it acts purely
    as a floor for a client nobody has updated yet.
    """
    assert min_stop_distance({}, {}, atr=8.0) == pytest.approx(2.0)
    assert min_stop_distance({}, {"min_stop_distance_atr": 0.5}, atr=8.0) == pytest.approx(4.0)
    # A deployment can switch the floor off, and without an ATR nothing is known.
    assert min_stop_distance({}, {"min_stop_distance_atr": 0}, atr=8.0) == 0.0
    assert min_stop_distance({}, {}, atr=0.0) == 0.0
    # The broker's own figure still wins over the fallback.
    assert min_stop_distance(
        {"point": 0.01, "stops_level": 250}, {"min_stop_distance_atr": 1.0}, atr=8.0
    ) == pytest.approx(2.5)


def test_a_stop_only_moves_when_it_is_worth_sending() -> None:
    """A stop that improves by a tick costs a round trip for nothing.

    Production saw eighteen modifications in forty minutes on one symbol, and on
    some brokers every extra modification is another chance of a refused stop.
    """
    request = _request()  # bid 4310.0, so a 1.0 ATR target with ATR 2.0 is 4308.0

    # 0.2 ATR step = 0.4, so an improvement of 1.0 goes through.
    assert turtle_agent._trailing_stop_decision(request, _position(sl=4307.0), {}, 2.0) is not None
    # An improvement of 0.2 does not.
    assert turtle_agent._trailing_stop_decision(request, _position(sl=4307.8), {}, 2.0) is None
    # With the step switched off, the small one moves again. No broker minimum
    # here, otherwise the clamp would pull the target below the stop in force.
    assert turtle_agent._trailing_stop_decision(
        _request(symbol_info={}), _position(sl=4307.9), {"trailing_min_step_atr": 0}, 2.0
    ) is not None


def test_the_stop_improvement_rule_never_loosens_a_stop() -> None:
    assert turtle_agent._stop_improves(4300.0, None, side="BUY", minimum_gain=0.5) is True
    assert turtle_agent._stop_improves(4300.4, 4300.0, side="BUY", minimum_gain=0.5) is False
    assert turtle_agent._stop_improves(4300.5, 4300.0, side="BUY", minimum_gain=0.5) is True
    # Without a step the move still has to be strictly better than what is there.
    assert turtle_agent._stop_improves(4300.0, 4300.0, side="BUY", minimum_gain=0.0) is False
    assert turtle_agent._stop_improves(4300.1, 4300.0, side="BUY", minimum_gain=0.0) is True
    # Sells mirror it: a lower stop is the improvement.
    assert turtle_agent._stop_improves(4299.6, 4300.0, side="SELL", minimum_gain=0.5) is False
    assert turtle_agent._stop_improves(4299.5, 4300.0, side="SELL", minimum_gain=0.5) is True
    assert turtle_agent._stop_improves(4300.0, 4300.0, side="SELL", minimum_gain=0.0) is False


def test_the_add_step_is_half_the_stop_distance() -> None:
    """A quarter left a full basket risking more than twice one unit.

    At 0.5 ATR the four units sat 0.5 ATR apart while the basket stop is 2 ATR
    below the furthest entry, so the stop ended up below the first entry and a
    full basket lost 4.5 ATR. At 1.0 it ends above the first entry.
    """
    assert turtle_agent.DEFAULT_ADD_STEP_ATR == pytest.approx(1.0)
    assert turtle_agent.DEFAULT_ADD_STEP_ATR == pytest.approx(turtle_agent.STOP_ATR / 2)

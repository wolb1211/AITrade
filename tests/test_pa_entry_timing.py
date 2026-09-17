"""A market entry that has already run away from the setup is entered late.

Production showed the price-action strategy taking every entry at market: the
model's order_type never reached the server, so a prompt asking for a pullback
entry still executed at the signal price - the end of the move - and the first
retracement stopped it out. The entry is now re-priced as a pending order at the
level the server had in mind, and a deployment may instead stand aside.
"""

from __future__ import annotations

import pytest

from app.strategies import pa_agent_lite


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

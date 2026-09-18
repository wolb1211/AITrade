"""A pending order whose setup is gone is cancelled by the server.

A pending entry waits at the level the setup wanted. When the price runs away
instead, the signal it was waiting for no longer exists, and an order left in the
market can fill much later on it. A client reported exactly that: a limit order
the price never came back to, still sitting there.

The client keeps sending its pending orders in the position list, so the decision
is made here and the client only executes it.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.strategies.pending_orders import cancel_stale_pending_orders


def _pending(ticket: str = "9001", price: float = 1.3000, placed_at: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        ticket=ticket, symbol="GBPUSD.c", side="BUY", volume=0.1,
        open_price=price, current_price=price, open_time=placed_at,
    )


def test_a_pending_order_the_price_left_behind_is_cancelled() -> None:
    actions = cancel_stale_pending_orders(
        [_pending(price=1.3000)], bid=1.3060, ask=1.3062, atr=0.0010, config={}
    )

    assert len(actions) == 1
    assert actions[0]["action"] == "cancel"
    assert actions[0]["ticket"] == "9001"
    assert "离开挂单价" in actions[0]["comment"]


def test_a_pending_order_still_within_reach_is_left_alone() -> None:
    """The order is waiting for a pullback: while it can still happen, it stays."""
    actions = cancel_stale_pending_orders(
        [_pending(price=1.3000)], bid=1.3010, ask=1.3012, atr=0.0010, config={}
    )

    assert actions == []


def test_a_pending_order_that_waited_too_long_is_cancelled() -> None:
    actions = cancel_stale_pending_orders(
        [_pending(price=1.3000, placed_at=1_000_000)],
        bid=1.3002, ask=1.3004, atr=0.0010, config={},
        timeframe="M15", now_epoch=1_000_000 + 4 * 900,
    )

    assert len(actions) == 1
    assert "等待" in actions[0]["comment"]


def test_an_order_inside_its_time_limit_is_left_alone() -> None:
    actions = cancel_stale_pending_orders(
        [_pending(price=1.3000, placed_at=1_000_000)],
        bid=1.3002, ask=1.3004, atr=0.0010, config={},
        timeframe="M15", now_epoch=1_000_000 + 2 * 900,
    )

    assert actions == []


def test_a_missing_placement_time_only_disables_the_age_rule() -> None:
    """Without a placement time the distance rule still applies."""
    actions = cancel_stale_pending_orders(
        [_pending(price=1.3000, placed_at=0)],
        bid=1.3060, ask=1.3062, atr=0.0010, config={},
        timeframe="M15", now_epoch=2_000_000,
    )

    assert len(actions) == 1


def test_the_whole_guard_can_be_switched_off() -> None:
    pending = [_pending(price=1.3000, placed_at=1_000_000)]
    assert cancel_stale_pending_orders(
        pending, bid=1.3060, ask=1.3062, atr=0.0010, config={"pending_cancel_guard": False},
        timeframe="M15", now_epoch=1_000_000 + 100 * 900,
    ) == []
    assert cancel_stale_pending_orders(
        pending, bid=1.3060, ask=1.3062, atr=0.0010,
        config={"pending_max_distance_atr": 0, "pending_max_bars": 0},
    ) == []


def test_the_limits_can_be_tuned() -> None:
    pending = [_pending(price=1.3000)]
    # 0.6 ATR away: inside the default 1.5, outside a tightened 0.5.
    assert cancel_stale_pending_orders(
        pending, bid=1.3006, ask=1.3008, atr=0.0010, config={"pending_max_distance_atr": 1.5}
    ) == []
    assert len(cancel_stale_pending_orders(
        pending, bid=1.3006, ask=1.3008, atr=0.0010, config={"pending_max_distance_atr": 0.5}
    )) == 1


def test_a_sell_pending_order_is_measured_from_the_ask() -> None:
    pending = [SimpleNamespace(
        ticket="9002", symbol="GBPUSD.c", side="SELL", volume=0.1,
        open_price=1.3100, current_price=1.3100, open_time=0,
    )]

    actions = cancel_stale_pending_orders(
        pending, bid=1.3040, ask=1.3042, atr=0.0010, config={}
    )

    assert len(actions) == 1
    assert actions[0]["direction"] == "sell"

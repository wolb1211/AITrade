"""Deciding when a pending order is no longer worth leaving in the market.

A pending entry is placed at the level the setup wanted, waiting for a pullback.
When the price instead runs away the setup it was waiting for is gone, and an
order left behind is worse than no order: it can fill much later on a signal that
no longer exists. A client reported exactly that - a limit order that never came
back and was never withdrawn.

The client keeps sending its pending orders, so the decision is made here and the
client only executes it. Two conditions, either of which is enough:

* the market has moved ``pending_max_distance_atr`` away from the pending price;
* the order has been waiting more than ``pending_max_bars`` bars.

Both can be switched off with 0, and ``pending_cancel_guard`` switches the whole
rule off for a deployment.
"""

from __future__ import annotations

from typing import Any

DEFAULT_PENDING_MAX_DISTANCE_ATR = 1.5
DEFAULT_PENDING_MAX_BARS = 3

_TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}


def _number(config: dict[str, Any], key: str, default: float) -> float:
    """A setting where an explicit 0 means zero rather than "unset"."""
    value = config.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _guard_enabled(config: dict[str, Any]) -> bool:
    return config.get("pending_cancel_guard") not in (False, 0, "0", "false", "off", "no")


def cancel_stale_pending_orders(
    pending: list[Any],
    *,
    bid: float,
    ask: float,
    atr: float,
    config: dict[str, Any],
    timeframe: str = "",
    now_epoch: int = 0,
) -> list[dict[str, Any]]:
    """Return a cancel action for every pending order that no longer applies."""
    if not pending or not _guard_enabled(config):
        return []

    max_distance = _number(config, "pending_max_distance_atr", DEFAULT_PENDING_MAX_DISTANCE_ATR)
    max_bars = _number(config, "pending_max_bars", DEFAULT_PENDING_MAX_BARS)
    bar_seconds = _TIMEFRAME_SECONDS.get(str(timeframe or "").upper(), 0)

    actions: list[dict[str, Any]] = []
    for item in pending:
        price = float(getattr(item, "open_price", 0) or 0)
        if price <= 0:
            continue
        side = str(getattr(item, "side", "") or "").upper()
        market = float(bid) if side == "BUY" else float(ask)
        reasons: list[str] = []

        if max_distance > 0 and atr > 0:
            distance = abs(market - price)
            if distance > max_distance * atr:
                reasons.append(f"价格已离开挂单价 {distance / atr:.1f} 倍ATR")
        if max_bars > 0 and bar_seconds > 0 and now_epoch > 0:
            placed_at = int(getattr(item, "open_time", 0) or 0)
            if placed_at > 0 and (now_epoch - placed_at) > max_bars * bar_seconds:
                bars = (now_epoch - placed_at) / bar_seconds
                reasons.append(f"挂单已等待 {bars:.1f} 根K线")

        if not reasons:
            continue
        actions.append({
            "action": "cancel",
            "ticket": str(getattr(item, "ticket", "") or ""),
            "direction": "buy" if side == "BUY" else "sell",
            "price": price,
            "comment": "挂单失效取消：" + "，".join(reasons),
        })
    return actions

"""Broker limits on where a protective stop may sit.

MT5 rejects a stop that sits closer to the market than the symbol's
``SYMBOL_TRADE_STOPS_LEVEL`` with retcode 10016 ("Invalid S/L or T/P"). A live
gold basket showed exactly that: the server kept asking the EA to lift a stop,
the EA forwarded the request, the broker refused every time, and the position
kept its old level - so the trailing looked broken from the chart while both the
server and the EA were working.

The rule is applied here so both strategies respect it, and an EA that does not
report the level yet can still be protected through the deployment config.
"""

from __future__ import annotations

from typing import Any, Mapping

# The symbol-info keys an EA may use for the minimum distance, in points.
_STOPS_LEVEL_KEYS = ("stops_level", "trade_stops_level", "stopsLevel", "stop_level")
_POINT_KEYS = ("point", "point_size", "tick_size", "trade_tick_size", "tick")


# Applied when the client does not report a stops level and no deployment
# override is set. A quarter ATR is small in absolute terms - it cannot make a
# strategy hold a materially wider stop - but it keeps a target from being
# parked essentially at the market, which is the shape brokers refuse. Set
# min_stop_distance_atr to 0 on a deployment to turn it off.
DEFAULT_MIN_STOP_DISTANCE_ATR = 0.25


def min_stop_distance(
    info: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
    *,
    atr: float = 0.0,
) -> float:
    """Smallest allowed distance between the market and a stop, in price.

    The layers, in order:

    1. the broker's own ``stops_level`` when the client reports it - exact;
    2. ``min_stop_distance_points`` from the deployment config;
    3. ``min_stop_distance_price`` from the deployment config;
    4. a quarter ATR by default, so a client that reports nothing is still kept
       away from the market and needs no EA update to benefit;
    5. nothing at all when the ATR is unknown or the fallback is switched off.
    """
    info = info if isinstance(info, Mapping) else {}
    config = config if isinstance(config, Mapping) else {}

    point = _positive(info, *_POINT_KEYS)
    points = _positive(info, *_STOPS_LEVEL_KEYS)
    if point and points:
        return point * points

    configured_points = _positive(config, "min_stop_distance_points")
    if point and configured_points:
        return point * configured_points

    configured_price = _positive(config, "min_stop_distance_price")
    if configured_price:
        return configured_price

    if atr <= 0:
        return 0.0
    factor = config.get("min_stop_distance_atr")
    try:
        factor_value = float(factor) if factor is not None else DEFAULT_MIN_STOP_DISTANCE_ATR
    except (TypeError, ValueError):
        factor_value = DEFAULT_MIN_STOP_DISTANCE_ATR
    if factor_value <= 0:
        return 0.0
    return float(atr) * factor_value


def respect_min_stop(
    sl: float,
    *,
    side: str,
    bid: float,
    ask: float,
    info: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    atr: float = 0.0,
) -> tuple[float, float]:
    """Pull a stop back to the closest level the broker will accept.

    Returns the usable level and the distance that had to be enforced, so the
    caller can record that the stop was moved for a reason other than the
    strategy's own rule. A stop that is already far enough away is returned
    untouched with a distance of zero, which keeps every existing behaviour.
    """
    distance = min_stop_distance(info, config, atr=atr)
    if distance <= 0 or float(sl or 0) <= 0:
        return sl, 0.0

    if str(side or "").upper() == "BUY":
        ceiling = float(bid) - distance
        if float(sl) > ceiling:
            return ceiling, distance
        return sl, 0.0

    floor = float(ask) + distance
    if float(sl) < floor:
        return floor, distance
    return sl, 0.0


def stop_is_placeable(
    sl: float,
    *,
    side: str,
    bid: float,
    ask: float,
    info: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
    atr: float = 0.0,
) -> bool:
    """Whether the broker would accept this stop as things stand right now.

    Besides its own minimum distance, MT5 rejects a stop that is not on the
    correct side of the market (retcode 10016 again). A trailing level computed
    a moment earlier can already be behind the market when the request is
    submitted, and sending it costs a round trip and an error. Callers use this
    to leave the existing stop in place instead until the market allows it.
    """
    level, _ = respect_min_stop(
        sl, side=side, bid=bid, ask=ask, info=info, config=config, atr=atr,
    )
    if float(level or 0) <= 0:
        return False
    if str(side or "").upper() == "BUY":
        return float(level) < float(bid)
    return float(level) > float(ask)


def _positive(source: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = float(source.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0

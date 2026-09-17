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


def min_stop_distance(
    info: Mapping[str, Any] | None,
    config: Mapping[str, Any] | None,
) -> float:
    """Smallest allowed distance between the market and a stop, in price.

    The broker's own figure wins when the EA reports it. Otherwise a deployment
    may set ``min_stop_distance_points`` (points) or ``min_stop_distance_price``
    (an absolute price distance); with neither set nothing is enforced, which is
    the behaviour every deployment had before.
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
    return _positive(config, "min_stop_distance_price")


def respect_min_stop(
    sl: float,
    *,
    side: str,
    bid: float,
    ask: float,
    info: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[float, float]:
    """Pull a stop back to the closest level the broker will accept.

    Returns the usable level and the distance that had to be enforced, so the
    caller can record that the stop was moved for a reason other than the
    strategy's own rule. A stop that is already far enough away is returned
    untouched with a distance of zero, which keeps every existing behaviour.
    """
    distance = min_stop_distance(info, config)
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


def _positive(source: Mapping[str, Any], *keys: str) -> float:
    for key in keys:
        try:
            value = float(source.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0

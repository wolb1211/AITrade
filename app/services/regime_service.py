"""Multi-timeframe trend read for the GL arbitrage EA (GL_ARBITRAGE_V1).

The arbitrage EA trades entirely on its own; the server is asked one question
only: what are the long, mid and short term trends doing right now. The answer is
built in two layers.

* A deterministic read from the bars the EA uploads - moving averages, ATR, where
  the close sits inside the recent channel, and the slope of the slow average.
  This always produces an answer, and it is the answer whenever the AI layer is
  unavailable or unclear.
* An AI opinion on top of those numbers. The hard cases - a range that is quietly
  turning into a trend, or a trend that has stalled - are exactly what a fixed
  rule gets wrong, and the EA uses the result as a reference rather than as an
  order.

Only closed bars are used: the EA uploads finished candles, so the newest bar is
treated as the latest closed one, matching every other engine in the service.
"""

from __future__ import annotations

from math import floor, log10
from typing import Any

from app.models import Candle

TREND_BULL = "bull"
TREND_BEAR = "bear"
TREND_RANGE = "range"
TREND_UNKNOWN = "unknown"

# Ordered short -> mid -> long: the EA reads the answer under these three names.
REGIME_LABELS: tuple[str, ...] = ("short", "mid", "long")

# The data contract of this endpoint is deliberately hardcoded rather than
# configured in the admin console. The endpoint exists for one strategy, so the
# shape is fixed: three timeframes, one answer slot each. Changing these means
# changing this line and the EA together, which is the honest cost of a private
# contract - a console field would only let the two drift apart silently.
#
# M15 because the EA polls on a fifteen minute timer; H4 and D1 because the
# strategy is a hedge basket that lives for hours to days, and only the higher
# timeframes say whether it is fighting a one-way market.
REGIME_PERIODS: tuple[str, ...] = ("M15", "H4", "D1")

# Bars used per timeframe, and how long one answer stays valid. 100 bars covers a
# 30 period average with context and is what the EA is asked to upload. The five
# minute window mainly lets several EAs on the same symbol share one paid call.
REGIME_KLINE_COUNT = 100
REGIME_CACHE_SECONDS = 300

# The only timeframe keys that may appear as a secondary timeframe.
ALLOWED_REGIME_TIMEFRAMES = frozenset({"M1", "M5", "M15", "M30", "H1", "H4", "D1"})

_RULE_TREND_TEXT = {
    TREND_BULL: "规则判定为多头",
    TREND_BEAR: "规则判定为空头",
    TREND_RANGE: "规则判定为震荡",
    TREND_UNKNOWN: "规则无法判定",
}


def _round_significant(value: float | None, digits: int = 4) -> float | None:
    """Round to a few significant digits.

    The numbers travel to a model and into the cache key, so trailing float noise
    is pure cost: it lengthens the prompt and makes two calls that describe the
    same market look different.
    """
    if value is None:
        return None
    number = float(value)
    if number == 0 or number != number:  # zero, or NaN
        return 0.0 if number == 0 else None
    if number in (float("inf"), float("-inf")):
        return None
    exponent = floor(log10(abs(number)))
    factor = 10 ** (digits - 1 - exponent)
    return round(number * factor) / factor


def _closes(candles: list[Candle]) -> list[float]:
    return [float(item.close) for item in candles]


def _moving_average(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / float(period)


def _atr(candles: list[Candle], period: int = 14) -> float | None:
    """Average true range over the last ``period`` bars.

    A plain mean of the true range rather than Wilder smoothing: the value is
    only ever compared against itself in the same read (gaps in ATR units), so
    the smoother variant would change nothing but the code volume.
    """
    if len(candles) < 2:
        return None
    window = candles[-(period + 1):]
    ranges: list[float] = []
    for index in range(1, len(window)):
        current = window[index]
        previous = window[index - 1]
        ranges.append(
            max(
                float(current.high) - float(current.low),
                abs(float(current.high) - float(previous.close)),
                abs(float(current.low) - float(previous.close)),
            )
        )
    if not ranges:
        return None
    return sum(ranges) / float(len(ranges))


def timeframe_features(
    candles: list[Candle],
    *,
    fast_period: int = 10,
    slow_period: int = 30,
    atr_period: int = 14,
    channel_period: int = 20,
    slope_period: int = 10,
) -> dict[str, Any]:
    """Objective read of one timeframe, in ATR units wherever it is a distance.

    ATR units matter: gold at 2000 and gold at 4000 look identical once a gap is
    divided by the average range, which is what makes the same thresholds usable
    across instruments and across the years.
    """
    features: dict[str, Any] = {
        "bars": len(candles),
        "timeframe_ready": len(candles) >= max(slow_period, atr_period, channel_period) + 1,
    }
    if not candles:
        features["rule_trend"] = TREND_UNKNOWN
        return features

    closes = _closes(candles)
    close = closes[-1]
    ma_fast = _moving_average(closes, fast_period)
    ma_slow = _moving_average(closes, slow_period)
    atr = _atr(candles, atr_period)
    channel_window = candles[-channel_period:] if len(candles) >= channel_period else candles
    channel_high = max(float(item.high) for item in channel_window)
    channel_low = min(float(item.low) for item in channel_window)
    slope_base = _moving_average(closes[:-slope_period], slow_period) if len(closes) > slope_period else None

    features.update(
        {
            "close": _round_significant(close),
            "ma_fast": _round_significant(ma_fast),
            "ma_slow": _round_significant(ma_slow),
            "atr": _round_significant(atr),
            "channel_high": _round_significant(channel_high),
            "channel_low": _round_significant(channel_low),
            "recent_closes": [_round_significant(value) for value in closes[-8:]],
        }
    )
    if atr and atr > 0:
        features["ma_gap_atr"] = _round_significant((ma_fast - ma_slow) / atr) if None not in (ma_fast, ma_slow) else None
        features["ma_slope_atr"] = _round_significant((ma_slow - slope_base) / atr) if None not in (ma_slow, slope_base) else None
        features["change_atr"] = _round_significant((close - closes[-slow_period]) / atr) if len(closes) >= slow_period else None
        features["channel_width_atr"] = _round_significant((channel_high - channel_low) / atr)
    width = channel_high - channel_low
    features["channel_position"] = (
        _round_significant((close - channel_low) / width) if width > 0 else None
    )
    features["rule_trend"] = rule_trend(features)
    return features


def rule_trend(features: dict[str, Any]) -> str:
    """The deterministic trend verdict, used as-is when the AI adds nothing.

    Both a wide enough average gap and a slow average that is actually moving are
    required before a trend is called: a fresh cross with a flat slow average is
    the signature of a range, and calling it a trend is what makes an add-on
    basket march into a market that keeps coming back.
    """
    gap = features.get("ma_gap_atr")
    slope = features.get("ma_slope_atr")
    close = features.get("close")
    ma_fast = features.get("ma_fast")
    if not features.get("timeframe_ready") or gap is None or slope is None:
        return TREND_UNKNOWN
    gap = float(gap)
    slope = float(slope)
    above = close is not None and ma_fast is not None and float(close) >= float(ma_fast)
    below = close is not None and ma_fast is not None and float(close) < float(ma_fast)
    if gap >= 0.35 and slope >= 0.10 and above:
        return TREND_BULL
    if gap <= -0.35 and slope <= -0.10 and below:
        return TREND_BEAR
    return TREND_RANGE


def normalize_trend_text(value: Any) -> str:
    """Map whatever the model wrote onto the four trend words the EA compares.

    Answers come back in Chinese as often as in English, and an unmapped word
    would leave the EA reading an empty trend, so both languages and the common
    synonyms are folded here.
    """
    text = str(value or "").strip().lower()
    if not text:
        return TREND_UNKNOWN
    if text in {TREND_BULL, TREND_BEAR, TREND_RANGE, TREND_UNKNOWN}:
        return text
    if any(token in text for token in ("bull", "up", "多", "涨", "上行", "上涨")):
        return TREND_BULL
    if any(token in text for token in ("bear", "down", "空", "跌", "下行", "下跌")):
        return TREND_BEAR
    if any(token in text for token in ("range", "side", "flat", "震荡", "盘整", "整理", "区间")):
        return TREND_RANGE
    return TREND_UNKNOWN


def rule_detail(features: dict[str, Any]) -> str:
    """A Chinese one-liner the EA panel can show when the AI said nothing useful.

    Written from the same numbers the AI saw, so the fallback reads like a weaker
    version of the AI answer instead of an error message.
    """
    if not features.get("bars"):
        return "该周期没有K线数据，无法判断趋势。"
    if not features.get("timeframe_ready"):
        return f"该周期K线只有 {features.get('bars', 0)} 根，数据不足，暂按震荡处理。"
    trend_text = _RULE_TREND_TEXT.get(str(features.get("rule_trend")), "规则无法判定")
    gap = features.get("ma_gap_atr")
    slope = features.get("ma_slope_atr")
    position = features.get("channel_position")
    parts = [trend_text]
    if gap is not None:
        parts.append(f"快慢均线差 {gap} ATR")
    if slope is not None:
        parts.append(f"慢均线斜率 {slope} ATR")
    if position is not None:
        parts.append(f"现价处于近20根区间 {round(float(position) * 100)}% 位置")
    return "，".join(parts) + "。"

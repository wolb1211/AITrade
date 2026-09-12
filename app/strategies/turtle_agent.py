from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import Candle, OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision
from app.services.ai_service import AiDecisionClient

# ---------------------------------------------------------------------------
# Strategy parameters.
#
# None of these are exposed to the user; they live here so that tuning happens in
# one place instead of at scattered call sites.  Where the strategy also reads a
# matching deployment config key (admin side), that key still wins and the
# constant below is the fallback.
# ---------------------------------------------------------------------------

# Entry: Donchian channel over this many closed bars (the newest bar excluded).
DEFAULT_ENTRY_PERIOD = 20
# Exit: opposite channel over this many closed bars.
DEFAULT_EXIT_PERIOD = 10
# ATR period used by every stop and exit calculation.
DEFAULT_ATR_PERIOD = 20

# Protective stop distance from the entry price, expressed in ATR.
STOP_ATR = 2.0

# Swing (price-action) entry system, used alongside the channel.
SWING_MIN_BARS = 30             # closed bars required before pivots are trusted
SWING_PIVOT_SPAN = 2            # bars required on each side of a confirmed pivot
SWING_PULLBACK_MIN_ATR = 0.5    # smallest pullback depth that qualifies
SWING_CONFIRMATION_BARS = 5     # window searched for a confirmation bar
SWING_EMA_FAST = 5              # confirmation EMA cross, fast period
SWING_EMA_SLOW = 10             # confirmation EMA cross, slow period
PIN_BAR_WICK_RATIO = 2.0        # pin-bar wick must be this many times the body

# Protection ladder: break-even first, then a trailing stop.
DEFAULT_BREAK_EVEN_ATR = 1.0         # favourable move that triggers break-even
DEFAULT_BREAK_EVEN_OFFSET = 0.0      # extra offset from entry, on top of the spread buffer
BREAK_EVEN_SPREAD_BUFFER = 1.5       # break-even must clear this many times the spread
DEFAULT_TRAILING_START_ATR = 1.5     # favourable move that starts trailing
DEFAULT_TRAILING_DISTANCE_ATR = 1.0  # trailing distance behind the current price

# Adding units.
MAX_UNITS = 4                        # this strategy never pyramids beyond this
DEFAULT_ADD_STEP_ATR = 0.5           # extension beyond the furthest entry allowing an add

# Proactive exit requested by the AI review.  The protective stop and the exit
# channel stay in force regardless, so declining to close is always safe.
PROACTIVE_EXIT_MIN_BARS = 1          # closed bars required before the AI may close early

# Sizing fallbacks used when the EA/broker does not provide contract details.
DEFAULT_MIN_LOT = 0.01
DEFAULT_LOT_STEP = 0.01


class TurtleTrendStrategy:
    """Deterministic current-timeframe Turtle/GL Trend strategy.

    The core signal and risk prices are calculated locally.  AI integration is
    intentionally kept outside this first version so an AI response cannot
    alter the breakout, unit size, or protective stop.
    """

    code = "GL_TREND_V1"

    def __init__(self, ai_client: AiDecisionClient | None = None) -> None:
        self.ai_client = ai_client

    def evaluate_open(self, request: OpenEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        period = _positive_int(config.get("entry_period"), DEFAULT_ENTRY_PERIOD)
        atr_period = _positive_int(config.get("atr_period"), DEFAULT_ATR_PERIOD)
        candles = _ordered(request.candles)
        minimum_bars = max(period + 1, atr_period + 2, SWING_MIN_BARS)
        if len(candles) < minimum_bars:
            return _hold_open(request, f"行情数据不足：已收盘 K 线少于{minimum_bars}根，本根不做判断")
        atr = _atr(candles, atr_period)
        # Both entry systems run on every bar.  The channel breakout is the
        # original turtle rule; the swing system covers the range-bound stretches
        # where a pure channel breakout tends to fire at the end of a move.
        swing_direction, swing_analysis = _swing_pullback_signal(candles, atr, config)
        window = candles[-period - 1:-1]
        close = candles[-1].close
        upper = max(item.high for item in window)
        lower = min(item.low for item in window)
        donchian_direction = "buy" if close > upper else "sell" if close < lower else ""
        if swing_direction and donchian_direction and swing_direction != donchian_direction:
            # Both systems read the trend, so a genuine disagreement means the
            # chart is ambiguous: stand aside for this bar.
            return _hold_open(
                request,
                "趋势信号冲突：回调结构偏"
                f"{_cn_direction(swing_direction)}、区间突破偏{_cn_direction(donchian_direction)}，"
                "两套趋势判断方向不一致，本根放弃入场",
            )
        if swing_direction:
            direction, entry_analysis = swing_direction, f"回调趋势确认：{swing_analysis}"
        elif donchian_direction:
            direction = donchian_direction
            entry_analysis = (
                f"突破趋势确认：收盘价{close:g}上破前{period}根 K 线区间高点{upper:g}，顺势入场"
            )
        else:
            return _hold_open(request, "趋势尚未形成：区间突破与回调结构均未成立，继续等待")
        entry = request.ask if direction == "buy" else request.bid
        sl = entry - STOP_ATR * atr if direction == "buy" else entry + STOP_ATR * atr
        lot, sizing = _unit_lot(request, config, entry=entry, stop_loss=sl)
        if lot <= 0:
            return _hold_open(request, "手数无法确定：当前止损距离与品种合约参数不匹配，本根不入场")
        decision = TradeDecision(
            decision_id=_id(), request_id=request.request_id, status="APPROVED",
            action="BUY" if direction == "buy" else "SELL", symbol=request.symbol,
            confidence=1.0, reason=entry_analysis,
            expires_at=_expires(), lot=lot, entry=entry, sl=sl, tp=None,
            metadata={
                "strategy_code": self.code,
                "position_sizing": sizing,
                "max_units": _max_units(config),
                "allow_add": bool(config.get("allow_add", False)),
                "entry_analysis": entry_analysis,
                # Both entry systems are reported so a decision log shows which
                # one fired and whether they ever disagreed.
                "swing_direction": swing_direction,
                "donchian_direction": donchian_direction,
            },
        )
        if self.ai_client is not None:
            allowed, risk_note, usage = self._ai_allows_open(request, deployment, decision, atr)
            if usage is not None:
                decision.usage = usage
            if not allowed:
                hold = _hold_open(request, f"AI 风险评估未通过：{risk_note}")
                hold.usage = usage
                return hold
            decision.metadata["ai_risk"] = risk_note
            # Show the AI verdict in the EA panel, not only in the metadata.
            decision.reason = f"{decision.reason}；{risk_note}"
        return decision

    def evaluate_position(self, request: PositionEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        period = _positive_int(config.get("exit_period"), DEFAULT_EXIT_PERIOD)
        atr_period = _positive_int(config.get("atr_period"), DEFAULT_ATR_PERIOD)
        candles = _ordered(request.candles)
        first_ticket = request.positions[0].ticket
        if len(candles) < max(period + 1, atr_period + 2):
            return _hold_position(
                request,
                first_ticket,
                f"行情数据不足：已收盘 K 线少于{max(period + 1, atr_period + 2)}根，暂不判断离场",
            )
        atr = _atr(candles, atr_period)
        window = candles[-period - 1:-1]
        channel_low = min(item.low for item in window)
        channel_high = max(item.high for item in window)
        # The basket is managed as one position, not unit by unit: the channel is
        # market-wide and the protective stop is a single level for the whole
        # basket, so an exit closes every unit of that side in one response
        # instead of peeling them off one at a time or waiting for each unit's
        # own stop.
        stop_levels = _basket_stop_levels(request.positions, atr)
        for side in ("BUY", "SELL"):
            members = [item for item in request.positions if item.side == side]
            if not members:
                continue
            stop_level = stop_levels[side]
            if side == "BUY":
                channel_broken = any(item.current_price < channel_low for item in members)
                stop_hit = any(item.current_price <= stop_level for item in members)
                channel_reason = (
                    f"趋势转弱：价格跌破前{period}根 K 线区间低点{channel_low:g}，全部离场"
                )
            else:
                channel_broken = any(item.current_price > channel_high for item in members)
                stop_hit = any(item.current_price >= stop_level for item in members)
                channel_reason = (
                    f"趋势转强：价格突破前{period}根 K 线区间高点{channel_high:g}，全部离场"
                )
            if channel_broken:
                return _close_basket(request, members, channel_reason)
            if stop_hit:
                return _close_basket(
                    request,
                    members,
                    f"触发保护止损（距最远入场 {STOP_ATR:g} 倍 ATR，止损位 {stop_level:g}），全部离场",
                )
        add_decision = _maybe_add(request, config, atr)
        if self.ai_client is None:
            protection = _protection_batch_decision(request, config, atr)
            if protection is not None:
                return protection
            if add_decision is not None:
                return add_decision
            return _hold_position(request, first_ticket, "趋势结构未被破坏，继续持有")
        # The AI is asked on every position request, including the bars where a
        # break-even or trailing update is due: taking profit is the AI's call,
        # while moving the stop stays the strategy's own logic.  Only the safety
        # exits above (basket stop and exit channel) run without asking it.
        review, review_usage = self._ai_position_review(
            request, deployment, add_decision, stop_levels, candles, atr,
        )
        if review["close"]:
            decision = _close_basket(
                request,
                list(request.positions),
                _with_ai_analysis(f"止盈离场：{review['reason']}", review),
            )
            decision.usage = review_usage
            return decision
        protection = _protection_batch_decision(request, config, atr)
        if protection is not None:
            protection.reason = _with_ai_analysis(protection.reason, review)
            protection.usage = review_usage
            return protection
        if add_decision is not None:
            add_decision.metadata["ai_risk"] = review["note"]
            add_decision.usage = review_usage
            if not review["allow_add"]:
                hold = _hold_position(
                    request,
                    first_ticket,
                    _with_ai_analysis(f"AI 判定暂不宜加仓：{review['reason']}", review),
                )
                hold.usage = review_usage
                return hold
            add_decision.reason = _with_ai_analysis(add_decision.reason, review)
            return add_decision
        hold = _hold_position(
            request,
            first_ticket,
            _with_ai_analysis("趋势结构未被破坏，继续持有", review),
        )
        hold.metadata["ai_risk"] = review["note"]
        hold.usage = review_usage
        return hold

    def _ai_position_review(
        self,
        request: PositionEvaluateRequest,
        deployment: dict[str, Any],
        add_candidate: TradeDecision | None,
        stop_levels: dict[str, float],
        candles: list[Candle],
        atr: float,
    ) -> tuple[dict[str, Any], Any]:
        """One AI call per position poll: proactive exit plus add-on verdict."""
        signal = {
            "protective_stop_levels": stop_levels,
            "stop_atr": STOP_ATR,
            "atr": atr,
            "position_summary": [
                {
                    "side": item.side,
                    "volume": item.volume,
                    "open_price": item.open_price,
                    "current_price": item.current_price,
                    "profit": item.profit,
                    "favorable_atr": _favorable_atr(item, atr),
                    "sl": item.sl,
                    "tp": item.tp,
                    "open_time": item.open_time,
                    "bars_since_open": _bars_since_open(candles, item),
                }
                for item in request.positions
            ],
        }
        if add_candidate is not None:
            signal["add_candidate"] = {
                "direction": "buy" if add_candidate.action == "BUY" else "sell",
                "entry": add_candidate.entry,
                "stop_loss": add_candidate.sl,
                "lot": add_candidate.lot,
                "units_open": len(request.positions),
                "max_units": _max_units(deployment.get("config") or {}),
            }
        else:
            signal["add_candidate"] = None

        result = self.ai_client.turtle_position_review(
            deployment=deployment,
            request_payload=request,
            signal=signal,
        )
        if result is None:
            return {
                "close": False,
                "allow_add": True,
                "reason": "",
                "analysis": "",
                "note": "AI 未返回结果，按策略规则执行",
            }, None

        reason = str(result.content.get("reason") or "").strip()
        ai_analysis = str(result.content.get("analysis") or "").strip()
        risk_level = str(result.content.get("risk_level") or "").strip().lower()
        close_requested = _truthy(result.content.get("close_now"))
        fresh_guard = _too_fresh_to_close(candles, request)
        # Recorded on every branch so a suppressed proactive exit stays visible.
        guard_note = (
            "；持仓不足一根已收盘 K 线，本根不提前离场"
            if close_requested and fresh_guard
            else ""
        )
        if close_requested and not fresh_guard:
            return {
                "close": True,
                "allow_add": True,
                "reason": reason or "AI 判断应当提前锁定利润",
                "analysis": ai_analysis,
                "note": f"止盈离场（AI 风险等级：{_cn_risk_level(risk_level)}）：{reason}",
            }, result.usage
        if _truthy(result.content.get("allow_add")):
            note = f"AI 风险评估通过（风险{_cn_risk_level(risk_level)}）"
            note = f"{note}：{reason}" if reason else note
            return {
                "close": False,
                "allow_add": True,
                "reason": reason,
                "analysis": ai_analysis,
                "note": note + guard_note,
            }, result.usage
        if _ai_risk_is_high(risk_level):
            return {
                "close": False,
                "allow_add": False,
                "reason": reason or "AI 判定当前风险偏高",
                "analysis": ai_analysis,
                "note": f"AI 判定风险{_cn_risk_level(risk_level)}，暂不加仓：{reason}" + guard_note,
            }, result.usage
        # Same leniency rule as the entry gate: a mild "no" must not veto an
        # add-on the deterministic rules already qualified.
        note = (
            f"AI 提示谨慎（{_cn_risk_level(risk_level)}）但未判定高风险，按策略规则加仓"
            + (f"：{reason}" if reason else "")
        )
        return {
            "close": False,
            "allow_add": True,
            "reason": reason,
            "analysis": ai_analysis,
            "note": note + guard_note,
        }, result.usage

    def _ai_allows_open(
        self,
        request: OpenEvaluateRequest,
        deployment: dict[str, Any],
        candidate: TradeDecision,
        atr: float,
    ) -> tuple[bool, str, Any]:
        """Risk gate for a new entry: AI may warn, never reshape the order."""
        result = self.ai_client.turtle_open_risk_decision(
            deployment=deployment,
            request_payload=request,
            signal={
                "direction": "buy" if candidate.action == "BUY" else "sell",
                "entry": candidate.entry,
                "protective_stop": candidate.sl,
                "stop_atr": STOP_ATR,
                "atr": atr,
                "lot": candidate.lot,
                "entry_analysis": candidate.metadata.get("entry_analysis"),
                "swing_direction": candidate.metadata.get("swing_direction"),
                "donchian_direction": candidate.metadata.get("donchian_direction"),
            },
        )
        if result is None:
            return True, "AI 未返回结果，按策略规则开仓", None
        reason = str(result.content.get("reason") or "").strip()
        # The full analysis is what the panel should show; the one-line reason is
        # only the fallback for callers that do not return one.
        ai_text = str(result.content.get("analysis") or "").strip() or reason
        risk_level = str(result.content.get("risk_level") or "").strip().lower()
        if _truthy(result.content.get("allow_open")):
            note = f"AI 风险评估通过（风险{_cn_risk_level(risk_level)}）"
            return True, f"{note}：{ai_text}" if ai_text else note, result.usage
        if _ai_risk_is_high(risk_level):
            return False, ai_text or "AI 判定当前风险偏高", result.usage
        # Lenient by design: a mild "no" from the model must not veto an entry
        # the deterministic rules already qualified.
        return (
            True,
            f"AI 提示谨慎（{_cn_risk_level(risk_level)}）但未判定高风险，按策略规则开仓"
            + (f"：{ai_text}" if ai_text else ""),
            result.usage,
        )


def _ordered(candles):
    return sorted(candles, key=lambda item: item.timestamp)


def _atr(candles, period: int) -> float:
    sample = candles[-period - 1:]
    ranges = []
    for index in range(1, len(sample)):
        current, previous = sample[index], sample[index - 1]
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return max(sum(ranges) / max(len(ranges), 1), 1e-9)


def _swing_pullback_signal(candles, atr: float, config: dict[str, Any]) -> tuple[str, str]:
    """Confirm a new small swing after a completed pullback.

    Only confirmed pivots are used (two bars on either side by default), so
    the signal never relies on the still-forming right edge of the chart.
    """
    pivot_span = _positive_int(config.get("swing_pivot_span"), SWING_PIVOT_SPAN)
    min_pullback = _positive_float(config.get("pullback_min_atr"), default=SWING_PULLBACK_MIN_ATR)
    confirmation_window = _positive_int(config.get("swing_confirmation_bars"), SWING_CONFIRMATION_BARS)
    points = _confirmed_pivots(candles, pivot_span)
    highs = [item for item in points if item[1] == "high"]
    lows = [item for item in points if item[1] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "", "有效波段高低点不足，暂不判断回调结构"
    last_high, previous_high = highs[-1], highs[-2]
    last_low, previous_low = lows[-1], lows[-2]
    close = float(candles[-1].close)
    previous_close = float(candles[-2].close)

    bullish_structure = last_high[2] > previous_high[2] and last_low[2] > previous_low[2]
    if bullish_structure and last_low[0] > last_high[0]:
        pullback_size = last_high[2] - last_low[2]
        crossed = close > last_high[2] and _crossed_recently(candles, last_high[2], "buy", confirmation_window)
        confirmation = _bullish_confirmation(candles, confirmation_window)
        if pullback_size >= min_pullback * atr and crossed and confirmation:
            return "buy", (
                f"低点与高点同步抬高（{previous_low[2]:g}→{last_low[2]:g}，{previous_high[2]:g}→{last_high[2]:g}）；"
                f"回调至{last_low[2]:g}后收盘上破{last_high[2]:g}；{confirmation}"
            )

    bearish_structure = last_high[2] < previous_high[2] and last_low[2] < previous_low[2]
    if bearish_structure and last_high[0] > last_low[0]:
        pullback_size = last_high[2] - last_low[2]
        crossed = close < last_low[2] and _crossed_recently(candles, last_low[2], "sell", confirmation_window)
        confirmation = _bearish_confirmation(candles, confirmation_window)
        if pullback_size >= min_pullback * atr and crossed and confirmation:
            return "sell", (
                f"高点与低点同步走低（{previous_high[2]:g}→{last_high[2]:g}，{previous_low[2]:g}→{last_low[2]:g}）；"
                f"反弹至{last_high[2]:g}后收盘下破{last_low[2]:g}；{confirmation}"
            )

    # Reversal setup: at the end of a down/up leg, do not wait for a complete
    # higher-high/higher-low structure before recognizing the first new swing.
    if last_low[2] < previous_low[2] and last_low[0] > previous_low[0]:
        event = _bullish_confirmation_event(candles, confirmation_window)
        if event is not None and event[0] > last_low[0]:
            event_index, confirmation = event
            trigger = float(candles[event_index].high)
            if close > trigger:
                return "buy", (
                    f"下跌末端转为企稳；下方低点{last_low[2]:g}；收盘上破确认 K 线高点{trigger:g}；"
                    f"{confirmation}"
                )

    if last_high[2] > previous_high[2] and last_high[0] > previous_high[0]:
        event = _bearish_confirmation_event(candles, confirmation_window)
        if event is not None and event[0] > last_high[0]:
            event_index, confirmation = event
            trigger = float(candles[event_index].low)
            if close < trigger:
                return "sell", (
                    f"上涨末端转为承压；上方高点{last_high[2]:g}；收盘下破确认 K 线低点{trigger:g}；"
                    f"{confirmation}"
                )
    return "", "回调结构与确认条件尚未同时满足"


def _confirmed_pivots(candles, span: int) -> list[tuple[int, str, float]]:
    points: list[tuple[int, str, float]] = []
    end = len(candles) - span
    for index in range(span, max(span, end)):
        current = candles[index]
        left = candles[index - span:index]
        right = candles[index + 1:index + span + 1]
        if len(right) < span:
            continue
        if all(current.high > item.high for item in (*left, *right)):
            points.append((index, "high", float(current.high)))
        if all(current.low < item.low for item in (*left, *right)):
            points.append((index, "low", float(current.low)))
    points.sort(key=lambda item: item[0])
    return points


def _crossed_recently(candles, level: float, direction: str, window: int) -> bool:
    start = max(1, len(candles) - max(1, window))
    if direction == "buy":
        return any(candles[index - 1].close <= level < candles[index].close for index in range(start, len(candles)))
    return any(candles[index - 1].close >= level > candles[index].close for index in range(start, len(candles)))


def _ema_cross(candles, fast: int, slow: int, direction: str, index: int | None = None) -> bool:
    if len(candles) < slow + 2:
        return False
    end = len(candles) - 1 if index is None else index
    if end < slow + 1:
        return False
    closes = [float(item.close) for item in candles]
    fast_values = _ema_series(closes, fast)
    slow_values = _ema_series(closes, slow)
    before = fast_values[end - 1] - slow_values[end - 1]
    after = fast_values[end] - slow_values[end]
    return before <= 0 < after if direction == "buy" else before >= 0 > after


def _ema_series(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result: list[float] = []
    current = values[0]
    for value in values:
        current = alpha * value + (1.0 - alpha) * current
        result.append(current)
    return result


def _bullish_confirmation(candles, window: int) -> str:
    event = _bullish_confirmation_event(candles, window)
    return event[1] if event is not None else ""


def _bullish_confirmation_event(candles, window: int) -> tuple[int, str] | None:
    start = max(1, len(candles) - max(1, window))
    for index in range(start, len(candles)):
        if _ema_cross(candles, SWING_EMA_FAST, SWING_EMA_SLOW, "buy", index):
            return index, "短均线上穿（金叉）确认"
        previous, current = candles[index - 1], candles[index]
        if previous.close < previous.open and current.close > current.open and current.open <= previous.close and current.close >= previous.open:
            return index, "看涨吞没形态确认"
        body = abs(current.close - current.open)
        lower_wick = min(current.open, current.close) - current.low
        if body > 0 and lower_wick >= body * PIN_BAR_WICK_RATIO and current.close > (current.high + current.low) / 2:
            return index, "看涨长下影（Pin Bar）确认"
    return None


def _bearish_confirmation(candles, window: int) -> str:
    event = _bearish_confirmation_event(candles, window)
    return event[1] if event is not None else ""


def _bearish_confirmation_event(candles, window: int) -> tuple[int, str] | None:
    start = max(1, len(candles) - max(1, window))
    for index in range(start, len(candles)):
        if _ema_cross(candles, SWING_EMA_FAST, SWING_EMA_SLOW, "sell", index):
            return index, "短均线下穿（死叉）确认"
        previous, current = candles[index - 1], candles[index]
        if previous.close > previous.open and current.close < current.open and current.open >= previous.close and current.close <= previous.open:
            return index, "看跌吞没形态确认"
        body = abs(current.close - current.open)
        upper_wick = current.high - max(current.open, current.close)
        if body > 0 and upper_wick >= body * PIN_BAR_WICK_RATIO and current.close < (current.high + current.low) / 2:
            return index, "看跌长上影（Pin Bar）确认"
    return None


def _unit_lot(
    request: OpenEvaluateRequest,
    config: dict[str, Any],
    *,
    entry: float,
    stop_loss: float,
) -> tuple[float, dict[str, Any]]:
    """Calculate one Turtle Unit using the shared deployment sizing settings.

    GL_TREND_V1 does not invent a second risk model.  It supplies its own
    deterministic stop (2 ATR), then uses the same fixed-volume/risk-volume
    settings as the rest of the platform.
    """
    info = request.symbol_info or {}
    min_lot = _positive_float(info, "volume_min", "lots_min", "min_lot", "minLot") or _positive_float(config.get("min_lot"), default=DEFAULT_MIN_LOT)
    max_lot = _positive_float(info, "volume_max", "lots_max", "max_lot", "maxLot") or _positive_float(config.get("max_lot"), default=0.0)
    step = _positive_float(info, "volume_step", "lots_step", "lot_step", "lotStep") or _positive_float(config.get("lot_step"), default=DEFAULT_LOT_STEP)
    mode = str(config.get("position_size_mode") or "fixed").lower()
    if mode != "risk":
        lot = _normalize_volume(float(config.get("fixed_volume") or config.get("lot") or 0.01), min_lot, max_lot, step)
        return lot, {"mode": "fixed", "unit_lot": lot}

    if config.get("risk_base_mode") == "balance_percent":
        risk_money = float(request.balance or request.equity or 0) * float(config.get("risk_percent") or 1) / 100.0
    else:
        risk_money = float(config.get("risk_amount") or 0)
    price_risk = abs(float(entry) - float(stop_loss))
    risk_per_lot = _risk_per_lot(price_risk, info, config)
    if risk_money <= 0 or risk_per_lot <= 0:
        return 0.0, {"mode": "risk", "risk_money": risk_money, "risk_per_lot": risk_per_lot}
    raw = risk_money / risk_per_lot
    if raw < min_lot:
        return 0.0, {"mode": "risk", "risk_money": risk_money, "risk_per_lot": risk_per_lot, "raw_lot": raw}
    lot = _normalize_volume(raw, min_lot, max_lot, step)
    return lot, {"mode": "risk", "risk_money": risk_money, "risk_per_lot": risk_per_lot, "unit_lot": lot}


def _risk_per_lot(price_risk: float, info: dict[str, Any], config: dict[str, Any]) -> float:
    if price_risk <= 0:
        return 0.0
    tick_size = _positive_float(info, "tick_size", "trade_tick_size", "tick", "point", "point_size")
    tick_value = _positive_float(info, "tick_value", "trade_tick_value", "tick_value_profit", "trade_tick_value_profit", "tickVal")
    value_per_price = _positive_float(info, "value_per_price", "money_per_price", "valuePerPrice")
    value_per_point = _positive_float(info, "value_per_point", "money_per_point", "valuePerPoint")
    point = _positive_float(info, "point", "point_size")
    contract = _positive_float(info, "contract_size", "trade_contract_size", "contractSize") or _positive_float(config.get("contract_size"), default=0.0)
    if tick_size and tick_value:
        return price_risk / tick_size * tick_value
    if value_per_price:
        return price_risk * value_per_price
    if point and value_per_point:
        return price_risk / point * value_per_point
    if contract:
        return price_risk * contract
    return 0.0


def _positive_float(value: Any = None, *keys: str, default: float = 0.0) -> float:
    if keys and isinstance(value, dict):
        for key in keys:
            try:
                number = float(value.get(key) or 0)
            except (TypeError, ValueError):
                number = 0.0
            if number > 0:
                return number
        return default
    try:
        number = float(value)
        return number if number > 0 else default
    except (TypeError, ValueError):
        return default


def _normalize_volume(value: float, minimum: float, maximum: float, step: float) -> float:
    if value <= 0 or step <= 0:
        return 0.0
    normalized = round(value / step) * step
    if normalized < minimum:
        normalized = minimum
    if maximum > 0:
        normalized = min(normalized, maximum)
    return round(normalized, 8)


def _max_units(config: dict[str, Any]) -> int:
    """Effective unit limit: the user's setting, never above MAX_UNITS."""
    try:
        requested = int(config.get("max_positions") or MAX_UNITS)
    except (TypeError, ValueError):
        requested = MAX_UNITS
    return max(1, min(requested, MAX_UNITS))


def _protection_batch_decision(request: PositionEvaluateRequest, config: dict[str, Any], atr: float) -> TradeDecision | None:
    actions: list[dict[str, Any]] = []
    for position in request.positions:
        # Evaluate both protection stages on every request.  When price has
        # already reached the trailing threshold, do not first apply the
        # weaker break-even target and wait for another tick/bar.
        candidates = [
            item for item in (
                _break_even_decision(request, position, config, atr),
                _trailing_stop_decision(request, position, config, atr),
            )
            if item is not None and item.sl is not None
        ]
        if position.side == "BUY":
            candidate = max(candidates, key=lambda item: float(item.sl)) if candidates else None
        else:
            candidate = min(candidates, key=lambda item: float(item.sl)) if candidates else None
        if candidate is not None and candidate.sl is not None:
            actions.append({
                "action": "modify",
                "ticket": str(position.ticket),
                "sl": candidate.sl,
                "tp": candidate.tp,
                "comment": candidate.reason,
            })
    if not actions:
        return None
    first = actions[0]
    return TradeDecision(
        decision_id=_id(), request_id=request.request_id, status="APPROVED",
        action="MODIFY_SL", symbol=request.symbol, confidence=1.0,
        reason=f"浮盈保护：同步调整{len(actions)}个持仓的止损",
        expires_at=_expires(), position_ticket=str(first["ticket"]), sl=first["sl"], tp=first["tp"],
        metadata={"strategy_code": "GL_TREND_V1", "batch_actions": actions},
    )


def _break_even_decision(request: PositionEvaluateRequest, position: Any, config: dict[str, Any], atr: float) -> TradeDecision | None:
    trigger = _positive_float(config.get("break_even_atr"), default=DEFAULT_BREAK_EVEN_ATR)
    configured_offset = float(config.get("break_even_offset") or DEFAULT_BREAK_EVEN_OFFSET)
    if trigger <= 0 or atr <= 0:
        return None
    # A stop exactly at the entry price still books a loss once spread and
    # slippage are paid, so the break-even target clears the current spread with
    # a small margin.  A larger configured offset still wins.
    spread = abs(float(request.ask) - float(request.bid))
    offset = max(configured_offset, spread * BREAK_EVEN_SPREAD_BUFFER)
    # Never park the stop beyond the level that triggered it.
    offset = min(offset, trigger * atr * 0.5)
    current_sl = position.sl if position.sl and position.sl > 0 else None
    if position.side == "BUY":
        if request.bid - position.open_price < trigger * atr:
            return None
        target = position.open_price + offset
        if current_sl is not None and current_sl >= target:
            return None
    else:
        if position.open_price - request.ask < trigger * atr:
            return None
        target = position.open_price - offset
        if current_sl is not None and current_sl <= target:
            return None
    return TradeDecision(
        decision_id=_id(), request_id=request.request_id, status="APPROVED",
        action="MODIFY_SL", symbol=request.symbol, confidence=1.0,
        reason=f"浮盈达{trigger:g}倍ATR，止损上移至保本+{offset:g}",
        expires_at=_expires(), position_ticket=position.ticket, sl=target, tp=position.tp,
        metadata={
            "strategy_code": "GL_TREND_V1",
            "break_even_atr": trigger,
            "break_even_offset": offset,
            "break_even_spread": spread,
        },
    )


def _trailing_stop_decision(request: PositionEvaluateRequest, position: Any, config: dict[str, Any], atr: float) -> TradeDecision | None:
    start = _positive_float(config.get("trailing_start_atr"), default=DEFAULT_TRAILING_START_ATR)
    distance = _positive_float(config.get("trailing_distance_atr"), default=DEFAULT_TRAILING_DISTANCE_ATR)
    if start <= 0 or distance <= 0 or atr <= 0:
        return None
    current_sl = position.sl if position.sl and position.sl > 0 else None
    if position.side == "BUY":
        favorable = request.bid - position.open_price
        if favorable < start * atr:
            return None
        target = request.bid - distance * atr
        if target <= position.open_price or (current_sl is not None and target <= current_sl):
            return None
    else:
        favorable = position.open_price - request.ask
        if favorable < start * atr:
            return None
        target = request.ask + distance * atr
        if target >= position.open_price or (current_sl is not None and target >= current_sl):
            return None
    return TradeDecision(
        decision_id=_id(), request_id=request.request_id, status="APPROVED",
        action="MODIFY_SL", symbol=request.symbol, confidence=1.0,
        reason=f"浮盈达{start:g}倍ATR，止损跟进至现价回撤{distance:g}倍ATR处",
        expires_at=_expires(), position_ticket=position.ticket, sl=target, tp=position.tp,
        metadata={"strategy_code": "GL_TREND_V1", "trailing_start_atr": start, "trailing_distance_atr": distance},
    )


def _maybe_add(request: PositionEvaluateRequest, config: dict[str, Any], atr: float) -> TradeDecision | None:
    if not bool(config.get("allow_add", False)) or atr <= 0 or len(request.positions) >= _max_units(config):
        return None
    sides = {item.side for item in request.positions}
    if len(sides) != 1:
        return None
    side = next(iter(sides))
    direction = "buy" if side == "BUY" else "sell"
    step = float(config.get("add_step_atr") or DEFAULT_ADD_STEP_ATR)
    if side == "BUY":
        anchor = max(item.open_price for item in request.positions)
        if request.bid < anchor + step * atr:
            return None
        entry, action, stop_loss = request.ask, "BUY", request.ask - STOP_ATR * atr
    else:
        anchor = min(item.open_price for item in request.positions)
        if request.ask > anchor - step * atr:
            return None
        entry, action, stop_loss = request.bid, "SELL", request.bid + STOP_ATR * atr
    lot, sizing = _unit_lot(request, config, entry=entry, stop_loss=stop_loss)
    if lot <= 0:
        return None
    reason = f"趋势延续：价格较最远入场再延伸{step:g}倍ATR，加仓第{len(request.positions) + 1}笔"
    # The added unit carries the basket's new unified stop, and every existing
    # unit is moved onto that same level in the same response.  A unit that is
    # already protected more tightly is left alone.
    batch_actions: list[dict[str, Any]] = [{
        "action": "add",
        "direction": direction,
        "volume": lot,
        "price": entry,
        "sl": stop_loss,
        "tp": None,
        "comment": reason,
    }]
    for item in request.positions:
        current_sl = item.sl if item.sl and item.sl > 0 else None
        if current_sl is not None:
            if direction == "buy" and stop_loss <= current_sl:
                continue
            if direction == "sell" and stop_loss >= current_sl:
                continue
        batch_actions.append({
            "action": "modify",
            "ticket": str(item.ticket),
            "sl": stop_loss,
            "tp": item.tp,
            "comment": "加仓后统一保护止损",
        })
    return TradeDecision(
        decision_id=_id(), request_id=request.request_id, status="APPROVED",
        action=action, symbol=request.symbol, confidence=1.0,
        reason=f"{reason}，全部持仓止损统一上移至{stop_loss:g}",
        expires_at=_expires(), lot=lot, entry=entry, sl=stop_loss,
        metadata={"strategy_code": "GL_TREND_V1", "position_sizing": sizing,
                  "unit_index": len(request.positions) + 1, "max_units": _max_units(config),
                  "unified_stop": stop_loss,
                  "batch_actions": batch_actions,
                  },
    )


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    """Parse a boolean the model may return as text, without treating "false" as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "approve", "approved"}


def _ai_risk_is_high(risk_level: str) -> bool:
    """Only an explicit high-risk verdict may veto a server-approved order."""
    text = str(risk_level or "").strip().lower()
    return "high" in text or "高" in text


def _cn_direction(direction: str) -> str:
    """Business wording for a direction, never the internal field value."""
    return "做多" if str(direction or "").strip().lower() in {"buy", "bullish", "long"} else "做空"


def _cn_risk_level(risk_level: str) -> str:
    """Business wording for the AI risk level shown to the user."""
    text = str(risk_level or "").strip().lower()
    if "high" in text or "高" in text:
        return "偏高"
    if "medium" in text or "中" in text:
        return "中等"
    if "low" in text or "低" in text:
        return "正常"
    return "未分级"


def _bars_since_open(candles: list[Candle], position: Any) -> int:
    """Closed bars printed since the position was opened, or -1 when unknown."""
    open_time = position.open_time
    if not open_time:
        return -1
    return sum(1 for item in candles if item.timestamp > open_time)


def _too_fresh_to_close(candles: list[Candle], request: PositionEvaluateRequest) -> bool:
    """Never let a proactive exit fire before one closed bar has printed."""
    return any(
        0 <= _bars_since_open(candles, item) < PROACTIVE_EXIT_MIN_BARS
        for item in request.positions
    )


def _favorable_atr(position: Any, atr: float) -> float:
    """Signed distance from entry to current price in ATR; positive = in favour.

    Gives the AI a scale-free "how far has this run" figure for judging whether
    enough profit has already been earned to take it.
    """
    if atr <= 0:
        return 0.0
    move = float(position.current_price) - float(position.open_price)
    if position.side == "SELL":
        move = -move
    return round(move / atr, 3)


def _with_ai_analysis(reason: str, review: dict[str, Any]) -> str:
    """Append the AI's own analysis so the EA panel shows it, not just metadata.

    The model's full ``analysis`` is preferred over its one-line ``reason``: the
    analysis is what explains the verdict, and it is already paid for on every
    call. Older callers that only supply ``reason`` keep working unchanged.
    """
    analysis = str(review.get("analysis") or "").strip()
    if not analysis:
        analysis = str(review.get("reason") or "").strip()
    if not analysis or analysis in reason:
        return reason
    return f"{reason}；AI 分析：{analysis}"


def _id() -> str:
    return f"dec_{uuid4().hex}"


def _expires() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=60)


def _hold_open(request, reason):
    return TradeDecision(decision_id=_id(), request_id=request.request_id, status="HOLD", action="HOLD", symbol=request.symbol, confidence=0.0, reason=reason, expires_at=_expires())


def _hold_position(request, ticket, reason):
    return TradeDecision(decision_id=_id(), request_id=request.request_id, status="HOLD", action="HOLD", symbol=request.symbol, position_ticket=ticket, confidence=0.0, reason=reason, expires_at=_expires())


def _basket_stop_levels(positions: list[Any], atr: float) -> dict[str, float]:
    """One protective stop per side, anchored at that side's furthest entry.

    The basket is managed as a single position: the stop sits `STOP_ATR` from the
    most recent (furthest) unit's entry, and every unit shares that level.
    """
    levels: dict[str, float] = {}
    buys = [item for item in positions if item.side == "BUY"]
    sells = [item for item in positions if item.side == "SELL"]
    if buys:
        levels["BUY"] = max(item.open_price for item in buys) - STOP_ATR * atr
    if sells:
        levels["SELL"] = min(item.open_price for item in sells) + STOP_ATR * atr
    return levels


def _close_basket(request: PositionEvaluateRequest, members: list[Any], reason: str) -> TradeDecision:
    """Close every unit of one side in a single response."""
    actions = [
        {
            "action": "close",
            "ticket": str(item.ticket),
            "volume": float(item.volume),
            "comment": reason,
        }
        for item in members
    ]
    return TradeDecision(
        decision_id=_id(), request_id=request.request_id, status="APPROVED",
        action="CLOSE", symbol=request.symbol, confidence=1.0, reason=reason,
        expires_at=_expires(), position_ticket=str(members[0].ticket),
        metadata={"strategy_code": "GL_TREND_V1", "batch_actions": actions},
    )

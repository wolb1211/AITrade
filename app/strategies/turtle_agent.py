from __future__ import annotations

from datetime import datetime, time as clock_time, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import Candle, OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision
from app.services.ai_service import AiDecisionClient
from app.strategies import time_windows
from app.strategies.stop_rules import min_stop_distance, respect_min_stop, stop_is_placeable

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
# How many distinct confirmation conditions have to appear inside that window, and
# one of them on the newest bar. The timing requirement is the hard one: the newest
# bar must carry a condition, or the trigger fires several bars after the move
# started, which is what the operator saw on the chart. The count is left at 1 on
# purpose - how many conditions fired is the signal's strength, and that is handed
# to the AI to weigh rather than used as a local cut-off. Setting it to 2 makes a
# deployment require two conditions before the AI is even asked.
SWING_CONFIRM_MIN_SIGNALS = 1
SWING_EMA_FAST = 5              # confirmation EMA cross, fast period
SWING_EMA_SLOW = 10             # confirmation EMA cross, slow period
PIN_BAR_WICK_RATIO = 2.0        # pin-bar wick must be this many times the body
# How far an engulfing body edge may miss the previous body by. A bar still moving
# at its close makes the next bar open a tick or three above that close, which is
# the common case in a live market; demanding an exact cover filtered real
# engulfings out. The allowance is the larger of a tick count and a small share of
# the ATR, so it also stretches on a symbol whose ticks are coarse. 0 on either
# switches that part off; both 0 restores the exact rule.
DEFAULT_ENGULF_TOLERANCE_POINTS = 5.0
DEFAULT_ENGULF_TOLERANCE_ATR = 0.03

# Protection ladder: break-even first, then a trailing stop.
DEFAULT_BREAK_EVEN_ATR = 0.5         # favourable move that triggers break-even
DEFAULT_BREAK_EVEN_OFFSET = 0.0      # extra offset from entry, on top of the spread buffer
# Favourable move of the WHOLE basket that moves every unit to the volume-weighted
# average entry. Per-unit protection leaves the newest unit naked until it earns
# its own ATR, which is where a pullback turns a winning basket into a losing one.
DEFAULT_BASKET_BREAK_EVEN_ATR = 1.0
BREAK_EVEN_SPREAD_BUFFER = 1.5       # break-even must clear this many times the spread
DEFAULT_TRAILING_START_ATR = 1.0     # favourable move that starts trailing
DEFAULT_TRAILING_DISTANCE_ATR = 0.5  # trailing distance behind the current price
# How much better a trailing stop has to become before the modification is sent.
# Without it any new high, even by a tick, triggers another round trip to the
# broker - and one more chance of a stop the broker refuses. 0 restores the
# continuous behaviour.
DEFAULT_TRAILING_MIN_STEP_ATR = 0.2

# Adding units.
MAX_UNITS = 4                        # this strategy never pyramids beyond this
# Extension beyond the furthest entry that allows another unit. Half the stop
# distance rather than a quarter: at 0.5 ATR a four-unit basket added its units
# while the basket stop rose only 0.5 ATR each time, so a full basket could be
# stopped out 4.5 ATR below its first entry - more than twice one unit's risk. At
# 1.0 the stop ends up above the first entry and a full basket costs about one
# unit's risk.
DEFAULT_ADD_STEP_ATR = 1.0           # extension beyond the furthest entry allowing an add

# Share of a basket's best profit it keeps before the rest is taken off. A live
# four-unit basket peaked well in profit and gave all of it back, because nothing
# looked at the give-back at all. 0 switches the rule off.
DEFAULT_GIVE_BACK_RATIO = 0.5
# How far the request's market quote may sit from the positions' own price before
# the server refuses to act on it. A client sent a quote forty points from the
# live price and everything computed from it was worthless.
DEFAULT_MAX_QUOTE_DIVERGENCE_ATR = 1.0
# The peak has to be worth protecting before the rule may fire, so ordinary early
# noise is left alone.
GIVE_BACK_MIN_ATR = 1.5

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
        # Every no-new-entry window is checked here; positions already open keep
        # their protective levels either way.
        closed = time_windows.closed_window_reason(config, time_windows.now_utc())
        if closed:
            return _hold_open(request, closed)
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
        swing_direction, swing_analysis = _swing_pullback_signal(
            candles, atr, config, _engulf_tolerance(request.symbol_info, config, atr)
        )
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
            # The wording has to follow the direction: a sell breaks the LOW, and
            # worded as "上破…区间高点" it reads as a calculation error. Production
            # showed sells reported with a close below the high they "broke".
            if direction == "buy":
                entry_analysis = (
                    f"突破趋势确认：收盘价{close:g}上破前{period}根 K 线区间高点{upper:g}，顺势入场"
                )
            else:
                entry_analysis = (
                    f"突破趋势确认：收盘价{close:g}下破前{period}根 K 线区间低点{lower:g}，顺势入场"
                )
        else:
            return _hold_open(request, "当前无合适入场点：等突破关键位、或回调到位后再进场")
        entry = request.ask if direction == "buy" else request.bid
        # The US data/open window and a spike bar both mean the move has just
        # happened; those entries wait for a pullback at the level that was
        # broken instead of buying the top of it.
        waits, anchor, wait_reason = _pending_entry_conditions(
            candles=candles, direction=direction, upper=upper, lower=lower,
            donchian_fired=bool(donchian_direction), atr=atr, config=config,
        )
        order_type = "market"
        wait_note = ""
        if waits and anchor > 0:
            # A limit only works when it sits behind the market, which is the case
            # these conditions describe; otherwise the market entry stands.
            behind_market = anchor < request.bid if direction == "buy" else anchor > request.ask
            if behind_market:
                order_type = "limit"
                entry = anchor
                wait_note = f"（{wait_reason}，改为限价挂单等待回调）"
        sl = entry - STOP_ATR * atr if direction == "buy" else entry + STOP_ATR * atr
        # A stop the broker will refuse is worse than a wider usable one, so the
        # entry stop respects the minimum distance too.
        sl, clamped = respect_min_stop(
            sl, side="BUY" if direction == "buy" else "SELL",
            bid=request.bid, ask=request.ask,
            info=request.symbol_info, config=config, atr=atr,
        )
        if not stop_is_placeable(
            sl, side="BUY" if direction == "buy" else "SELL",
            bid=request.bid, ask=request.ask,
            info=request.symbol_info, config=config, atr=atr,
        ):
            # The broker would answer "Invalid S/L or T/P", so there is no entry
            # to place on this bar; say so instead of sending it anyway.
            return _hold_open(request, "止损位不在市价正确一侧，本根不入场（等待报价更新）")
        lot, sizing = _unit_lot(request, config, entry=entry, stop_loss=sl)
        if lot <= 0:
            return _hold_open(request, "手数无法确定：当前止损距离与品种合约参数不匹配，本根不入场")
        decision = TradeDecision(
            decision_id=_id(), request_id=request.request_id, status="APPROVED",
            action="BUY" if direction == "buy" else "SELL", symbol=request.symbol,
            confidence=1.0, reason=entry_analysis + wait_note,
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
                "order_type": order_type,
                # Non-zero when the broker's minimum stop distance moved this
                # entry's stop, so a wider stop than the ATR rule implies is
                # explainable from the decision alone.
                "min_stop_distance_enforced": round(clamped, 5),
                # What the server resolved from the client's symbol info and the
                # config, so a client that reports a stops level can be told
                # apart from one that does not without reading its payload.
                "min_stop_distance_used": round(
                    min_stop_distance(request.symbol_info, config, atr=atr), 5
                ),
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
        # Stale market data has to be refused before anything is decided from it:
        # one client sent a quote forty points from the live price, so the basket
        # stop it produced sat above the market and every order was refused.
        stale = _quote_divergence(request, config, atr)
        if stale is not None:
            return _hold_position(request, first_ticket, stale)
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
        # A basket that has handed back most of its best profit is taken off
        # before anything else is considered: it must not add a unit into a
        # pullback, and the protective levels below only ever look at how much is
        # made, never at how much of it has been given back.
        give_back = _give_back_decision(request, request.positions, config, atr, candles)
        if give_back is not None:
            return give_back
        add_decision, add_block_reason = _maybe_add(request, config, atr)
        if self.ai_client is None:
            # Adding is evaluated first. A bar that prints a new high also makes
            # the trailing target tighter, so returning the protection update
            # first skipped the add on exactly the advancing bars a pyramid
            # exists for. The add moves the whole basket onto its own new stop,
            # so nothing is left unprotected by taking this branch.
            if add_decision is not None:
                return add_decision
            protection = _protection_batch_decision(request, config, atr)
            if protection is not None:
                return protection
            return _hold_position(
                request, first_ticket, add_block_reason or "趋势结构未被破坏，继续持有",
            )
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
        # Adding is evaluated before the protection ladder. A bar that prints a
        # new high makes the trailing target tighter as well, so returning the
        # protection update first skipped the add on exactly the advancing bars a
        # pyramid exists for, and the strategy could rarely reach its unit cap.
        # The add carries the whole basket onto its new stop, so no protection is
        # lost here; a protection update that was due on the same bar is applied
        # on the next one.
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
        protection = _protection_batch_decision(request, config, atr)
        if protection is not None:
            protection.reason = _with_ai_analysis(protection.reason, review)
            protection.usage = review_usage
            return protection
        hold = _hold_position(
            request,
            first_ticket,
            _with_ai_analysis(add_block_reason or "趋势结构未被破坏，继续持有", review),
        )
        hold.metadata["ai_risk"] = review["note"]
        if add_block_reason:
            # Keep the sizing failure on the decision as well, so it is queryable
            # rather than only visible in the panel text.
            hold.metadata["add_blocked_reason"] = add_block_reason
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
        # The generic prompt shape asks for an "action" while this review reads
        # close_now and allow_add, so the same field is accepted under both
        # spellings; otherwise the review silently read defaults on every call.
        action = str(result.content.get("action") or "").strip().lower()
        close_requested = _truthy(result.content.get("close_now")) or action == "close"
        add_allowed = _truthy(result.content.get("allow_add")) or action == "add"
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
                "note": f"止盈离场{_risk_level_note(risk_level)}：{reason}",
            }, result.usage
        if add_allowed:
            note = f"AI 风险评估通过{_risk_level_note(risk_level)}"
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
                "note": f"AI 判定暂不加仓{_risk_level_note(risk_level)}：{reason}" + guard_note,
            }, result.usage
        # Same leniency rule as the entry gate: a mild "no" must not veto an
        # add-on the deterministic rules already qualified.
        note = (
            f"AI 提示谨慎{_risk_level_note(risk_level)}但未判定高风险，按策略规则加仓"
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
        signal = {
            "direction": "buy" if candidate.action == "BUY" else "sell",
            "entry": candidate.entry,
            "protective_stop": candidate.sl,
            "stop_atr": STOP_ATR,
            "atr": atr,
            "lot": candidate.lot,
            "entry_analysis": candidate.metadata.get("entry_analysis"),
            "swing_direction": candidate.metadata.get("swing_direction"),
            "donchian_direction": candidate.metadata.get("donchian_direction"),
        }
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        # What a provider outage means for the entry. The gate is the risk check,
        # so the default leaves the entry unapproved; a deployment that would
        # rather keep trading on its own rules can set ai_gate_fail_open.
        fail_open = config.get("ai_gate_fail_open") in (True, 1, "1", "true", "on", "yes")
        try:
            result = self.ai_client.turtle_open_risk_decision(
                deployment=deployment, request_payload=request, signal=signal,
            )
        except Exception as exc:  # noqa: BLE001 - the gate must survive a provider outage
            if fail_open:
                return True, f"AI 服务不可用（{type(exc).__name__}），按配置放行本次开仓", None
            return False, f"AI 服务不可用（{type(exc).__name__}），本次保守不开仓", None
        if result is None:
            if fail_open:
                return True, "AI 未返回结果，按配置放行本次开仓", None
            return False, "AI 未返回结果，本次保守不开仓", None
        outcome = _open_risk_outcome(result)
        if outcome is not None:
            return outcome
        # The answer ignored the output contract: neither allow_open nor a usable
        # risk_level came back. That is not a verdict the gate can read - reading
        # it as "not high" is what let a warned-about entry through, while
        # reading it as a refusal blocks setups the same analysis calls sound.
        # Ask once more with the contract spelled out, then give up.
        retry = self.ai_client.turtle_open_risk_decision(
            deployment=deployment,
            request_payload=request,
            signal=signal,
            correction=_OPEN_RISK_FORMAT_REMINDER,
        )
        retried = _open_risk_outcome(retry) if retry is not None else None
        if retried is not None:
            return retried
        risk_level = str(result.content.get("risk_level") or "").strip().lower()
        reason = str(result.content.get("reason") or "").strip()
        ai_text = str(result.content.get("analysis") or "").strip() or reason
        return (
            False,
            f"AI 风险等级无法识别（{risk_level or '缺失'}），为确保安全本次不开仓"
            + (f"：{ai_text}" if ai_text else ""),
            result.usage,
        )


# A closed bar this many times wider than the ATR is the move itself - a release
# spike or an opening drive - and entering at market there enters at its end.
DEFAULT_SPIKE_BAR_ATR = 2.0


def _pending_entry_conditions(
    *,
    candles: list[Any],
    direction: str,
    upper: float,
    lower: float,
    donchian_fired: bool,
    atr: float,
    config: dict[str, Any],
) -> tuple[bool, float, str]:
    """Whether this entry must wait for a pullback, and at what price.

    Only the two "the move just finished" cases are checked for this strategy:
    the US data/open window and a spike bar. A generic chase limit is not applied
    here on purpose - a trend entry is expected to be some way beyond the level
    it broke, and refusing those would remove the breakout entries the strategy
    exists for.

    The anchor is the level that was broken, which is where a retest would come
    back to. When the bar that just closed is itself the spike, its middle wins:
    that is the level the newest move would retrace to, and it sits nearer the
    market, so the order is more likely to be filled than one parked at the
    channel.
    """
    if atr <= 0:
        return False, 0.0, ""

    anchor = 0.0
    if donchian_fired and upper > 0 and lower > 0:
        anchor = float(upper) if direction == "buy" else float(lower)

    boundaries = _us_window_boundaries(config)
    in_window = boundaries is not None and time_windows.in_us_entry_window(
        time_windows.now_utc(), start=boundaries[0], end=boundaries[1]
    )

    spike = False
    spike_mid = 0.0
    if candles:
        bar = candles[-1]
        bar_range = max(float(bar.high) - float(bar.low), 0.0)
        spike_limit = _positive_float(config.get("spike_bar_atr"), default=DEFAULT_SPIKE_BAR_ATR)
        if spike_limit > 0 and bar_range > spike_limit * atr:
            spike = True
            spike_mid = (float(bar.high) + float(bar.low)) / 2.0
            anchor = spike_mid

    if not in_window and not spike:
        return False, 0.0, ""
    if anchor <= 0:
        return False, 0.0, ""
    reason = "上一根K线振幅偏大（尖峰），不追单" if spike else "美盘数据/开盘窗口内不追单"
    return True, anchor, reason


def _us_window_boundaries(config: dict[str, Any]) -> tuple[Any, Any] | None:
    """The cautious window in US Eastern time, or None when it is switched off."""
    if config.get("us_window_guard") in (False, 0, "0", "false", "off", "no"):
        return None
    start = _clock_time(config.get("us_window_start"), time_windows.DEFAULT_WINDOW_START)
    end = _clock_time(config.get("us_window_end"), time_windows.DEFAULT_WINDOW_END)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _clock_time(value: Any, fallback: Any) -> Any:
    """Parse an "HH:MM" deployment setting, falling back when it is unusable."""
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        hour, _, minute = text.partition(":")
        return clock_time(int(hour), int(minute or 0))
    except (TypeError, ValueError):
        return fallback


def _ordered(candles):
    return sorted(candles, key=lambda item: item.timestamp)


def _atr(candles, period: int) -> float:
    sample = candles[-period - 1:]
    ranges = []
    for index in range(1, len(sample)):
        current, previous = sample[index], sample[index - 1]
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return max(sum(ranges) / max(len(ranges), 1), 1e-9)


def _engulf_tolerance(symbol_info: Any, config: dict[str, Any], atr: float = 0.0) -> float:
    """How far an engulfing body edge may miss the previous body, in price."""
    info = symbol_info if isinstance(symbol_info, dict) else {}
    try:
        point = float(info.get("point") or 0)
    except (TypeError, ValueError):
        point = 0.0
    points = _config_number(config, "engulf_tolerance_points", DEFAULT_ENGULF_TOLERANCE_POINTS)
    share = _config_number(config, "engulf_tolerance_atr", DEFAULT_ENGULF_TOLERANCE_ATR)
    from_ticks = point * points if point > 0 and points > 0 else 0.0
    from_atr = atr * share if atr > 0 and share > 0 else 0.0
    return max(from_ticks, from_atr)


def _swing_pullback_signal(
    candles, atr: float, config: dict[str, Any], engulf_tolerance: float = 0.0
) -> tuple[str, str]:
    """Confirm a new small swing after a completed pullback.

    Only confirmed pivots are used (two bars on either side by default), so
    the signal never relies on the still-forming right edge of the chart.
    """
    pivot_span = _positive_int(config.get("swing_pivot_span"), SWING_PIVOT_SPAN)
    min_pullback = _positive_float(config.get("pullback_min_atr"), default=SWING_PULLBACK_MIN_ATR)
    confirmation_window = _positive_int(config.get("swing_confirmation_bars"), SWING_CONFIRMATION_BARS)
    confirm_min_signals = _positive_int(
        config.get("swing_confirm_min_signals"), SWING_CONFIRM_MIN_SIGNALS
    )
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
        confirmation = _bullish_confirmation(
            candles, confirmation_window, confirm_min_signals, engulf_tolerance
        )
        if pullback_size >= min_pullback * atr and crossed and confirmation:
            return "buy", (
                f"低点与高点同步抬高（{previous_low[2]:g}→{last_low[2]:g}，{previous_high[2]:g}→{last_high[2]:g}）；"
                f"回调至{last_low[2]:g}后收盘上破{last_high[2]:g}；{confirmation}"
            )

    bearish_structure = last_high[2] < previous_high[2] and last_low[2] < previous_low[2]
    if bearish_structure and last_high[0] > last_low[0]:
        pullback_size = last_high[2] - last_low[2]
        crossed = close < last_low[2] and _crossed_recently(candles, last_low[2], "sell", confirmation_window)
        confirmation = _bearish_confirmation(
            candles, confirmation_window, confirm_min_signals, engulf_tolerance
        )
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


def _confirmation_signals(
    candles, window: int, direction: str, engulf_tolerance: float = 0.0
) -> list[tuple[int, str]]:
    """Every confirmation hit in the window, oldest first, as (bar index, kind).

    Each condition is reported separately so the caller can require more than one
    of them, and can require the newest bar to carry one - which is what makes the
    entry punctual instead of firing several bars after the move started.

    ``engulf_tolerance`` forgives a body edge that misses the previous body by a
    tick. A bar that is still moving at its close makes the next bar open a tick
    above that close, which is the common case in a live market, and demanding an
    exact cover threw those engulfings away.
    """
    start = max(1, len(candles) - max(1, window))
    hits: list[tuple[int, str]] = []
    bullish = direction == "buy"
    for index in range(start, len(candles)):
        if _ema_cross(candles, SWING_EMA_FAST, SWING_EMA_SLOW, direction, index):
            hits.append((index, "短均线上穿（金叉）" if bullish else "短均线下穿（死叉）"))
            continue
        previous, current = candles[index - 1], candles[index]
        if bullish and previous.close < previous.open and current.close > current.open \
                and current.open <= previous.close + engulf_tolerance \
                and current.close >= previous.open - engulf_tolerance:
            hits.append((index, "看涨吞没"))
            continue
        if not bullish and previous.close > previous.open and current.close < current.open \
                and current.open >= previous.close - engulf_tolerance \
                and current.close <= previous.open + engulf_tolerance:
            hits.append((index, "看跌吞没"))
            continue
        body = abs(current.close - current.open)
        if body > 0 and bullish:
            wick = min(current.open, current.close) - current.low
            if wick >= body * PIN_BAR_WICK_RATIO and current.close > (current.high + current.low) / 2:
                hits.append((index, "看涨长下影（Pin Bar）"))
        elif body > 0:
            wick = current.high - max(current.open, current.close)
            if wick >= body * PIN_BAR_WICK_RATIO and current.close < (current.high + current.low) / 2:
                hits.append((index, "看跌长上影（Pin Bar）"))
    return hits


def _confirmation_text(
    candles, window: int, direction: str, min_signals: int, engulf_tolerance: float = 0.0
) -> str:
    """The confirmation wording, or an empty string when the bar does not qualify.

    Two requirements, both from the source design: at least ``min_signals``
    distinct conditions inside the window, and one of them on the newest bar. The
    second is what stops a condition from five bars ago still counting as a fresh
    entry - the trigger has to be happening now.
    """
    hits = _confirmation_signals(candles, window, direction, engulf_tolerance)
    if not hits:
        return ""
    kinds = list(dict.fromkeys(text for _, text in hits))
    if len(kinds) < max(1, min_signals):
        return ""
    newest = len(candles) - 1
    if not any(index == newest for index, _ in hits):
        return ""
    return "、".join(kinds)


def _bullish_confirmation(candles, window: int, min_signals: int = 2, engulf_tolerance: float = 0.0) -> str:
    return _confirmation_text(candles, window, "buy", min_signals, engulf_tolerance)


def _bearish_confirmation(candles, window: int, min_signals: int = 2, engulf_tolerance: float = 0.0) -> str:
    return _confirmation_text(candles, window, "sell", min_signals, engulf_tolerance)


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


# How far the two account-currency per-lot risk figures may disagree before the
# sizing records it. They describe the same quantity, but a broker quoting in
# another currency or an EA deriving one value from the contract size makes them
# diverge by the conversion factor, so this flags the payload for inspection
# rather than blocking it.
NOTABLE_SYMBOL_INFO_SPREAD = 5.0


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
    risk_per_lot, risk_source, risk_spread = _risk_per_lot(price_risk, info)
    # Record how the figure was derived: a wrong per-lot risk once sized a 46-lot
    # order and finding out why took a second production round trip, so the
    # branch, its inputs and the disagreement between branches travel with the
    # decision.
    sizing: dict[str, Any] = {
        "mode": "risk",
        "risk_money": risk_money,
        "risk_per_lot": risk_per_lot,
        "risk_source": risk_source,
        "price_risk": price_risk,
    }
    if risk_spread > NOTABLE_SYMBOL_INFO_SPREAD:
        sizing["risk_spread"] = round(risk_spread, 4)
    if risk_money <= 0 or risk_per_lot <= 0:
        return 0.0, sizing
    raw = risk_money / risk_per_lot
    if raw < min_lot:
        sizing["raw_lot"] = raw
        return 0.0, sizing
    lot = _normalize_volume(raw, min_lot, max_lot, step)
    sizing["unit_lot"] = lot
    return lot, sizing


def _risk_per_lot(price_risk: float, info: dict[str, Any]) -> tuple[float, str, float]:
    """Money risked per 1.0 lot when the price moves ``price_risk``.

    Returns the figure, the branch that produced it, and how far the two
    account-currency figures disagree (1.0 when only one is available). A wide
    spread is recorded rather than refused: a broker quoting in another currency,
    or an EA deriving one value from the contract size, diverges legitimately,
    and refusing would stop a healthy client from trading.

    Several symbol-info fields describe the same quantity and a partial payload
    used to fall through to whichever branch matched first: a deployment whose
    sizing came from a mis-set contract size reported a per-lot risk of about 2
    where 1438 was right, and its add-ons were sized at 46, 5538 and 40 million
    lots instead of roughly 0.07.

    The branches keep their priority, but a figure is only accepted when it can
    be right: a lot is never smaller than one contract unit, so the money risked
    per lot can never be below the price move itself. Anything smaller means the
    payload described the symbol in units that branch does not expect, and sizing
    from it would scale the intended risk by the same factor. When no branch
    yields a usable figure the order is not sized at all, so bad input can only
    ever skip a trade.

    Only symbol-info fields are consulted. The deployment config used to be a
    fallback for the contract size, but a contract size is a quantity of the
    underlying rather than an amount of money: it only behaves like one when the
    quote currency matches the account currency, and a mis-set value silently
    multiplied the position size.
    """
    if price_risk <= 0:
        return 0.0, "invalid_price_risk"
    tick_size = _positive_float(info, "tick_size", "trade_tick_size", "tick", "point", "point_size")
    tick_value = _positive_float(info, "tick_value", "trade_tick_value", "tick_value_profit", "trade_tick_value_profit", "tickVal")
    value_per_price = _positive_float(info, "value_per_price", "money_per_price", "valuePerPrice")
    value_per_point = _positive_float(info, "value_per_point", "money_per_point", "valuePerPoint")
    point = _positive_float(info, "point", "point_size")
    contract = _positive_float(info, "contract_size", "trade_contract_size", "contractSize")

    tick_figure = price_risk / tick_size * tick_value if (tick_size and tick_value) else 0.0
    price_figure = price_risk * value_per_price if value_per_price else 0.0
    point_figure = price_risk / point * value_per_point if (point and value_per_point) else 0.0

    # tick_value / tick_size and value_per_price describe the same quantity when
    # the client reports them consistently, but they may legitimately diverge: a
    # broker quoting in another currency, or an EA deriving one of them from the
    # contract size rather than the tick value, differs by the conversion factor.
    # A disagreement is therefore recorded rather than treated as an error, and
    # the largest usable figure wins - a bigger per-lot risk means a smaller
    # position, so a branch that is wrong can only under-size the order, never
    # scale it up. The spread is reported so a genuinely mixed-up payload stays
    # visible instead of silently trading at the wrong size.
    spread = 1.0
    if tick_figure and price_figure:
        spread = max(tick_figure, price_figure) / min(tick_figure, price_figure)

    usable = [
        (value, source)
        for value, source in (
            (tick_figure, "tick_size"),
            (price_figure, "value_per_price"),
            (point_figure, "point"),
        )
        if value >= price_risk
    ]
    if usable:
        value, source = max(usable, key=lambda item: item[0])
        return value, source, spread
    # Only the raw unit count is left. It describes the underlying rather than an
    # amount of money, so it is a last resort and still has to clear the floor.
    if contract >= 1:
        return price_risk * contract, "contract_size", spread
    return 0.0, "none", spread


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


def _basket_break_even_level(
    request: PositionEvaluateRequest,
    config: dict[str, Any],
    atr: float,
) -> tuple[float, float] | None:
    """The whole basket's break-even stop and how far ahead it is, or None.

    Per-unit protection leaves the newest unit unprotected until it earns its own
    ATR: a two-unit basket at +83 and +22 points had the first protected and the
    second a full stop away from its own, so a pullback stopped the second at a
    loss. Once the basket as a whole is ahead by ``basket_breakeven_atr`` (1.0 by
    default, 0 to switch it off) every unit moves to the volume-weighted average
    entry, so the same pullback exits flat instead.
    """
    trigger = _config_number(config, "basket_breakeven_atr", DEFAULT_BASKET_BREAK_EVEN_ATR)
    if trigger <= 0 or atr <= 0 or len(request.positions) < 2:
        return None
    total_volume = sum(float(item.volume) for item in request.positions)
    if total_volume <= 0:
        return None
    weighted = sum(
        float(item.open_price) * float(item.volume) for item in request.positions
    ) / total_volume
    side = str(request.positions[0].side).upper()
    price = float(request.bid) if side == "BUY" else float(request.ask)
    favorable = (price - weighted) if side == "BUY" else (weighted - price)
    if favorable < trigger * atr:
        return None
    # A stop has to sit behind the market, which the average entry does once the
    # basket is ahead of it.
    if side == "BUY" and weighted >= price:
        return None
    if side == "SELL" and weighted <= price:
        return None
    return weighted, favorable / atr


def _protection_batch_decision(request: PositionEvaluateRequest, config: dict[str, Any], atr: float) -> TradeDecision | None:
    actions: list[dict[str, Any]] = []
    # The basket-wide level competes with each unit's own target and the better one
    # wins. Unit-by-unit protection leaves the newest unit naked until it earns its
    # own 1.0 ATR: a two-unit basket at +83 and +22 points had the first protected
    # and the second still a full stop from its own, so an ordinary pullback
    # stopped the second at a loss while the first kept its profit.
    basket = _basket_break_even_level(request, config, atr)
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
        target = float(candidate.sl) if candidate is not None and candidate.sl is not None else None
        comment = candidate.reason if candidate is not None else ""
        if basket is not None:
            level, favorable = basket
            current = float(position.sl) if position.sl and float(position.sl) > 0 else None
            better = (
                level > current if position.side == "BUY" else level < current
            ) if current is not None else True
            if better and (
                target is None
                or (level > target if position.side == "BUY" else level < target)
            ):
                target = level
                comment = f"整篮保本：持仓整体浮盈 {favorable:.1f} 倍ATR，全部止损移至加权成本"
        if target is None:
            continue
        actions.append({
            "action": "modify",
            "ticket": str(position.ticket),
            "sl": target,
            "tp": candidate.tp if candidate is not None else None,
            "comment": comment,
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
    else:
        if position.open_price - request.ask < trigger * atr:
            return None
        target = position.open_price - offset
    # A live gold basket never reached break-even because every request was
    # refused with "Invalid S/L or T/P": the target sat closer to the market than
    # the broker allows. Pull it back to a level the broker accepts, and if that
    # leaves no improvement over the stop already in place, send nothing.
    target, clamped = respect_min_stop(
        target, side=position.side, bid=request.bid, ask=request.ask,
        info=request.symbol_info, config=config, atr=atr,
    )
    if not stop_is_placeable(
        target, side=position.side, bid=request.bid, ask=request.ask,
        info=request.symbol_info, config=config, atr=atr,
    ):
        # The market already moved past this level; the broker would refuse the
        # modification, so keep the stop that is in force until it can hold.
        return None
    if position.side == "BUY":
        if current_sl is not None and current_sl >= target:
            return None
    else:
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
            "min_stop_distance_enforced": round(clamped, 5),
            "min_stop_distance_used": round(
                min_stop_distance(request.symbol_info, config, atr=atr), 5
            ),
        },
    )


def _trailing_stop_decision(request: PositionEvaluateRequest, position: Any, config: dict[str, Any], atr: float) -> TradeDecision | None:
    start = _positive_float(config.get("trailing_start_atr"), default=DEFAULT_TRAILING_START_ATR)
    distance = _positive_float(config.get("trailing_distance_atr"), default=DEFAULT_TRAILING_DISTANCE_ATR)
    configured_step = config.get("trailing_min_step_atr")
    step = DEFAULT_TRAILING_MIN_STEP_ATR if configured_step is None else _config_number(
        config, "trailing_min_step_atr", DEFAULT_TRAILING_MIN_STEP_ATR
    )
    minimum_gain = max(step, 0.0) * atr
    if start <= 0 or distance <= 0 or atr <= 0:
        return None
    current_sl = position.sl if position.sl and position.sl > 0 else None
    if position.side == "BUY":
        favorable = request.bid - position.open_price
        if favorable < start * atr:
            return None
        target = request.bid - distance * atr
    else:
        favorable = position.open_price - request.ask
        if favorable < start * atr:
            return None
        target = request.ask + distance * atr
    # Same broker limit as break-even: a trailing level the broker refuses leaves
    # the position on its old stop, which is what made gold look untrailed.
    target, clamped = respect_min_stop(
        target, side=position.side, bid=request.bid, ask=request.ask,
        info=request.symbol_info, config=config, atr=atr,
    )
    if not stop_is_placeable(
        target, side=position.side, bid=request.bid, ask=request.ask,
        info=request.symbol_info, config=config, atr=atr,
    ):
        # Price has already run past the trailing level, so the broker would
        # refuse it; the stop in force stays until the market allows the move.
        return None
    if position.side == "BUY":
        if target <= position.open_price or not _stop_improves(
            target, current_sl, side="BUY", minimum_gain=minimum_gain
        ):
            return None
    else:
        if target >= position.open_price or not _stop_improves(
            target, current_sl, side="SELL", minimum_gain=minimum_gain
        ):
            return None
    return TradeDecision(
        decision_id=_id(), request_id=request.request_id, status="APPROVED",
        action="MODIFY_SL", symbol=request.symbol, confidence=1.0,
        reason=f"浮盈达{start:g}倍ATR，止损跟进至现价回撤{distance:g}倍ATR处",
        expires_at=_expires(), position_ticket=position.ticket, sl=target, tp=position.tp,
        metadata={
            "strategy_code": "GL_TREND_V1",
            "trailing_start_atr": start,
            "trailing_distance_atr": distance,
            "min_stop_distance_enforced": round(clamped, 5),
            "min_stop_distance_used": round(
                min_stop_distance(request.symbol_info, config, atr=atr), 5
            ),
        },
    )


def _stop_improves(
    target: float,
    current: float | None,
    *,
    side: str,
    minimum_gain: float,
) -> bool:
    """Whether a new stop is worth sending to the broker.

    It must be strictly better than the stop already in force - a stop is never
    loosened - and, once a minimum gain is set, better by at least that much. A
    stop that improves by a tick has no trading value but costs a round trip and,
    on some brokers, another chance of a refused modification.
    """
    if not current or current <= 0:
        return True
    if side == "BUY":
        return target >= current + minimum_gain if minimum_gain > 0 else target > current
    return target <= current - minimum_gain if minimum_gain > 0 else target < current


def _basket_stop_floor(*, positions: list[Any], side: str, config: dict[str, Any]) -> float:
    """The lowest level the basket stop may sit at, from the deployment setting.

    The unified stop is anchored at the furthest entry, which for a young basket
    sits below where it started: with a single add-on the stop lands one ATR
    under the first unit, so an ordinary pullback stops the whole basket at a
    loss even though it was well in profit at the top.

    ``first_entry`` (the default) keeps the stop at the earliest unit's entry so
    the basket cannot be stopped below where it opened. ``farthest_entry`` puts it
    at the newest unit's entry, where no unit can be stopped at a loss. ``off``
    restores the behaviour before this existed.
    """
    mode = str(config.get("basket_stop_floor") or "first_entry").strip().lower()
    if mode in {"off", "none", "0", "false", "no"}:
        return 0.0

    entries = [float(item.open_price) for item in positions if float(item.open_price or 0) > 0]
    if not entries:
        return 0.0
    if mode in {"farthest", "farthest_entry", "last_entry"}:
        return max(entries) if side == "BUY" else min(entries)
    # first_entry: a pyramid's earliest unit is the lowest for a buy and the
    # highest for a sell, so the extreme of the entries is that first unit.
    return min(entries) if side == "BUY" else max(entries)


def _give_back_decision(
    request: PositionEvaluateRequest,
    positions: list[Any],
    config: dict[str, Any],
    atr: float,
    candles: list[Any],
) -> TradeDecision | None:
    """Take the basket off once it has handed back most of its best profit.

    A four-unit basket peaked well in profit and then returned to nothing: the
    protective levels only ever ask how much has been made, never how much of it
    has been given back, so nothing acted on the way down. The peak is derived
    from the bars since the basket opened, so no state has to be carried between
    requests and a restart cannot lose it.

    ``give_back_ratio`` is the share of the peak the basket keeps (0.5 by
    default, 0 to switch it off). Nothing fires until the peak itself was worth
    protecting, so ordinary early noise is left alone.
    """
    # An explicit 0 switches the rule off.
    ratio = _config_number(config, "give_back_ratio", DEFAULT_GIVE_BACK_RATIO)
    if ratio <= 0 or ratio >= 1 or atr <= 0 or not positions:
        return None

    sides = {item.side for item in positions}
    if len(sides) != 1:
        return None
    side = next(iter(sides))

    entries = [float(item.open_price) for item in positions if float(item.open_price or 0) > 0]
    if not entries:
        return None
    first_entry = min(entries) if side == "BUY" else max(entries)

    price = float(request.bid) if side == "BUY" else float(request.ask)
    current = (price - first_entry) if side == "BUY" else (first_entry - price)

    opened_at = min((int(item.open_time or 0) for item in positions), default=0)
    if opened_at <= 0:
        # Without an open time the bars cannot be attributed to this basket, and a
        # peak measured over older bars would fire the rule far too early. The
        # trailing stop is the protection that does not depend on it, so nothing
        # is lost by standing down here.
        return None
    relevant = [bar for bar in candles if int(bar.timestamp) >= opened_at]
    if not relevant:
        return None
    if side == "BUY":
        peak = max(float(bar.high) for bar in relevant) - first_entry
    else:
        peak = first_entry - min(float(bar.low) for bar in relevant)
    peak = max(peak, current)

    if peak < GIVE_BACK_MIN_ATR * atr:
        return None
    if current > peak * ratio:
        return None
    return _close_basket(
        request,
        list(positions),
        f"浮盈回吐保护：峰值浮盈 {peak / atr:.1f} 倍ATR，已回吐至 {current / atr:.1f} 倍ATR"
        f"（回吐超过 {(1 - ratio) * 100:.0f}%），全部离场",
    )


def _config_number(config: dict[str, Any], key: str, default: float) -> float:
    """A numeric deployment setting where an explicit 0 means zero.

    _positive_float treats 0 as unusable and returns the default, which is wrong
    for the switches that use 0 to mean "off". That mistake has been made three
    times in this file, so the settings that need it go through here.
    """
    value = config.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quote_gap_atr(request: PositionEvaluateRequest, atr: float) -> float:
    """How far the request's quote sits from its own positions' price, in ATR."""
    if atr <= 0:
        return 0.0
    prices = [float(item.current_price) for item in request.positions if item.current_price]
    if not prices:
        return 0.0
    reference = sum(prices) / len(prices)
    quote = (float(request.bid) + float(request.ask)) / 2.0
    return abs(quote - reference) / atr


def _quote_divergence(
    request: PositionEvaluateRequest,
    config: dict[str, Any],
    atr: float,
) -> str | None:
    """Refuse to act when the request's quote disagrees with its own positions.

    A client sent market data about forty points away from the live price, and on
    the same symbol twice a day apart, so the basket stop computed from it sat
    above the market for a long basket and every add-on and modification was
    refused as invalid. The quote and the per-position price come from different
    fields of the same payload, which is what makes a large gap informative:
    acting on a quote that stale is worse than doing nothing, and the stops
    already held by the broker stay in force either way.

    ``max_quote_divergence_atr`` sets the tolerance (1.0 ATR by default, 0 to
    switch the check off).
    """
    limit = _config_number(config, "max_quote_divergence_atr", DEFAULT_MAX_QUOTE_DIVERGENCE_ATR)
    if limit <= 0 or atr <= 0 or not request.positions:
        return None
    prices = [float(item.current_price) for item in request.positions if item.current_price]
    if not prices:
        return None
    reference = sum(prices) / len(prices)
    quote = (float(request.bid) + float(request.ask)) / 2.0
    divergence = abs(quote - reference)
    if divergence <= limit * atr:
        return None
    return (
        f"行情报价 {quote:.5f} 与持仓现价 {reference:.5f} 相差 {divergence / atr:.1f} 倍ATR，"
        "疑似数据过期，本轮不动作"
    )


def _maybe_add(
    request: PositionEvaluateRequest,
    config: dict[str, Any],
    atr: float,
) -> tuple[TradeDecision | None, str]:
    """Return the add-on decision plus the reason it could not be sized.

    The reason is only set when the symbol info made the position size
    uncomputable, so a client EA that predates the contract metadata cannot be
    silently ignored: the operator sees why nothing was added.
    """
    if not bool(config.get("allow_add", False)) or atr <= 0 or len(request.positions) >= _max_units(config):
        return None, ""
    # An add-on is a new entry, so the closed windows apply to it too.
    if time_windows.closed_window_reason(config, time_windows.now_utc()):
        return None, ""
    sides = {item.side for item in request.positions}
    if len(sides) != 1:
        return None, ""
    side = next(iter(sides))
    direction = "buy" if side == "BUY" else "sell"
    step = float(config.get("add_step_atr") or DEFAULT_ADD_STEP_ATR)
    if side == "BUY":
        anchor = max(item.open_price for item in request.positions)
        if request.bid < anchor + step * atr:
            return None, ""
        entry, action, stop_loss = request.ask, "BUY", request.ask - STOP_ATR * atr
    else:
        anchor = min(item.open_price for item in request.positions)
        if request.ask > anchor - step * atr:
            return None, ""
        entry, action, stop_loss = request.bid, "SELL", request.bid + STOP_ATR * atr
    # Recorded before the floor is applied so the raw level can be told apart
    # from the floored one, and the quote gap is recorded so a request whose
    # market block disagrees with its own positions is visible in the log.
    stop_before_floor = stop_loss
    divergence_atr = _quote_gap_atr(request, atr)
    # The floor exists for a pyramided basket: it keeps a basket that has already
    # added units from being stopped below where it started. With a single unit
    # there is nothing to protect against and it would simply place the stop at
    # the entry itself, which the broker refuses.
    floor_used = 0.0
    if len(request.positions) > 1:
        floor = _basket_stop_floor(positions=request.positions, side=side, config=config)
        if floor > 0:
            floor_used = floor
            stop_loss = max(stop_loss, floor) if side == "BUY" else min(stop_loss, floor)
    # The added unit carries the basket stop, which the broker has to accept as
    # well; sizing then uses the distance the broker will actually hold.
    stop_loss, stop_clamped = respect_min_stop(
        stop_loss, side=action, bid=request.bid, ask=request.ask,
        info=request.symbol_info, config=config, atr=atr,
    )
    if not stop_is_placeable(
        stop_loss, side=action, bid=request.bid, ask=request.ask,
        info=request.symbol_info, config=config, atr=atr,
    ):
        # A basket stop the broker refuses would leave every unit unprotected at
        # the new level, so the add is skipped and the old stops stay in force.
        return None, "加仓止损位不在市价正确一侧（报价可能已过期），本次不加仓"
    lot, sizing = _unit_lot(request, config, entry=entry, stop_loss=stop_loss)
    if lot <= 0:
        return None, (
            "加仓手数无法确定：合约参数缺失或单位不匹配"
            f"（{sizing.get('risk_source') or sizing.get('mode')}），本次不加仓"
        )
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
                  # The inputs the stop came from, so a level that looks wrong on
                  # the client can be checked against what the server actually
                  # used instead of being inferred from the order log.
                  "add_bid": round(float(request.bid), 6),
                  "add_ask": round(float(request.ask), 6),
                  "add_atr": round(float(atr), 6),
                  "add_stop_before_floor": round(stop_before_floor, 6),
                  "add_quote_divergence_atr": round(divergence_atr, 3),
                  # The entries the stop was anchored on. A client reported a cost
                  # basis the server did not agree with, and the level it produced
                  # matched the position's stop rather than its entry, so what the
                  # server actually received is recorded here.
                  "add_entries": [round(float(item.open_price), 6) for item in request.positions],
                  "add_floor": round(floor_used, 6),
                  "min_stop_distance_enforced": round(stop_clamped, 5),
                  "min_stop_distance_used": round(
                      min_stop_distance(request.symbol_info, config, atr=atr), 5
                  ),
                  "batch_actions": batch_actions,
                  },
    ), ""


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


_OPEN_RISK_FORMAT_REMINDER = (
    "Your previous answer did not follow the output contract. "
    "Answer again with exactly this JSON object and nothing else: "
    '{"allow_open": true|false, "risk_level": "low"|"medium"|"high", '
    '"reason": "short Chinese text", "analysis": "short Chinese text"}. '
    "risk_level is mandatory and must be one of those three values."
)


_APPROVAL_KEYS = ("allow_open", "should_open", "approved", "approve")


def _open_risk_outcome(result: Any) -> tuple[bool, str, Any] | None:
    """Read an entry-risk verdict, or None when the answer carries nothing usable.

    The approval flag is read from whichever key the model used. The generic
    prompt shape asks for should_open while this gate was written against
    allow_open, and the model follows its habit, so reading only one spelling
    made every verdict look unreadable - entries then opened regardless or were
    all refused, depending on how the unreadable case was treated.

    A flag that is present decides the entry: true approves, false vetoes. The
    risk level only decorates the wording, so a model that omits it can still
    approve. That is what makes "the analysis warned about risk yet it opened"
    impossible when the model actually answers false.
    """
    content = result.content if isinstance(result.content, dict) else {}
    risk_level = str(content.get("risk_level") or "").strip().lower()
    reason = str(content.get("reason") or "").strip()
    # The full analysis is what the panel should show; the one-line reason is only
    # the fallback for callers that do not return one.
    ai_text = str(content.get("analysis") or "").strip() or reason

    approval: bool | None = None
    for key in _APPROVAL_KEYS:
        if key in content and content.get(key) not in (None, ""):
            approval = _truthy(content.get(key))
            break

    if approval is True:
        note = f"AI 风险评估通过{_risk_level_note(risk_level)}"
        return True, (f"{note}：{ai_text}" if ai_text else note), result.usage
    if approval is False:
        note = f"AI 判定不宜开仓{_risk_level_note(risk_level)}，本次不开仓"
        return False, (f"{note}：{ai_text}" if ai_text else note), result.usage
    if _ai_risk_is_high(risk_level):
        return False, (ai_text or "AI 判定当前风险偏高"), result.usage
    if _ai_risk_is_known(risk_level):
        # Lenient by design: a mild "no" from the model must not veto an entry the
        # deterministic rules already qualified, and the panel says so, because
        # "the AI warned yet it still opened" is the question customers ask.
        return (
            True,
            f"AI 持保留意见（风险{_cn_risk_level(risk_level)}），未达到否决标准，"
            "按策略规则开仓（AI 仅在判定高风险时才阻止开仓）"
            + (f"：{ai_text}" if ai_text else ""),
            result.usage,
        )
    return None


def _ai_risk_is_known(risk_level: str) -> bool:
    """Whether the model answered the risk level the prompt asks for.

    The prompt requires exactly low, medium or high. Anything else is a malformed
    verdict and must not be read as "not high", which would quietly open the
    trade the model was warning about.
    """
    text = str(risk_level or "").strip().lower()
    return any(marker in text for marker in ("high", "medium", "low", "高", "中", "低"))


def _ai_risk_is_high(risk_level: str) -> bool:
    """Only an explicit high-risk verdict may veto a server-approved order."""
    text = str(risk_level or "").strip().lower()
    return "high" in text or "高" in text


def _cn_direction(direction: str) -> str:
    """Business wording for a direction, never the internal field value."""
    return "做多" if str(direction or "").strip().lower() in {"buy", "bullish", "long"} else "做空"


def _risk_level_note(risk_level: str) -> str:
    """Render a risk level only when the model actually supplied one.

    The models in use follow the generic prompt shape, which carries no
    risk_level, so every panel used to read "风险未分级" - accurate, but it reads
    like a fault the operator cannot act on. The approval flag is the meaningful
    signal, so the level is shown only when it exists.
    """
    return f"（风险{_cn_risk_level(risk_level)}）" if _ai_risk_is_known(risk_level) else ""


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

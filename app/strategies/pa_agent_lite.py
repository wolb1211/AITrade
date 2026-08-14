from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import Candle, OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision
from app.services.ai_service import AiDecisionClient


@dataclass(frozen=True)
class PaFeatureSnapshot:
    atr14: float
    ema20: float | None
    range_high: float
    range_low: float
    range_width_atr: float
    range_position: float
    zone: str
    dist_to_high_atr: float
    dist_to_low_atr: float
    overlap_mean: float
    doji_inside_ratio: float
    barbwire_score: float
    barbwire_candidate: bool
    bull_trend_bars: int
    bear_trend_bars: int
    breakout: str
    breakout_event: str
    swing_structure: str
    pullback_depth_atr: float
    pullback_bars: int
    h_count: int
    l_count: int
    support_1: float | None
    resistance_1: float | None
    invalidation_long: float | None
    invalidation_short: float | None
    background_direction: str
    recent_direction: str
    trend_relationship: str
    recent_spike: str | None
    cycle_position: str
    detected_patterns: tuple[str, ...]
    bar_by_bar: tuple[dict[str, Any], ...]
    market_phase: str
    transition_risk: str
    climax_risk: str
    always_in: str
    signal_bar_type: str
    signal_bar_quality: str
    follow_through: str
    wedge_type: str
    triangle_type: str
    double_structure: str
    mtr_candidate: bool
    final_flag_candidate: bool
    setup_bias: str
    setup_score: int
    setup_name: str
    last_close: float


class PaAgentLiteStrategy:
    """First-party PA strategy inspired by PA Agent's feature-first workflow.

    This is a clean-room implementation for GainLab. It keeps the first version
    deterministic so MT5, API, and strategy-library routing can be tested before
    the two-stage AI pipeline is connected.
    """

    code = "PA_AGENT_V1"

    def __init__(self, ai_client: AiDecisionClient | None = None) -> None:
        self.ai_client = ai_client

    @staticmethod
    def _expires_at() -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=60)

    @staticmethod
    def _decision_id() -> str:
        return f"dec_{uuid4().hex}"

    def evaluate_open(
        self,
        request: OpenEvaluateRequest,
        deployment: dict[str, Any],
    ) -> TradeDecision:
        features = _compute_features(request.candles)
        if features is None:
            return _hold(request, self._decision_id(), "PA Agent strategy requires at least 30 candles")

        ai_decision = self._evaluate_open_with_ai(request, deployment, features)
        if ai_decision is not None:
            return ai_decision

        if _is_choppy(features):
            return _hold(
                request,
                self._decision_id(),
                "PA Agent detected overlapping range conditions; waiting for cleaner price action",
                confidence=0.3,
            )

        direction = _open_direction(features)
        if direction is None:
            return _hold(
                request,
                self._decision_id(),
                _describe_hold(features),
                confidence=0.35,
            )

        return _build_open_decision(request, deployment, features, direction)

    def evaluate_position(
        self,
        request: PositionEvaluateRequest,
        deployment: dict[str, Any],
    ) -> TradeDecision:
        features = _compute_features(request.candles)
        position = request.positions[0]
        config = deployment["config"]
        max_loss, take_profit = _position_money_limits(config, request)

        if position.profit <= -max_loss:
            return _close(request, self._decision_id(), position.ticket, "Position reached configured maximum loss")
        if position.profit >= take_profit:
            return _close(request, self._decision_id(), position.ticket, "Position reached configured profit target")
        if features is None:
            return _position_hold(request, self._decision_id(), position.ticket, "PA Agent position strategy requires at least 30 candles")

        ai_decision = self._evaluate_position_with_ai(request, deployment, features)
        if ai_decision is not None:
            if ai_decision.action == "CLOSE" and _cooldown_blocks_close(request, position, features):
                return _position_hold(
                    request,
                    self._decision_id(),
                    position.ticket,
                    _cooldown_reason(request, position, features),
                    confidence=0.52,
                    usage=ai_decision.usage,
                )
            return ai_decision

        open_signal = _open_direction(features)
        if position.side == "BUY" and open_signal == "SELL":
            if _cooldown_blocks_close(request, position, features):
                return _position_hold(request, self._decision_id(), position.ticket, _cooldown_reason(request, position, features))
            return _close(request, self._decision_id(), position.ticket, "PA Agent detected opposite bearish price-action setup")
        if position.side == "SELL" and open_signal == "BUY":
            if _cooldown_blocks_close(request, position, features):
                return _position_hold(request, self._decision_id(), position.ticket, _cooldown_reason(request, position, features))
            return _close(request, self._decision_id(), position.ticket, "PA Agent detected opposite bullish price-action setup")

        spread_price = abs(request.ask - request.bid)
        trail_gap = max(features.atr14, spread_price * 5)
        if position.side == "BUY" and position.profit > 0:
            new_sl = max(position.sl or 0.0, request.bid - trail_gap)
            if new_sl > 0 and (position.sl is None or new_sl > position.sl):
                return _modify(request, self._decision_id(), position.ticket, "PA Agent moved buy stop under recent structure", sl=new_sl, tp=position.tp)
        if position.side == "SELL" and position.profit > 0:
            new_sl = request.ask + trail_gap
            if position.sl is None or new_sl < position.sl:
                return _modify(request, self._decision_id(), position.ticket, "PA Agent moved sell stop above recent structure", sl=new_sl, tp=position.tp)

        return _position_hold(request, self._decision_id(), position.ticket, "PA Agent position management conditions remain valid")

    def _evaluate_open_with_ai(
        self,
        request: OpenEvaluateRequest,
        deployment: dict[str, Any],
        features: PaFeatureSnapshot,
    ) -> TradeDecision | None:
        if self.ai_client is None:
            return None
        result = self.ai_client.pa_open_decision(
            deployment=deployment,
            request_payload=request,
            features=_feature_dict(features),
        )
        if result is None:
            return None

        content = result.content
        reason = str(content.get("reason") or "AI strategy returned hold").strip()[:500]
        should_open = bool(content.get("should_open", False))
        direction = str(content.get("direction") or "").strip().lower()
        confidence = _clamp(float(content.get("confidence") or 0.45), 0.0, 1.0)
        if not should_open or direction not in {"buy", "sell"}:
            return _hold(
                request,
                self._decision_id(),
                reason or "AI strategy decided to hold",
                confidence=confidence,
                usage=result.usage,
            )

        config = deployment["config"]
        spread_price = abs(request.ask - request.bid)
        min_distance = max(spread_price * 30, features.atr14 * 1.2)
        sl_distance = max(abs(float(content.get("sl_distance_price") or 0.0)), min_distance)
        tp_distance = max(abs(float(content.get("tp_distance_price") or 0.0)), min_distance * 1.5)

        if direction == "buy":
            entry = request.ask
            action = "BUY"
            sl = entry - sl_distance
            tp = entry + tp_distance
        else:
            entry = request.bid
            action = "SELL"
            sl = entry + sl_distance
            tp = entry - tp_distance

        if config.get("position_size_mode") == "risk":
            lot = _position_size_lot(config, request, entry=entry, sl=sl)
        else:
            lot = _fixed_lot(config)

        space_reason = _space_block_reason(features, action, entry)
        if space_reason:
            return _hold(
                request,
                self._decision_id(),
                f"AI candidate blocked by space filter: {space_reason}; AI reason={reason}",
                confidence=min(confidence, 0.48),
                usage=result.usage,
            )

        return TradeDecision(
            decision_id=self._decision_id(),
            request_id=request.request_id,
            status="APPROVED",
            action=action,
            symbol=request.symbol,
            confidence=confidence,
            reason=reason or "AI strategy approved an open order",
            expires_at=self._expires_at(),
            lot=lot,
            entry=entry,
            sl=sl,
            tp=tp,
            usage=result.usage,
        )

    def _evaluate_position_with_ai(
        self,
        request: PositionEvaluateRequest,
        deployment: dict[str, Any],
        features: PaFeatureSnapshot,
    ) -> TradeDecision | None:
        if self.ai_client is None:
            return None
        result = self.ai_client.pa_position_decision(
            deployment=deployment,
            request_payload=request,
            features=_feature_dict(features),
        )
        if result is None:
            return None

        content = result.content
        action = str(content.get("action") or "hold").strip().lower()
        reason = str(content.get("reason") or "AI position strategy returned hold").strip()[:500]
        confidence = _clamp(float(content.get("confidence") or 0.45), 0.0, 1.0)
        ticket = str(content.get("ticket") or request.positions[0].ticket)
        target = next((item for item in request.positions if item.ticket == ticket), request.positions[0])

        if action == "close":
            decision = _close(request, self._decision_id(), target.ticket, reason or "AI strategy requested close")
            decision.confidence = confidence
            decision.usage = result.usage
            return decision
        if action == "modify":
            sl = _optional_float(content.get("sl"))
            tp = _optional_float(content.get("tp"))
            if sl is None and tp is None:
                return None
            decision = _modify(
                request,
                self._decision_id(),
                target.ticket,
                reason or "AI strategy requested stop/take-profit modification",
                sl=sl,
                tp=tp,
            )
            decision.confidence = confidence
            decision.usage = result.usage
            return decision
        if action == "add":
            direction = str(content.get("direction") or "").strip().lower()
            if direction not in {"buy", "sell"}:
                return None
            lot = _fixed_lot(deployment["config"])
            spread_price = abs(request.ask - request.bid)
            min_distance = max(spread_price * 30, features.atr14 * 1.2)
            if direction == "buy":
                entry = request.ask
                sl = entry - min_distance
                tp = entry + min_distance * 1.5
                trade_action = "BUY"
            else:
                entry = request.bid
                sl = entry + min_distance
                tp = entry - min_distance * 1.5
                trade_action = "SELL"
            return TradeDecision(
                decision_id=self._decision_id(),
                request_id=request.request_id,
                status="APPROVED",
                action=trade_action,
                symbol=request.symbol,
                confidence=confidence,
                reason=reason or "AI strategy requested add position",
                expires_at=self._expires_at(),
                lot=lot,
                entry=entry,
                sl=sl,
                tp=tp,
                usage=result.usage,
            )
        if action == "hold":
            return _position_hold(
                request,
                self._decision_id(),
                target.ticket,
                reason or "AI strategy decided to hold position",
                confidence=confidence,
                usage=result.usage,
            )
        return None


def _hold(
    request: OpenEvaluateRequest,
    decision_id: str,
    reason: str,
    *,
    confidence: float = 0.25,
    usage: Any = None,
) -> TradeDecision:
    return TradeDecision(
        decision_id=decision_id,
        request_id=request.request_id,
        status="HOLD",
        action="HOLD",
        symbol=request.symbol,
        confidence=confidence,
        reason=reason,
        expires_at=PaAgentLiteStrategy._expires_at(),
        usage=usage or {},
    )


def _position_hold(
    request: PositionEvaluateRequest,
    decision_id: str,
    ticket: str,
    reason: str,
    *,
    confidence: float = 0.45,
    usage: Any = None,
) -> TradeDecision:
    return TradeDecision(
        decision_id=decision_id,
        request_id=request.request_id,
        status="HOLD",
        action="HOLD",
        symbol=request.symbol,
        confidence=confidence,
        reason=reason,
        expires_at=PaAgentLiteStrategy._expires_at(),
        position_ticket=ticket,
        usage=usage or {},
    )


def _close(
    request: PositionEvaluateRequest,
    decision_id: str,
    ticket: str,
    reason: str,
) -> TradeDecision:
    return TradeDecision(
        decision_id=decision_id,
        request_id=request.request_id,
        status="APPROVED",
        action="CLOSE",
        symbol=request.symbol,
        confidence=0.72,
        reason=reason,
        expires_at=PaAgentLiteStrategy._expires_at(),
        position_ticket=ticket,
    )


def _modify(
    request: PositionEvaluateRequest,
    decision_id: str,
    ticket: str,
    reason: str,
    *,
    sl: float | None,
    tp: float | None,
) -> TradeDecision:
    return TradeDecision(
        decision_id=decision_id,
        request_id=request.request_id,
        status="APPROVED",
        action="MODIFY_SL",
        symbol=request.symbol,
        confidence=0.62,
        reason=reason,
        expires_at=PaAgentLiteStrategy._expires_at(),
        position_ticket=ticket,
        sl=sl,
        tp=tp,
    )


def _compute_features(candles: list[Candle]) -> PaFeatureSnapshot | None:
    if len(candles) < 30:
        return None

    bars = candles[-80:]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    closes = [bar.close for bar in bars]

    atr14 = _atr(highs, lows, closes, 14)
    if atr14 <= 0:
        return None

    ema20 = _ema(closes, 20)
    window = bars[-20:]
    range_high = max(bar.high for bar in window)
    range_low = min(bar.low for bar in window)
    width = range_high - range_low
    last_close = bars[-1].close
    range_position = (last_close - range_low) / width if width > 0 else 0.5
    range_width_atr = width / atr14 if atr14 > 0 else 0.0
    zone = _zone(range_position)
    dist_to_high_atr = (range_high - last_close) / atr14 if atr14 > 0 else 0.0
    dist_to_low_atr = (last_close - range_low) / atr14 if atr14 > 0 else 0.0
    overlap_mean = _mean_overlap(window[-10:])
    doji_inside_ratio = _doji_inside_ratio(window[-10:])
    barbwire_score = _barbwire_score(overlap_mean, doji_inside_ratio, range_width_atr)
    bull_trend_bars = _same_direction_count(bars, bullish=True)
    bear_trend_bars = _same_direction_count(bars, bullish=False)
    breakout = _breakout(bars)
    breakout_event = _breakout_event(bars, range_high, range_low)
    pivots = _swing_pivots(bars[-40:])
    swing_structure = _swing_structure(pivots)
    support_1, resistance_1 = _nearest_levels(pivots, last_close)
    pullback_depth_atr, pullback_bars = _pullback_metrics(pivots, last_close, atr14)
    h_count, l_count = _hl_counts(bars[-20:])
    background_direction = _direction_vote(bars[:-40], atr14, ema20) if len(bars) > 50 else "neutral"
    recent_direction = _direction_vote(bars[-40:], atr14, ema20)
    recent_spike = _recent_spike(bars[-8:])
    trend_relationship = _trend_relationship(background_direction, recent_direction)
    cycle_position = _cycle_position(
        recent_spike=recent_spike,
        range_width_atr=range_width_atr,
        overlap_mean=overlap_mean,
        barbwire_score=barbwire_score,
        recent_direction=recent_direction,
        swing_structure=swing_structure,
        bull_trend_bars=bull_trend_bars,
        bear_trend_bars=bear_trend_bars,
    )
    bar_by_bar = _bar_by_bar_summary(bars[-8:], atr14, ema20)
    wedge_type = _wedge_type(pivots)
    triangle_type = _triangle_type(pivots)
    double_structure = _double_structure(pivots, atr14)
    always_in = _always_in(
        recent_direction=recent_direction,
        recent_spike=recent_spike,
        cycle_position=cycle_position,
        bull_trend_bars=bull_trend_bars,
        bear_trend_bars=bear_trend_bars,
    )
    signal_bar_type = str(bar_by_bar[0]["type"]) if bar_by_bar else "other"
    signal_bar_quality = _signal_bar_quality(signal_bar_type, bars[-1], atr14)
    follow_through = _follow_through(bars)
    climax_risk = _climax_risk(bars[-8:], atr14, ema20, recent_direction)
    mtr_candidate = _mtr_candidate(
        trend_relationship=trend_relationship,
        breakout_event=breakout_event,
        double_structure=double_structure,
        wedge_type=wedge_type,
        climax_risk=climax_risk,
    )
    final_flag_candidate = _final_flag_candidate(
        cycle_position=cycle_position,
        climax_risk=climax_risk,
        range_width_atr=range_width_atr,
        overlap_mean=overlap_mean,
    )
    market_phase, transition_risk = _market_phase(
        trend_relationship=trend_relationship,
        breakout_event=breakout_event,
        climax_risk=climax_risk,
        mtr_candidate=mtr_candidate,
        final_flag_candidate=final_flag_candidate,
    )
    detected_patterns = _detected_patterns(
        breakout=breakout,
        breakout_event=breakout_event,
        barbwire_score=barbwire_score,
        cycle_position=cycle_position,
        h_count=h_count,
        l_count=l_count,
        swing_structure=swing_structure,
        bar_by_bar=bar_by_bar,
        recent_spike=recent_spike,
        wedge_type=wedge_type,
        triangle_type=triangle_type,
        double_structure=double_structure,
        mtr_candidate=mtr_candidate,
        final_flag_candidate=final_flag_candidate,
        climax_risk=climax_risk,
    )
    setup = _score_setup(
        breakout=breakout,
        breakout_event=breakout_event,
        range_position=range_position,
        above_ema=ema20 is not None and last_close > ema20,
        below_ema=ema20 is not None and last_close < ema20,
        bull_trend_bars=bull_trend_bars,
        bear_trend_bars=bear_trend_bars,
        h_count=h_count,
        l_count=l_count,
        swing_structure=swing_structure,
        barbwire_score=barbwire_score,
        pullback_depth_atr=pullback_depth_atr,
        dist_to_high_atr=dist_to_high_atr,
        dist_to_low_atr=dist_to_low_atr,
        cycle_position=cycle_position,
        recent_direction=recent_direction,
        trend_relationship=trend_relationship,
        detected_patterns=detected_patterns,
        market_phase=market_phase,
        transition_risk=transition_risk,
        climax_risk=climax_risk,
        always_in=always_in,
        signal_bar_quality=signal_bar_quality,
        follow_through=follow_through,
    )

    return PaFeatureSnapshot(
        atr14=atr14,
        ema20=ema20,
        range_high=range_high,
        range_low=range_low,
        range_width_atr=range_width_atr,
        range_position=range_position,
        zone=zone,
        dist_to_high_atr=dist_to_high_atr,
        dist_to_low_atr=dist_to_low_atr,
        overlap_mean=overlap_mean,
        doji_inside_ratio=doji_inside_ratio,
        barbwire_score=barbwire_score,
        barbwire_candidate=barbwire_score >= 0.6,
        bull_trend_bars=bull_trend_bars,
        bear_trend_bars=bear_trend_bars,
        breakout=breakout,
        breakout_event=breakout_event,
        swing_structure=swing_structure,
        pullback_depth_atr=pullback_depth_atr,
        pullback_bars=pullback_bars,
        h_count=h_count,
        l_count=l_count,
        support_1=support_1,
        resistance_1=resistance_1,
        invalidation_long=support_1 or range_low,
        invalidation_short=resistance_1 or range_high,
        background_direction=background_direction,
        recent_direction=recent_direction,
        trend_relationship=trend_relationship,
        recent_spike=recent_spike,
        cycle_position=cycle_position,
        detected_patterns=detected_patterns,
        bar_by_bar=bar_by_bar,
        market_phase=market_phase,
        transition_risk=transition_risk,
        climax_risk=climax_risk,
        always_in=always_in,
        signal_bar_type=signal_bar_type,
        signal_bar_quality=signal_bar_quality,
        follow_through=follow_through,
        wedge_type=wedge_type,
        triangle_type=triangle_type,
        double_structure=double_structure,
        mtr_candidate=mtr_candidate,
        final_flag_candidate=final_flag_candidate,
        setup_bias=setup["bias"],
        setup_score=int(setup["score"]),
        setup_name=str(setup["name"]),
        last_close=last_close,
    )


def _open_direction(features: PaFeatureSnapshot) -> str | None:
    above_ema = features.ema20 is not None and features.last_close > features.ema20
    below_ema = features.ema20 is not None and features.last_close < features.ema20
    if features.setup_score >= 70 and features.setup_bias == "bullish":
        return "BUY"
    if features.setup_score >= 70 and features.setup_bias == "bearish":
        return "SELL"
    if features.breakout == "up" and above_ema and features.range_position >= 0.7:
        return "BUY"
    if features.breakout == "down" and below_ema and features.range_position <= 0.3:
        return "SELL"
    if features.bull_trend_bars >= 3 and above_ema and features.range_position >= 0.6:
        return "BUY"
    if features.bear_trend_bars >= 3 and below_ema and features.range_position <= 0.4:
        return "SELL"
    return None


def _is_choppy(features: PaFeatureSnapshot) -> bool:
    width_atr = (features.range_high - features.range_low) / features.atr14
    return (
        features.setup_score < 70
        and features.overlap_mean >= 0.65
        and width_atr <= 3
    )


def _describe_open(features: PaFeatureSnapshot, direction: str) -> str:
    side = "bullish" if direction == "BUY" else "bearish"
    return (
        f"PA Agent {side} setup: {features.setup_name}, score={features.setup_score}, "
        f"breakout={features.breakout}/{features.breakout_event}, "
        f"cycle={features.cycle_position}, patterns={','.join(features.detected_patterns)}, "
        f"zone={features.zone}, range_position={features.range_position:.2f}, "
        f"trend={features.background_direction}/{features.recent_direction}/{features.trend_relationship}, "
        f"swing={features.swing_structure}, H/L={features.h_count}/{features.l_count}, "
        f"atr14={features.atr14:.5f}"
    )


def _describe_hold(features: PaFeatureSnapshot) -> str:
    ema_text = f"{features.ema20:.5f}" if features.ema20 is not None else "n/a"
    return (
        f"No trade: {features.setup_name}, score={features.setup_score}, "
        f"bias={features.setup_bias}, breakout={features.breakout}/{features.breakout_event}, "
        f"cycle={features.cycle_position}, patterns={','.join(features.detected_patterns)}, "
        f"zone={features.zone}, range_position={features.range_position:.2f}, "
        f"EMA20={ema_text}, "
        f"trend={features.background_direction}/{features.recent_direction}/{features.trend_relationship}, "
        f"swing={features.swing_structure}, barbwire={features.barbwire_score:.2f}"
    )


def _trace_summary(features: PaFeatureSnapshot) -> str:
    gate = (
        f"gate: data=ok, cycle={features.cycle_position}, "
        f"direction={features.recent_direction}, always_in={features.always_in}, "
        f"momentum={'ok' if features.setup_score >= 70 else 'weak'}"
    )
    decision = (
        f"decision: signal={features.signal_bar_quality}/{features.follow_through}, "
        f"phase={features.market_phase}, transition={features.transition_risk}, "
        f"climax={features.climax_risk}, patterns={','.join(features.detected_patterns)}"
    )
    return f"{gate}; {decision}"


def _space_block_reason(
    features: PaFeatureSnapshot,
    direction: str,
    entry: float,
) -> str:
    min_atr = 1.2
    hard_min_atr = 0.8
    resistance = features.resistance_1
    if resistance is None or resistance <= entry:
        resistance = features.range_high if features.range_high > entry else None
    support = features.support_1
    if support is None or support >= entry:
        support = features.range_low if features.range_low < entry else None
    if direction == "BUY" and resistance is not None and resistance > entry:
        distance = resistance - entry
        distance_atr = distance / features.atr14 if features.atr14 > 0 else 99.0
        if distance_atr < hard_min_atr or (distance_atr < min_atr and features.setup_score < 85):
            return (
                f"上方阻力空间不足: resistance={resistance:.5f}, "
                f"distance={distance:.5f}({distance_atr:.2f} ATR)"
            )
    if direction == "SELL" and support is not None and support < entry:
        distance = entry - support
        distance_atr = distance / features.atr14 if features.atr14 > 0 else 99.0
        if distance_atr < hard_min_atr or (distance_atr < min_atr and features.setup_score < 85):
            return (
                f"下方支撑空间不足: support={support:.5f}, "
                f"distance={distance:.5f}({distance_atr:.2f} ATR)"
            )
    return ""


def _cooldown_blocks_close(
    request: PositionEvaluateRequest,
    position: Any,
    features: PaFeatureSnapshot,
) -> bool:
    if position.open_time is None:
        return False
    bars_since_open = _bars_since_open(request, int(position.open_time))
    if bars_since_open <= 0:
        return True
    if bars_since_open >= 3:
        return False
    strong_opposite = (
        features.setup_score >= 95
        and features.follow_through == "yes"
        and (
            (position.side == "BUY" and features.setup_bias == "bearish")
            or (position.side == "SELL" and features.setup_bias == "bullish")
        )
    )
    deep_loss = position.profit < 0 and abs(position.profit) >= 50
    return not strong_opposite and not deep_loss


def _cooldown_reason(
    request: PositionEvaluateRequest,
    position: Any,
    features: PaFeatureSnapshot,
) -> str:
    bars = _bars_since_open(request, int(position.open_time or 0))
    return (
        f"新单冷却中: opened {bars} bars ago, 暂不主动平仓; "
        f"side={position.side}, profit={position.profit:.2f}, "
        f"setup_score={features.setup_score}, bias={features.setup_bias}"
    )


def _bars_since_open(request: PositionEvaluateRequest, open_time: int) -> int:
    open_seconds = _normalize_epoch_seconds(open_time)
    if open_seconds <= 0:
        return 999
    timeframe_seconds = _timeframe_seconds(request.timeframe)
    if timeframe_seconds <= 0:
        timeframe_seconds = _infer_bar_seconds(request.candles)
    if timeframe_seconds <= 0:
        timeframe_seconds = 300
    return max(0, int((request.bar_time - open_seconds) // timeframe_seconds))


def _normalize_epoch_seconds(value: int) -> int:
    timestamp = int(value)
    if timestamp > 10_000_000_000_000:
        return timestamp // 1_000_000
    if timestamp > 10_000_000_000:
        return timestamp // 1_000
    return timestamp


def _timeframe_seconds(timeframe: str) -> int:
    text = str(timeframe or "").strip().upper()
    if text.startswith("M") and text[1:].isdigit():
        return int(text[1:]) * 60
    if text.startswith("H") and text[1:].isdigit():
        return int(text[1:]) * 3600
    if text in {"D", "D1"}:
        return 86400
    return 0


def _infer_bar_seconds(candles: list[Candle]) -> int:
    if len(candles) < 2:
        return 0
    return max(0, int(candles[-1].timestamp - candles[-2].timestamp))


def _build_open_decision(
    request: OpenEvaluateRequest,
    deployment: dict[str, Any],
    features: PaFeatureSnapshot,
    direction: str,
) -> TradeDecision:
    config = deployment["config"]
    spread_price = abs(request.ask - request.bid)
    min_stop = max(spread_price * 30, features.atr14 * 1.2)

    if direction == "BUY":
        entry = request.ask
        space_reason = _space_block_reason(features, "BUY", entry)
        if space_reason:
            return _hold(
                request,
                PaAgentLiteStrategy._decision_id(),
                f"{space_reason}; {_describe_hold(features)}",
                confidence=0.42,
            )
        structure_sl = features.invalidation_long or features.range_low
        sl = min(structure_sl, entry - min_stop)
        risk = max(entry - sl, min_stop)
        tp = entry + risk * 1.8
    else:
        entry = request.bid
        space_reason = _space_block_reason(features, "SELL", entry)
        if space_reason:
            return _hold(
                request,
                PaAgentLiteStrategy._decision_id(),
                f"{space_reason}; {_describe_hold(features)}",
                confidence=0.42,
            )
        structure_sl = features.invalidation_short or features.range_high
        sl = max(structure_sl, entry + min_stop)
        risk = max(sl - entry, min_stop)
        tp = entry - risk * 1.8

    lot = _position_size_lot(config, request, entry=entry, sl=sl)
    return TradeDecision(
        decision_id=PaAgentLiteStrategy._decision_id(),
        request_id=request.request_id,
        status="APPROVED",
        action=direction,
        symbol=request.symbol,
        confidence=_clamp(0.45 + features.setup_score / 200, 0.5, 0.86),
        reason=f"{_describe_open(features, direction)}; {_trace_summary(features)}",
        expires_at=PaAgentLiteStrategy._expires_at(),
        lot=lot,
        entry=entry,
        sl=sl,
        tp=tp,
    )


def _position_size_lot(
    config: dict[str, Any],
    request: OpenEvaluateRequest,
    *,
    entry: float,
    sl: float | None,
) -> float:
    fixed_lot = _fixed_lot(config)
    if config.get("position_size_mode") != "risk" or sl is None:
        return fixed_lot

    price_risk = abs(entry - sl)
    if price_risk <= 0:
        return fixed_lot

    if config.get("risk_base_mode") == "balance_percent":
        risk_money = request.balance * _positive_float(config.get("risk_percent"), 1.0) / 100
    else:
        risk_money = _positive_float(config.get("risk_amount"), 10.0)
    if risk_money <= 0:
        return fixed_lot

    info = request.symbol_info or {}
    tick_size = _first_positive_float(
        info,
        "tick_size",
        "trade_tick_size",
        "tick",
        "point",
        "point_size",
    )
    tick_value = _first_positive_float(
        info,
        "tick_value",
        "trade_tick_value",
        "tick_value_profit",
        "trade_tick_value_profit",
        "tickVal",
        "tick_value_profit",
    )
    contract_size = _first_positive_float(info, "contract_size", "trade_contract_size", "contractSize")
    value_per_price = _first_positive_float(info, "value_per_price", "money_per_price", "valuePerPrice")
    value_per_point = _first_positive_float(info, "value_per_point", "money_per_point", "valuePerPoint")
    point = _first_positive_float(info, "point", "point_size")

    risk_per_lot = 0.0
    if tick_size and tick_value:
        risk_per_lot = price_risk / tick_size * tick_value
    elif value_per_price:
        risk_per_lot = price_risk * value_per_price
    elif point and value_per_point:
        risk_per_lot = price_risk / point * value_per_point
    elif contract_size:
        risk_per_lot = price_risk * contract_size
    if risk_per_lot <= 0:
        return fixed_lot

    raw_lot = risk_money / risk_per_lot
    min_lot = _first_positive_float(info, "volume_min", "lots_min", "min_lot", "minLot") or 0.01
    max_lot = _first_positive_float(info, "volume_max", "lots_max", "max_lot", "maxLot") or raw_lot
    step = _first_positive_float(info, "volume_step", "lots_step", "lot_step", "lotStep") or 0.01
    return _normalize_volume(raw_lot, min_lot=min_lot, max_lot=max_lot, step=step)


def _fixed_lot(config: dict[str, Any]) -> float:
    return _positive_float(config.get("fixed_volume") or config.get("lot"), 0.01)


def _position_money_limits(config: dict[str, Any], request: PositionEvaluateRequest) -> tuple[float, float]:
    if config.get("position_size_mode") != "risk":
        max_loss = _positive_float(config.get("max_loss_per_position"), 100.0)
        take_profit = _positive_float(config.get("take_profit_per_position"), 150.0)
        return abs(max_loss), abs(take_profit)

    if config.get("risk_base_mode") == "balance_percent":
        risk_money = request.balance * _positive_float(config.get("risk_percent"), 1.0) / 100
        if risk_money <= 0:
            risk_money = _positive_float(config.get("risk_amount"), 100.0)
    else:
        risk_money = _positive_float(config.get("risk_amount"), 100.0)

    max_loss = abs(risk_money)
    take_profit = max_loss * 1.8
    return max_loss, take_profit


def _positive_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def _first_positive_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        try:
            value = float(payload.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _normalize_volume(raw: float, *, min_lot: float, max_lot: float, step: float) -> float:
    if raw <= 0:
        return min_lot
    capped = min(max(raw, min_lot), max_lot)
    steps = int(capped / step)
    normalized = max(min_lot, steps * step)
    return round(normalized, 8)


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
    if len(closes) < period + 1:
        return 0.0
    ranges: list[float] = []
    for index in range(1, len(closes)):
        ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            ),
        )
    return sum(ranges[-period:]) / period


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    seed = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    current = seed
    for value in values[period:]:
        current = value * alpha + current * (1 - alpha)
    return current


def _mean_overlap(bars: list[Candle]) -> float:
    if len(bars) < 2:
        return 0.0
    ratios: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        high = min(previous.high, current.high)
        low = max(previous.low, current.low)
        overlap = max(0.0, high - low)
        denominator = max(previous.high, current.high) - min(previous.low, current.low)
        if denominator > 0:
            ratios.append(overlap / denominator)
    return sum(ratios) / len(ratios) if ratios else 0.0


def _doji_inside_ratio(bars: list[Candle]) -> float:
    if not bars:
        return 0.0
    count = 0
    for index, bar in enumerate(bars):
        full_range = max(bar.high - bar.low, 0.0)
        body = abs(bar.close - bar.open)
        is_doji = full_range > 0 and body / full_range <= 0.25
        previous = bars[index - 1] if index > 0 else None
        is_inside = previous is not None and bar.high <= previous.high and bar.low >= previous.low
        if is_doji or is_inside:
            count += 1
    return count / len(bars)


def _barbwire_score(overlap_mean: float, doji_inside_ratio: float, range_width_atr: float) -> float:
    score = 0.0
    if overlap_mean >= 0.65:
        score += 0.4
    if doji_inside_ratio >= 0.4:
        score += 0.3
    if 0 < range_width_atr <= 3:
        score += 0.3
    return min(score, 1.0)


def _direction_vote(bars: list[Candle], atr14: float, ema20: float | None) -> str:
    if len(bars) < 8:
        return "neutral"
    score = 0
    latest = bars[-1]
    older = bars[max(0, len(bars) - 12)]
    if ema20 is not None:
        if latest.close > ema20:
            score += 1
        elif latest.close < ema20:
            score -= 1
    if latest.close - older.close > atr14 * 0.5:
        score += 1
    elif older.close - latest.close > atr14 * 0.5:
        score -= 1
    bull, bear = _trend_bar_counts(bars[-20:])
    if bull >= bear * 1.5 and bull >= 3:
        score += 1
    if bear >= bull * 1.5 and bear >= 3:
        score -= 1
    pivots = _swing_pivots(bars[-30:])
    structure = _swing_structure(pivots)
    if structure == "HH_HL":
        score += 1
    elif structure == "LL_LH":
        score -= 1
    overlap = _mean_overlap(bars[-10:])
    if overlap < 0.35 and score > 0:
        score += 1
    elif overlap < 0.35 and score < 0:
        score -= 1
    if score >= 2:
        return "bullish"
    if score <= -2:
        return "bearish"
    return "neutral"


def _trend_bar_counts(bars: list[Candle]) -> tuple[int, int]:
    bull = 0
    bear = 0
    for bar in bars:
        full_range = max(bar.high - bar.low, 0.0)
        if full_range <= 0:
            continue
        body = abs(bar.close - bar.open) / full_range
        close_position = (bar.close - bar.low) / full_range
        if bar.close > bar.open and body >= 0.45 and close_position >= 0.65:
            bull += 1
        elif bar.close < bar.open and body >= 0.45 and close_position <= 0.35:
            bear += 1
    return bull, bear


def _recent_spike(bars: list[Candle]) -> str | None:
    if len(bars) < 5:
        return None
    bull, bear = _trend_bar_counts(bars)
    overlap = _mean_overlap(bars)
    if bull >= 3 and bull >= bear * 1.5 and overlap <= 0.35:
        return "bullish"
    if bear >= 3 and bear >= bull * 1.5 and overlap <= 0.35:
        return "bearish"
    return None


def _trend_relationship(background: str, recent: str) -> str:
    if background == recent and background != "neutral":
        return "aligned"
    if background in {"bullish", "bearish"} and recent in {"bullish", "bearish"} and background != recent:
        return "conflict"
    if background == "neutral":
        return "neutral_background"
    return "mixed"


def _cycle_position(
    *,
    recent_spike: str | None,
    range_width_atr: float,
    overlap_mean: float,
    barbwire_score: float,
    recent_direction: str,
    swing_structure: str,
    bull_trend_bars: int,
    bear_trend_bars: int,
) -> str:
    if recent_spike:
        return "spike"
    trend_streak = max(bull_trend_bars, bear_trend_bars)
    if trend_streak >= 5:
        return "micro_channel"
    if recent_direction != "neutral" and overlap_mean < 0.45 and swing_structure in {"HH_HL", "LL_LH"}:
        return "tight_channel"
    if recent_direction != "neutral" and overlap_mean < 0.6:
        return "normal_channel"
    if range_width_atr <= 3 or barbwire_score >= 0.6:
        return "trading_range"
    if overlap_mean >= 0.55 and recent_direction != "neutral":
        return "trending_tr"
    return "broad_channel" if recent_direction != "neutral" else "unknown"


def _bar_by_bar_summary(
    bars: list[Candle],
    atr14: float,
    ema20: float | None,
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for offset, bar in enumerate(reversed(bars), start=1):
        full_range = max(bar.high - bar.low, 0.0)
        body_ratio = abs(bar.close - bar.open) / full_range if full_range > 0 else 0.0
        close_position = (bar.close - bar.low) / full_range if full_range > 0 else 0.5
        bar_type = "other"
        if body_ratio <= 0.25:
            bar_type = "doji"
        elif bar.close > bar.open and close_position >= 0.65:
            bar_type = "trend_bull"
        elif bar.close < bar.open and close_position <= 0.35:
            bar_type = "trend_bear"
        ema_relation = "unknown"
        if ema20 is not None:
            ema_relation = "above" if bar.close > ema20 else "below" if bar.close < ema20 else "touch"
        out.append(
            {
                "bar": f"K{offset}",
                "type": bar_type,
                "body_ratio": round(body_ratio, 3),
                "close_position": round(close_position, 3),
                "range_atr": round(full_range / atr14, 3) if atr14 > 0 else 0.0,
                "ema_relation": ema_relation,
            },
        )
    return tuple(out)


def _detected_patterns(
    *,
    breakout: str,
    breakout_event: str,
    barbwire_score: float,
    cycle_position: str,
    h_count: int,
    l_count: int,
    swing_structure: str,
    bar_by_bar: tuple[dict[str, Any], ...],
    recent_spike: str | None,
    wedge_type: str,
    triangle_type: str,
    double_structure: str,
    mtr_candidate: bool,
    final_flag_candidate: bool,
    climax_risk: str,
) -> tuple[str, ...]:
    patterns: list[str] = []
    if breakout in {"up", "down"}:
        patterns.append("breakout_test")
    if breakout_event in {"breakout_up_retest", "breakout_down_retest"}:
        patterns.append("breakout_pullback")
    if breakout_event in {"failed_breakout_up", "failed_breakout_down"}:
        patterns.extend(["breakout_failure", "failed_signal"])
    if barbwire_score >= 0.6:
        patterns.extend(["barbwire", "overlap", "middle_range"])
    if cycle_position in {"trading_range", "trending_tr"}:
        patterns.append("middle_range")
    if recent_spike:
        patterns.append("always_in")
    if h_count >= 2:
        patterns.append("h2")
    elif h_count == 1:
        patterns.append("h1")
    if l_count >= 2:
        patterns.append("l2")
    elif l_count == 1:
        patterns.append("l1")
    if swing_structure in {"HH_HL", "LL_LH"}:
        patterns.append("trend_structure")
    if wedge_type != "none":
        patterns.append("wedge")
    if triangle_type != "none":
        patterns.append(triangle_type)
    if double_structure != "none":
        patterns.append("double_top_bottom")
    if mtr_candidate:
        patterns.extend(["mtr", "reversal_attempt"])
    if final_flag_candidate:
        patterns.append("final_flag")
    if climax_risk == "triggered":
        patterns.append("climax_triggered")
    elif climax_risk == "warning":
        patterns.append("climax_warning")
    if _has_micro_double(bar_by_bar):
        patterns.append("double_top_bottom")
    return tuple(dict.fromkeys(patterns))


def _has_micro_double(bar_by_bar: tuple[dict[str, Any], ...]) -> bool:
    return len(bar_by_bar) >= 2 and bar_by_bar[0]["type"] == bar_by_bar[1]["type"] == "doji"


def _wedge_type(pivots: list[dict[str, float | str | int]]) -> str:
    highs = [pivot for pivot in pivots if pivot["kind"] == "high"][-3:]
    lows = [pivot for pivot in pivots if pivot["kind"] == "low"][-3:]
    if len(highs) == 3:
        prices = [float(item["price"]) for item in highs]
        pushes = [prices[1] - prices[0], prices[2] - prices[1]]
        if prices[0] < prices[1] < prices[2] and pushes[1] < pushes[0] * 1.15:
            return "rising_wedge"
    if len(lows) == 3:
        prices = [float(item["price"]) for item in lows]
        pushes = [prices[0] - prices[1], prices[1] - prices[2]]
        if prices[0] > prices[1] > prices[2] and pushes[1] < pushes[0] * 1.15:
            return "falling_wedge"
    return "none"


def _triangle_type(pivots: list[dict[str, float | str | int]]) -> str:
    highs = [float(pivot["price"]) for pivot in pivots if pivot["kind"] == "high"][-3:]
    lows = [float(pivot["price"]) for pivot in pivots if pivot["kind"] == "low"][-3:]
    if len(highs) < 3 or len(lows) < 3:
        return "none"
    high_span = max(highs) - min(highs)
    low_span = max(lows) - min(lows)
    total_span = max(highs) - min(lows)
    flat_high = total_span > 0 and high_span / total_span <= 0.18
    flat_low = total_span > 0 and low_span / total_span <= 0.18
    rising_lows = lows[0] < lows[1] < lows[2]
    falling_highs = highs[0] > highs[1] > highs[2]
    expanding = highs[0] < highs[-1] and lows[0] > lows[-1]
    if flat_high and rising_lows:
        return "ascending_triangle"
    if flat_low and falling_highs:
        return "descending_triangle"
    if falling_highs and rising_lows:
        return "symmetrical_triangle"
    if expanding:
        return "expanding_triangle"
    return "none"


def _double_structure(pivots: list[dict[str, float | str | int]], atr14: float) -> str:
    tolerance = max(atr14 * 0.25, 0.0)
    highs = [float(pivot["price"]) for pivot in pivots if pivot["kind"] == "high"][-2:]
    lows = [float(pivot["price"]) for pivot in pivots if pivot["kind"] == "low"][-2:]
    if len(highs) == 2 and abs(highs[1] - highs[0]) <= tolerance:
        return "double_top"
    if len(lows) == 2 and abs(lows[1] - lows[0]) <= tolerance:
        return "double_bottom"
    return "none"


def _always_in(
    *,
    recent_direction: str,
    recent_spike: str | None,
    cycle_position: str,
    bull_trend_bars: int,
    bear_trend_bars: int,
) -> str:
    if recent_spike == "bullish" or (
        recent_direction == "bullish"
        and cycle_position in {"spike", "micro_channel", "tight_channel", "normal_channel"}
        and bull_trend_bars >= 3
    ):
        return "long"
    if recent_spike == "bearish" or (
        recent_direction == "bearish"
        and cycle_position in {"spike", "micro_channel", "tight_channel", "normal_channel"}
        and bear_trend_bars >= 3
    ):
        return "short"
    return "neutral"


def _signal_bar_quality(signal_bar_type: str, bar: Candle, atr14: float) -> str:
    full_range = max(bar.high - bar.low, 0.0)
    range_atr = full_range / atr14 if atr14 > 0 else 0.0
    if signal_bar_type in {"trend_bull", "trend_bear"} and 0.35 <= range_atr <= 2.0:
        return "strong"
    if signal_bar_type in {"trend_bull", "trend_bear"}:
        return "medium"
    if signal_bar_type in {"doji", "inside"}:
        return "weak"
    return "invalid"


def _follow_through(bars: list[Candle]) -> str:
    if len(bars) < 2:
        return "pending"
    signal = bars[-2]
    latest = bars[-1]
    if signal.close > signal.open:
        if latest.close > signal.close:
            return "yes"
        if latest.close < signal.open:
            return "failed"
        return "no"
    if signal.close < signal.open:
        if latest.close < signal.close:
            return "yes"
        if latest.close > signal.open:
            return "failed"
        return "no"
    return "pending"


def _climax_risk(
    bars: list[Candle],
    atr14: float,
    ema20: float | None,
    recent_direction: str,
) -> str:
    if len(bars) < 5 or atr14 <= 0:
        return "none"
    bull, bear = _trend_bar_counts(bars)
    ranges = [(bar.high - bar.low) / atr14 for bar in bars[-3:]]
    large_recent = sum(1 for item in ranges if item >= 1.6)
    latest = bars[-1]
    ema_distance = abs(latest.close - ema20) / atr14 if ema20 is not None else 0.0
    if recent_direction == "bullish" and bull >= 5 and (large_recent >= 2 or ema_distance >= 3):
        return "triggered"
    if recent_direction == "bearish" and bear >= 5 and (large_recent >= 2 or ema_distance >= 3):
        return "triggered"
    if max(bull, bear) >= 4 and (large_recent >= 1 or ema_distance >= 2):
        return "warning"
    return "none"


def _mtr_candidate(
    *,
    trend_relationship: str,
    breakout_event: str,
    double_structure: str,
    wedge_type: str,
    climax_risk: str,
) -> bool:
    if breakout_event in {"failed_breakout_up", "failed_breakout_down"}:
        return True
    if trend_relationship == "conflict" and double_structure != "none":
        return True
    if climax_risk in {"warning", "triggered"} and wedge_type != "none":
        return True
    return False


def _final_flag_candidate(
    *,
    cycle_position: str,
    climax_risk: str,
    range_width_atr: float,
    overlap_mean: float,
) -> bool:
    return (
        cycle_position in {"trading_range", "trending_tr"}
        and climax_risk in {"warning", "triggered"}
        and range_width_atr <= 4
        and overlap_mean >= 0.5
    )


def _market_phase(
    *,
    trend_relationship: str,
    breakout_event: str,
    climax_risk: str,
    mtr_candidate: bool,
    final_flag_candidate: bool,
) -> tuple[str, str]:
    risk = "low"
    if trend_relationship == "conflict" or breakout_event.startswith("failed"):
        risk = "medium"
    if climax_risk == "triggered" or mtr_candidate or final_flag_candidate:
        risk = "high"
    phase = "transitioning" if risk in {"medium", "high"} else "stable"
    return phase, risk


def _same_direction_count(bars: list[Candle], *, bullish: bool) -> int:
    count = 0
    for bar in reversed(bars):
        if bullish and bar.close > bar.open:
            count += 1
        elif not bullish and bar.close < bar.open:
            count += 1
        else:
            break
    return count


def _breakout(bars: list[Candle], lookback: int = 20) -> str:
    if len(bars) <= lookback:
        return "none"
    latest = bars[-1]
    previous = bars[-lookback - 1 : -1]
    previous_high = max(bar.high for bar in previous)
    previous_low = min(bar.low for bar in previous)
    broke_high = latest.close > previous_high
    broke_low = latest.close < previous_low
    if broke_high and broke_low:
        return "both"
    if broke_high:
        return "up"
    if broke_low:
        return "down"
    return "none"


def _breakout_event(bars: list[Candle], range_high: float, range_low: float) -> str:
    if len(bars) < 5:
        return "none"
    latest = bars[-1]
    previous = bars[-5:-1]
    recent_high = max(bar.high for bar in previous)
    recent_low = min(bar.low for bar in previous)
    if latest.close > recent_high and latest.low <= recent_high:
        return "breakout_up_retest"
    if latest.close < recent_low and latest.high >= recent_low:
        return "breakout_down_retest"
    if latest.high > range_high and latest.close < range_high:
        return "failed_breakout_up"
    if latest.low < range_low and latest.close > range_low:
        return "failed_breakout_down"
    if latest.close > range_high:
        return "range_breakout_up"
    if latest.close < range_low:
        return "range_breakout_down"
    return "none"


def _zone(position: float) -> str:
    if position >= 0.67:
        return "upper"
    if position <= 0.33:
        return "lower"
    return "middle"


def _swing_pivots(bars: list[Candle]) -> list[dict[str, float | str | int]]:
    pivots: list[dict[str, float | str | int]] = []
    if len(bars) < 5:
        return pivots
    for index in range(2, len(bars) - 2):
        current = bars[index]
        left = bars[index - 2 : index]
        right = bars[index + 1 : index + 3]
        if all(current.high > item.high for item in [*left, *right]):
            pivots.append({"kind": "high", "price": current.high, "index": index})
        if all(current.low < item.low for item in [*left, *right]):
            pivots.append({"kind": "low", "price": current.low, "index": index})
    return pivots[-10:]


def _swing_structure(pivots: list[dict[str, float | str | int]]) -> str:
    highs = [pivot for pivot in pivots if pivot["kind"] == "high"]
    lows = [pivot for pivot in pivots if pivot["kind"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "insufficient"
    high_new, high_old = highs[-1], highs[-2]
    low_new, low_old = lows[-1], lows[-2]
    hh = float(high_new["price"]) > float(high_old["price"])
    hl = float(low_new["price"]) > float(low_old["price"])
    ll = float(low_new["price"]) < float(low_old["price"])
    lh = float(high_new["price"]) < float(high_old["price"])
    if hh and hl:
        return "HH_HL"
    if ll and lh:
        return "LL_LH"
    return "mixed"


def _nearest_levels(
    pivots: list[dict[str, float | str | int]],
    close: float,
) -> tuple[float | None, float | None]:
    supports = sorted(
        [float(pivot["price"]) for pivot in pivots if pivot["kind"] == "low" and float(pivot["price"]) < close],
        reverse=True,
    )
    resistances = sorted(
        [float(pivot["price"]) for pivot in pivots if pivot["kind"] == "high" and float(pivot["price"]) > close],
    )
    return supports[0] if supports else None, resistances[0] if resistances else None


def _pullback_metrics(
    pivots: list[dict[str, float | str | int]],
    close: float,
    atr14: float,
) -> tuple[float, int]:
    if not pivots or atr14 <= 0:
        return 0.0, 0
    last = pivots[-1]
    price = float(last["price"])
    depth = abs(close - price) / atr14
    return depth, max(0, int(last["index"]))


def _hl_counts(bars: list[Candle]) -> tuple[int, int]:
    h_count = 0
    l_count = 0
    for previous, current in zip(bars, bars[1:]):
        if current.high > previous.high:
            h_count += 1
        if current.low < previous.low:
            l_count += 1
    return h_count, l_count


def _score_setup(**kwargs: Any) -> dict[str, str | int]:
    bull = 0
    bear = 0
    penalty = 0
    name_parts: list[str] = []
    patterns = set(kwargs.get("detected_patterns") or ())

    if kwargs["above_ema"]:
        bull += 12
    if kwargs["below_ema"]:
        bear += 12
    if kwargs["breakout"] == "up":
        bull += 22
        name_parts.append("range breakout up")
    if kwargs["breakout"] == "down":
        bear += 22
        name_parts.append("range breakout down")
    if kwargs["breakout_event"] == "breakout_up_retest":
        bull += 26
        name_parts.append("breakout retest up")
    if kwargs["breakout_event"] == "breakout_down_retest":
        bear += 26
        name_parts.append("breakout retest down")
    if kwargs["breakout_event"] == "failed_breakout_down":
        bull += 18
        name_parts.append("failed breakdown reversal")
    if kwargs["breakout_event"] == "failed_breakout_up":
        bear += 18
        name_parts.append("failed breakout reversal")

    bull_trend = int(kwargs["bull_trend_bars"])
    bear_trend = int(kwargs["bear_trend_bars"])
    bull += min(bull_trend * 9, 27)
    bear += min(bear_trend * 9, 27)
    bull += min(int(kwargs["h_count"]) * 3, 18)
    bear += min(int(kwargs["l_count"]) * 3, 18)

    if kwargs["swing_structure"] == "HH_HL":
        bull += 20
        name_parts.append("HH/HL structure")
    if kwargs["swing_structure"] == "LL_LH":
        bear += 20
        name_parts.append("LL/LH structure")
    if kwargs["cycle_position"] in {"spike", "micro_channel", "tight_channel"}:
        if kwargs["recent_direction"] == "bullish":
            bull += 18
            name_parts.append(str(kwargs["cycle_position"]))
        elif kwargs["recent_direction"] == "bearish":
            bear += 18
            name_parts.append(str(kwargs["cycle_position"]))
    if kwargs["trend_relationship"] == "aligned":
        if kwargs["recent_direction"] == "bullish":
            bull += 10
        elif kwargs["recent_direction"] == "bearish":
            bear += 10
    if "breakout_pullback" in patterns:
        bull += 10 if kwargs["recent_direction"] == "bullish" else 0
        bear += 10 if kwargs["recent_direction"] == "bearish" else 0
    if "failed_signal" in patterns:
        bull += 8 if kwargs["breakout_event"] == "failed_breakout_down" else 0
        bear += 8 if kwargs["breakout_event"] == "failed_breakout_up" else 0
    if "h2" in patterns:
        bull += 8
    if "l2" in patterns:
        bear += 8
    if "mtr" in patterns:
        bull += 12 if kwargs["breakout_event"] == "failed_breakout_down" else 0
        bear += 12 if kwargs["breakout_event"] == "failed_breakout_up" else 0
        name_parts.append("MTR/reversal attempt")
    if "final_flag" in patterns:
        penalty += 8
        name_parts.append("final flag risk")
    if "wedge" in patterns:
        if kwargs["recent_direction"] == "bullish":
            bull += 6
        elif kwargs["recent_direction"] == "bearish":
            bear += 6
    if any(item in patterns for item in {"ascending_triangle", "descending_triangle", "symmetrical_triangle"}):
        if kwargs["recent_direction"] == "bullish":
            bull += 5
        elif kwargs["recent_direction"] == "bearish":
            bear += 5
        name_parts.append("triangle compression")
    if kwargs["signal_bar_quality"] == "strong":
        if kwargs["recent_direction"] == "bullish":
            bull += 8
        elif kwargs["recent_direction"] == "bearish":
            bear += 8
    if kwargs["follow_through"] == "failed":
        penalty += 12
    if kwargs["climax_risk"] == "triggered":
        penalty += 15
    elif kwargs["climax_risk"] == "warning":
        penalty += 8
    if float(kwargs["range_position"]) >= 0.67:
        bull += 8
    if float(kwargs["range_position"]) <= 0.33:
        bear += 8

    pullback = float(kwargs["pullback_depth_atr"])
    if 0.3 <= pullback <= 2.2:
        bull += 6 if kwargs["above_ema"] else 0
        bear += 6 if kwargs["below_ema"] else 0

    if float(kwargs["barbwire_score"]) >= 0.6:
        penalty += 25
        name_parts.append("barbwire risk")
    if kwargs["cycle_position"] == "trading_range" and "breakout_pullback" not in patterns and "failed_signal" not in patterns:
        penalty += 12
    if kwargs["breakout"] == "none" and kwargs["breakout_event"] == "none":
        penalty += 8

    bull = max(0, bull - penalty)
    bear = max(0, bear - penalty)
    if bull >= bear and bull > 0:
        bias = "bullish"
        score = min(bull, 100)
    elif bear > 0:
        bias = "bearish"
        score = min(bear, 100)
    else:
        bias = "neutral"
        score = 0

    if not name_parts:
        name_parts.append("no qualified PA setup")
    return {
        "bias": bias,
        "score": int(score),
        "name": ", ".join(name_parts[:3]),
    }


def _feature_dict(features: PaFeatureSnapshot) -> dict[str, Any]:
    data = asdict(features)
    return {
        key: round(value, 6) if isinstance(value, float) else value
        for key, value in data.items()
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))

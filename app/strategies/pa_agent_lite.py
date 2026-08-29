from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any
from uuid import uuid4

from app.models import Candle, OpenEvaluateRequest, PositionEvaluateRequest, PositionSnapshot, TradeDecision
from app.services.ai_service import AiDecisionClient


@dataclass(frozen=True)
class PaSetupCandidate:
    code: str
    label: str
    direction: str
    context_score: int
    structure_score: int
    trigger_score: int
    space_score: int
    penalty_score: int
    total_score: int
    hard_blocks: tuple[str, ...]
    evidence: tuple[str, ...]


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
    setup_code: str
    setup_version: int
    setup_components: dict[str, int]
    long_score: int
    short_score: int
    score_margin: int
    candidate_valid: bool
    h_l_pattern: dict[str, Any]
    setup_candidates: tuple[dict[str, Any], ...]
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

        # Run cheap deterministic filters before AI so routine no-signal bars do not burn tokens.
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

        local_decision = _build_open_decision(request, deployment, features, direction)
        if local_decision.action not in {"BUY", "SELL"}:
            return local_decision

        ai_decision = self._evaluate_open_with_ai(
            request,
            deployment,
            features,
            expected_direction=direction,
            local_decision=local_decision,
        )
        if ai_decision is not None:
            return ai_decision

        return local_decision

    def evaluate_position(
        self,
        request: PositionEvaluateRequest,
        deployment: dict[str, Any],
    ) -> TradeDecision:
        features = _compute_features(request.candles)
        position = request.positions[0]
        config = deployment["config"]
        max_loss, take_profit = _position_money_limits(config, request)

        for checked_position in request.positions:
            if max_loss is not None and checked_position.profit <= -max_loss:
                return _close(request, self._decision_id(), checked_position.ticket, "Position reached configured maximum loss")
            if take_profit is not None and checked_position.profit >= take_profit:
                return _close(request, self._decision_id(), checked_position.ticket, "Position reached configured profit target")
        if features is None:
            return _position_hold(request, self._decision_id(), position.ticket, "PA Agent position strategy requires at least 30 candles")

        ai_decision = self._evaluate_position_with_ai(request, deployment, features)
        if ai_decision is not None:
            if ai_decision.action != "HOLD":
                return ai_decision
            target = next(
                (item for item in request.positions if item.ticket == ai_decision.position_ticket),
                position,
            )
            protective_stop = _atr_protective_stop(
                request,
                target,
                atr=features.atr14,
                usage=ai_decision.usage,
            )
            return protective_stop or ai_decision

        open_signal = _open_direction(features)
        if position.side == "BUY" and open_signal == "SELL":
            if _cooldown_blocks_close(request, position, features):
                return _position_hold(request, self._decision_id(), position.ticket, _cooldown_reason(request, position, features))
            return _close(request, self._decision_id(), position.ticket, "PA Agent detected opposite bearish price-action setup")
        if position.side == "SELL" and open_signal == "BUY":
            if _cooldown_blocks_close(request, position, features):
                return _position_hold(request, self._decision_id(), position.ticket, _cooldown_reason(request, position, features))
            return _close(request, self._decision_id(), position.ticket, "PA Agent detected opposite bullish price-action setup")

        protective_stop = _atr_protective_stop(request, position, atr=features.atr14)
        if protective_stop is not None:
            return protective_stop

        return _position_hold(request, self._decision_id(), position.ticket, "PA Agent position management conditions remain valid")

    def _evaluate_open_with_ai(
        self,
        request: OpenEvaluateRequest,
        deployment: dict[str, Any],
        features: PaFeatureSnapshot,
        *,
        expected_direction: str,
        local_decision: TradeDecision,
    ) -> TradeDecision | None:
        if self.ai_client is None:
            return None
        ai_features = _feature_dict(features)
        ai_features.update({
            "server_candidate_direction": expected_direction.lower(),
            "server_structure_sl": local_decision.sl,
            "server_min_risk_reward": 1.8,
        })
        result = self.ai_client.pa_open_decision(
            deployment=deployment,
            request_payload=request,
            features=ai_features,
        )
        if result is None:
            return None

        content = result.content
        reason = _ai_decision_message(content, "AI strategy returned hold")
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

        if direction.upper() != expected_direction:
            return _hold(
                request,
                self._decision_id(),
                "AI返回方向与服务端候选方向冲突，本次不开仓",
                confidence=min(confidence, 0.45),
                usage=result.usage,
            )

        config = deployment["config"]
        spread_price = abs(request.ask - request.bid)
        min_distance = max(spread_price * 30, features.atr14 * 1.2)
        ai_sl_distance = _positive_float(content.get("sl_distance_price"), min_distance)
        ai_tp_distance = _positive_float(content.get("tp_distance_price"), 0.0)

        if direction == "buy":
            entry = request.ask
            action = "BUY"
            structure_distance = max(entry - float(local_decision.sl or entry), 0.0)
            sl_distance = max(ai_sl_distance, structure_distance, min_distance)
            tp_distance = max(ai_tp_distance, sl_distance * 1.8)
            sl = entry - sl_distance
            tp = entry + tp_distance
        else:
            entry = request.bid
            action = "SELL"
            structure_distance = max(float(local_decision.sl or entry) - entry, 0.0)
            sl_distance = max(ai_sl_distance, structure_distance, min_distance)
            tp_distance = max(ai_tp_distance, sl_distance * 1.8)
            sl = entry + sl_distance
            tp = entry - tp_distance

        if config.get("position_size_mode") == "risk":
            lot = _position_size_lot(config, request, entry=entry, sl=sl)
        else:
            lot = _fixed_lot(config)

        if lot <= 0:
            return _hold(
                request,
                self._decision_id(),
                "最小交易手数超过当前风险额度，本次不开仓",
                confidence=min(confidence, 0.45),
                usage=result.usage,
            )

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
            metadata=_setup_metadata(features),
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
        reason = _ai_decision_message(content, "AI position strategy returned hold")
        confidence = _clamp(float(content.get("confidence") or 0.45), 0.0, 1.0)
        ticket = str(content.get("ticket") or request.positions[0].ticket)
        target = next((item for item in request.positions if item.ticket == ticket), request.positions[0])

        if action == "close":
            decision = _close(request, self._decision_id(), target.ticket, reason or "AI strategy requested close")
            decision.confidence = confidence
            decision.usage = result.usage
            return decision
        if action == "modify":
            sl, tp, changed = _validated_position_modification(
                request,
                target,
                sl=_optional_float(content.get("sl")),
                tp=_optional_float(content.get("tp")),
            )
            if not changed:
                return _position_hold(
                    request,
                    self._decision_id(),
                    target.ticket,
                    "AI返回的止损止盈未收紧或不符合当前持仓方向，保持原设置",
                    confidence=min(confidence, 0.45),
                    usage=result.usage,
                )
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
            config = deployment["config"]
            max_positions = max(1, int(config.get("max_positions") or 1))
            same_direction = all(item.side.lower() == direction for item in request.positions)
            if not bool(config.get("allow_add")) or len(request.positions) >= max_positions or not same_direction:
                return _position_hold(
                    request,
                    self._decision_id(),
                    target.ticket,
                    "当前策略配置不允许本次加仓，继续持有原仓位",
                    confidence=min(confidence, 0.45),
                    usage=result.usage,
                )
            spread_price = abs(request.ask - request.bid)
            min_distance = max(spread_price * 30, features.atr14 * 1.2)
            if direction == "buy":
                entry = request.ask
                sl = entry - min_distance
                tp = entry + min_distance * 1.8
                trade_action = "BUY"
            else:
                entry = request.bid
                sl = entry + min_distance
                tp = entry - min_distance * 1.8
                trade_action = "SELL"
            lot = _position_size_lot(config, request, entry=entry, sl=sl)
            if lot <= 0:
                return _position_hold(
                    request,
                    self._decision_id(),
                    target.ticket,
                    "最小交易手数超过当前风险额度，本次不加仓",
                    confidence=min(confidence, 0.45),
                    usage=result.usage,
                )
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


def _ai_decision_message(content: dict[str, Any], fallback: str) -> str:
    analysis = str(content.get("analysis") or "").strip()
    reason = str(content.get("reason") or "").strip()
    return (analysis or reason or fallback)[:800]


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


def _atr_protective_stop(
    request: PositionEvaluateRequest,
    position: PositionSnapshot,
    *,
    atr: float,
    usage: Any = None,
) -> TradeDecision | None:
    """Apply the official strategy's deterministic break-even/trailing stop.

    At 0.5 ATR favorable movement the stop moves to entry. Afterwards, whenever
    price is at least 1 ATR away from the protected stop, the stop catches up to
    0.5 ATR behind the executable market price. Stops are never loosened.
    """
    if atr <= 0:
        return None

    existing_sl = position.sl if position.sl is not None and position.sl > 0 else None
    tolerance = max(1e-9, abs(position.open_price) * 1e-10)

    if position.side == "BUY":
        current_price = request.bid
        if current_price - position.open_price < atr * 0.5:
            return None
        protected_sl = max(position.open_price, existing_sl or position.open_price)
        reason = "浮盈达到0.5 ATR，止损移动到保本价"
        if current_price - protected_sl >= atr:
            protected_sl = max(protected_sl, current_price - atr * 0.5)
            reason = "当前价格与止损相距达到1 ATR，止损跟进至距当前价格0.5 ATR"
        if existing_sl is not None and protected_sl <= existing_sl + tolerance:
            return None
    else:
        current_price = request.ask
        if position.open_price - current_price < atr * 0.5:
            return None
        protected_sl = min(position.open_price, existing_sl or position.open_price)
        reason = "浮盈达到0.5 ATR，止损移动到保本价"
        if protected_sl - current_price >= atr:
            protected_sl = min(protected_sl, current_price + atr * 0.5)
            reason = "当前价格与止损相距达到1 ATR，止损跟进至距当前价格0.5 ATR"
        if existing_sl is not None and protected_sl >= existing_sl - tolerance:
            return None

    decision = _modify(
        request,
        PaAgentLiteStrategy._decision_id(),
        position.ticket,
        reason,
        sl=protected_sl,
        tp=position.tp,
    )
    if usage is not None:
        decision.usage = usage
    return decision


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
    prior_window = window[:-1]
    prior_range_high = max(bar.high for bar in prior_window)
    prior_range_low = min(bar.low for bar in prior_window)
    breakout_event = _breakout_event(bars, prior_range_high, prior_range_low)
    pivots = _swing_pivots(bars[-40:])
    swing_structure = _swing_structure(pivots)
    support_1, resistance_1 = _nearest_levels(pivots, last_close)
    pullback_depth_atr, pullback_bars = _pullback_metrics(
        pivots,
        last_close,
        atr14,
        window_size=min(len(bars), 40),
    )
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
    long_attempt = _pullback_attempt_pattern(
        bars[-16:],
        direction="long",
        invalidation= support_1 or range_low,
    )
    short_attempt = _pullback_attempt_pattern(
        bars[-16:],
        direction="short",
        invalidation=resistance_1 or range_high,
    )
    setup_candidates = _build_setup_candidates(
        bars=bars,
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
        background_direction=background_direction,
        recent_direction=recent_direction,
        trend_relationship=trend_relationship,
        detected_patterns=detected_patterns,
        market_phase=market_phase,
        transition_risk=transition_risk,
        climax_risk=climax_risk,
        always_in=always_in,
        signal_bar_quality=signal_bar_quality,
        follow_through=follow_through,
        support=support_1,
        resistance=resistance_1,
        atr14=atr14,
        last_close=last_close,
        long_attempt=long_attempt,
        short_attempt=short_attempt,
    )
    setup = _select_setup_candidate(setup_candidates)

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
        setup_code=str(setup["code"]),
        setup_version=2,
        setup_components=dict(setup["components"]),
        long_score=int(setup["long_score"]),
        short_score=int(setup["short_score"]),
        score_margin=int(setup["margin"]),
        candidate_valid=bool(setup["valid"]),
        h_l_pattern=(long_attempt if setup["bias"] == "bullish" else short_attempt),
        setup_candidates=tuple(asdict(item) for item in setup_candidates),
        last_close=last_close,
    )


def _open_direction(features: PaFeatureSnapshot) -> str | None:
    if not features.candidate_valid:
        return None
    if features.setup_bias == "bullish":
        return "BUY"
    if features.setup_bias == "bearish":
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


def _pa_text(value: str) -> str:
    translations = {
        "no qualified PA setup": "未发现符合条件的价格行为形态",
        "bullish": "多头",
        "bearish": "空头",
        "neutral": "中性",
        "none": "无",
        "unknown": "未知",
        "middle": "中部",
        "upper": "上部",
        "lower": "下部",
        "trading_range": "震荡区间",
        "neutral_background": "中性背景",
        "mixed": "混合",
        "up": "向上",
        "down": "向下",
        "yes": "是",
        "no": "否",
    }
    return translations.get(value, value)


def _describe_hold(features: PaFeatureSnapshot) -> str:
    ema_text = f"{features.ema20:.5f}" if features.ema20 is not None else "n/a"
    patterns = ",".join(_pa_text(item) for item in features.detected_patterns) or "无"
    return (
        f"暂不开仓：当前信号未达到开仓条件，形态={_pa_text(features.setup_name)}，"
        f"信号评分={features.setup_score}，方向偏向={_pa_text(features.setup_bias)}，"
        f"突破={_pa_text(features.breakout)}/{_pa_text(features.breakout_event)}，"
        f"周期位置={_pa_text(features.cycle_position)}，识别形态={patterns}，"
        f"价格区域={_pa_text(features.zone)}，区间位置={features.range_position:.2f}，"
        f"EMA20={ema_text}，"
        f"趋势={_pa_text(features.background_direction)}/{_pa_text(features.recent_direction)}/{_pa_text(features.trend_relationship)}，"
        f"摆动结构={_pa_text(features.swing_structure)}，盘整度={features.barbwire_score:.2f}"
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
    if lot <= 0:
        return _hold(
            request,
            PaAgentLiteStrategy._decision_id(),
            "最小交易手数超过当前风险额度，本次不开仓",
            confidence=0.4,
        )
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
        metadata=_setup_metadata(features),
    )


def _position_size_lot(
    config: dict[str, Any],
    request: OpenEvaluateRequest | PositionEvaluateRequest,
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
    if raw_lot < min_lot:
        return 0.0
    return _normalize_volume(raw_lot, min_lot=min_lot, max_lot=max_lot, step=step)


def _fixed_lot(config: dict[str, Any]) -> float:
    return _positive_float(config.get("fixed_volume") or config.get("lot"), 0.01)


def _position_money_limits(
    config: dict[str, Any],
    request: PositionEvaluateRequest,
) -> tuple[float | None, float | None]:
    if config.get("position_size_mode") != "risk":
        # Fixed-volume strategies are protected by their price SL/TP. Do not
        # apply the old hidden 100/150 account-currency close thresholds.
        return None, None

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
    return number if math.isfinite(number) and number > 0 else fallback


def _first_positive_float(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        try:
            value = float(payload.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
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
    *,
    window_size: int,
) -> tuple[float, int]:
    if not pivots or atr14 <= 0:
        return 0.0, 0
    last = pivots[-1]
    price = float(last["price"])
    depth = abs(close - price) / atr14
    bars_since_pivot = max(0, window_size - 1 - int(last["index"]))
    return depth, bars_since_pivot


def _hl_counts(bars: list[Candle]) -> tuple[int, int]:
    h_count = 0
    l_count = 0
    for previous, current in zip(bars, bars[1:]):
        if current.high > previous.high:
            h_count += 1
        if current.low < previous.low:
            l_count += 1
    return h_count, l_count


def _pullback_attempt_pattern(
    bars: list[Candle],
    *,
    direction: str,
    invalidation: float | None,
) -> dict[str, Any]:
    """Detect H1/H2 or L1/L2 as distinct attempts inside a pullback."""
    if len(bars) < 4:
        return {
            "pattern": "none", "direction": direction, "triggered": False,
            "trigger_bar_index": None, "pullback_bars": 0, "structure_valid": False,
        }

    pullback_start: int | None = None
    for index in range(1, len(bars)):
        current, previous = bars[index], bars[index - 1]
        is_pullback = (
            current.low < previous.low or current.close < current.open
            if direction == "long"
            else current.high > previous.high or current.close > current.open
        )
        if is_pullback:
            pullback_start = index
            break
    if pullback_start is None:
        return {
            "pattern": "none", "direction": direction, "triggered": False,
            "trigger_bar_index": None, "pullback_bars": 0, "structure_valid": True,
        }

    attempts = 0
    armed = True
    latest_attempt_index: int | None = None
    for index in range(pullback_start + 1, len(bars)):
        current, previous = bars[index], bars[index - 1]
        if direction == "long":
            if current.low < previous.low:
                armed = True
            triggered = current.high > previous.high
        else:
            if current.high > previous.high:
                armed = True
            triggered = current.low < previous.low
        if triggered and armed:
            attempts += 1
            latest_attempt_index = index
            armed = False

    latest = bars[-1]
    structure_valid = True
    if invalidation is not None:
        structure_valid = latest.low > invalidation if direction == "long" else latest.high < invalidation
    is_latest_trigger = latest_attempt_index == len(bars) - 1
    prefix = "H" if direction == "long" else "L"
    pattern = f"{prefix}{min(attempts, 2)}" if attempts else "none"
    return {
        "pattern": pattern,
        "direction": direction,
        "triggered": bool(is_latest_trigger),
        "trigger_bar_index": -1 if is_latest_trigger else None,
        "pullback_bars": len(bars) - pullback_start,
        "structure_valid": structure_valid,
    }


def _candidate_space(
    *,
    direction: str,
    entry: float,
    support: float | None,
    resistance: float | None,
    atr14: float,
) -> tuple[int, tuple[str, ...]]:
    barrier = resistance if direction == "bullish" else support
    if barrier is None or atr14 <= 0:
        return 15, ()
    distance = barrier - entry if direction == "bullish" else entry - barrier
    if distance <= 0:
        return 15, ()
    distance_atr = distance / atr14
    if distance_atr >= 1.5:
        return 15, ()
    if distance_atr >= 1.2:
        return 12, ()
    if distance_atr >= 0.8:
        return 7, ()
    return 0, ("insufficient_space",)


def _setup_penalty(
    *,
    barbwire_score: float,
    trend_relationship: str,
    follow_through: str,
    climax_risk: str,
    transition_risk: str,
    breakout_setup: bool = False,
) -> int:
    penalty = 0
    if barbwire_score >= 0.6:
        penalty += 8 if breakout_setup else 18
    if trend_relationship == "conflict":
        penalty += 12
    if follow_through == "failed":
        penalty += 12
    if climax_risk == "triggered":
        penalty += 18
    elif climax_risk == "warning":
        penalty += 8
    if transition_risk == "high":
        penalty += 8
    return min(penalty, 40)


def _make_setup_candidate(
    *,
    code: str,
    label: str,
    direction: str,
    context: int,
    structure: int,
    trigger: int,
    space: int,
    penalty: int,
    hard_blocks: tuple[str, ...] = (),
    evidence: list[str] | tuple[str, ...] = (),
) -> PaSetupCandidate:
    context_score = min(25, max(0, context))
    structure_score = min(30, max(0, structure))
    trigger_score = min(30, max(0, trigger))
    space_score = min(15, max(0, space))
    penalty_score = min(40, max(0, penalty))
    total = max(0, min(100, context_score + structure_score + trigger_score + space_score - penalty_score))
    return PaSetupCandidate(
        code=code,
        label=label,
        direction=direction,
        context_score=context_score,
        structure_score=structure_score,
        trigger_score=trigger_score,
        space_score=space_score,
        penalty_score=penalty_score,
        total_score=total,
        hard_blocks=hard_blocks,
        evidence=tuple(evidence),
    )


def _build_setup_candidates(**kwargs: Any) -> list[PaSetupCandidate]:
    candidates: list[PaSetupCandidate] = []
    bars: list[Candle] = kwargs["bars"]
    latest = bars[-1]
    full_range = max(latest.high - latest.low, 0.0)
    body_ratio = abs(latest.close - latest.open) / full_range if full_range > 0 else 0.0
    close_position = (latest.close - latest.low) / full_range if full_range > 0 else 0.5

    def direction_context(direction: str) -> int:
        bullish = direction == "bullish"
        score = 0
        if kwargs["recent_direction"] == direction:
            score += 10
        if kwargs["trend_relationship"] == "aligned" and kwargs["background_direction"] == direction:
            score += 8
        if kwargs["swing_structure"] == ("HH_HL" if bullish else "LL_LH"):
            score += 7
        if kwargs["above_ema"] if bullish else kwargs["below_ema"]:
            score += 5
        return min(score, 25)

    def direction_space(direction: str) -> tuple[int, tuple[str, ...]]:
        return _candidate_space(
            direction=direction,
            entry=float(kwargs["last_close"]),
            support=kwargs.get("support"),
            resistance=kwargs.get("resistance"),
            atr14=float(kwargs["atr14"]),
        )

    breakout_direction = "bullish" if (
        kwargs["breakout"] == "up"
        or kwargs["breakout_event"] in {"breakout_up_retest", "range_breakout_up", "failed_breakout_down"}
    ) else "bearish" if (
        kwargs["breakout"] == "down"
        or kwargs["breakout_event"] in {"breakout_down_retest", "range_breakout_down", "failed_breakout_up"}
    ) else ""
    if breakout_direction:
        bullish = breakout_direction == "bullish"
        context = 8
        if kwargs["cycle_position"] in {"trading_range", "trending_tr"} or kwargs["barbwire_score"] >= 0.4:
            context += 9
        if kwargs["recent_direction"] in {breakout_direction, "neutral"}:
            context += 6
        event = str(kwargs["breakout_event"])
        is_retest = event in {"breakout_up_retest", "breakout_down_retest"}
        structure = 24 if is_retest else 18
        if kwargs["swing_structure"] == ("HH_HL" if bullish else "LL_LH"):
            structure += 6
        trigger = 0
        if body_ratio >= 0.5:
            trigger += 12
        if (bullish and close_position >= 0.7) or (not bullish and close_position <= 0.3):
            trigger += 10
        if kwargs["signal_bar_quality"] == "strong":
            trigger += 8
        elif kwargs["signal_bar_quality"] == "medium":
            trigger += 4
        space, blocks = direction_space(breakout_direction)
        penalty = _setup_penalty(
            barbwire_score=float(kwargs["barbwire_score"]),
            trend_relationship=str(kwargs["trend_relationship"]),
            follow_through=str(kwargs["follow_through"]),
            climax_risk=str(kwargs["climax_risk"]),
            transition_risk=str(kwargs["transition_risk"]),
            breakout_setup=True,
        )
        suffix = "long" if bullish else "short"
        candidates.append(_make_setup_candidate(
            code=f"breakout_{'retest_' if is_retest else ''}{suffix}",
            label=f"突破{'回踩' if is_retest else ''}{'做多' if bullish else '做空'}",
            direction=breakout_direction,
            context=context,
            structure=structure,
            trigger=trigger,
            space=space,
            penalty=penalty,
            hard_blocks=blocks,
            evidence=[str(kwargs["breakout"]), event, str(kwargs["signal_bar_quality"])],
        ))

    for direction in ("bullish", "bearish"):
        bullish = direction == "bullish"
        trend_bars = int(kwargs["bull_trend_bars"] if bullish else kwargs["bear_trend_bars"])
        direction_matches = kwargs["recent_direction"] == direction
        trend_structure = kwargs["swing_structure"] == ("HH_HL" if bullish else "LL_LH")
        if direction_matches and (trend_bars >= 2 or trend_structure or kwargs["always_in"] == ("long" if bullish else "short")):
            context = direction_context(direction)
            structure = min(30, min(trend_bars, 4) * 4)
            if kwargs["cycle_position"] in {"micro_channel", "tight_channel", "normal_channel", "spike"}:
                structure += 9
            if kwargs["always_in"] == ("long" if bullish else "short"):
                structure += 7
            structure = min(structure, 30)
            trigger = 0
            latest_matches = latest.close > latest.open if bullish else latest.close < latest.open
            if latest_matches:
                trigger += 12
            if kwargs["signal_bar_quality"] == "strong":
                trigger += 8
            elif kwargs["signal_bar_quality"] == "medium":
                trigger += 4
            if kwargs["follow_through"] == "yes":
                trigger += 10
            if (bullish and kwargs["breakout"] == "up") or (not bullish and kwargs["breakout"] == "down"):
                trigger += 5
            space, blocks = direction_space(direction)
            penalty = _setup_penalty(
                barbwire_score=float(kwargs["barbwire_score"]),
                trend_relationship=str(kwargs["trend_relationship"]),
                follow_through=str(kwargs["follow_through"]),
                climax_risk=str(kwargs["climax_risk"]),
                transition_risk=str(kwargs["transition_risk"]),
            )
            candidates.append(_make_setup_candidate(
                code=f"trend_continuation_{'long' if bullish else 'short'}",
                label=f"趋势延续{'做多' if bullish else '做空'}",
                direction=direction,
                context=context,
                structure=structure,
                trigger=trigger,
                space=space,
                penalty=penalty,
                hard_blocks=blocks,
                evidence=[str(kwargs["recent_direction"]), str(kwargs["cycle_position"]), f"bars={trend_bars}"],
            ))

        attempt = kwargs["long_attempt"] if bullish else kwargs["short_attempt"]
        if direction_matches and bool(attempt.get("triggered")):
            context = direction_context(direction)
            pullback = float(kwargs["pullback_depth_atr"])
            structure = 10 if bool(attempt.get("structure_valid")) else 0
            if 0.3 <= pullback <= 2.0:
                structure += 12
            elif 0 < pullback <= 2.5:
                structure += 6
            if kwargs["swing_structure"] == ("HH_HL" if bullish else "LL_LH"):
                structure += 8
            pattern = str(attempt.get("pattern") or "none")
            trigger = 22 if pattern in {"H2", "L2"} else 16
            latest_matches = latest.close > latest.open if bullish else latest.close < latest.open
            if latest_matches:
                trigger += 8
            if kwargs["signal_bar_quality"] == "strong":
                trigger += 6
            space, blocks = direction_space(direction)
            if not bool(attempt.get("structure_valid")):
                blocks = (*blocks, "pullback_structure_broken")
            penalty = _setup_penalty(
                barbwire_score=float(kwargs["barbwire_score"]),
                trend_relationship=str(kwargs["trend_relationship"]),
                follow_through=str(kwargs["follow_through"]),
                climax_risk=str(kwargs["climax_risk"]),
                transition_risk=str(kwargs["transition_risk"]),
            )
            candidates.append(_make_setup_candidate(
                code=f"pullback_{pattern.lower()}_{'long' if bullish else 'short'}",
                label=f"回调{pattern}{'做多' if bullish else '做空'}",
                direction=direction,
                context=context,
                structure=structure,
                trigger=trigger,
                space=space,
                penalty=penalty,
                hard_blocks=tuple(dict.fromkeys(blocks)),
                evidence=[pattern, f"depth={pullback:.2f}ATR", str(kwargs["swing_structure"])],
            ))

    return candidates


def _select_setup_candidate(candidates: list[PaSetupCandidate]) -> dict[str, Any]:
    eligible = [item for item in candidates if not item.hard_blocks]
    longs = [item for item in eligible if item.direction == "bullish"]
    shorts = [item for item in eligible if item.direction == "bearish"]
    long_score = max((item.total_score for item in longs), default=0)
    short_score = max((item.total_score for item in shorts), default=0)
    best = max(eligible, key=lambda item: item.total_score, default=None)
    margin = abs(long_score - short_score)
    valid = bool(best and best.total_score >= 70 and margin >= 12)
    if best is None:
        return {
            "bias": "neutral", "score": 0, "name": "no qualified PA setup", "code": "none",
            "components": {}, "long_score": 0, "short_score": 0, "margin": 0, "valid": False,
        }
    return {
        "bias": best.direction,
        "score": best.total_score,
        "name": best.label,
        "code": best.code,
        "components": {
            "context": best.context_score,
            "structure": best.structure_score,
            "trigger": best.trigger_score,
            "space": best.space_score,
            "penalty": best.penalty_score,
        },
        "long_score": long_score,
        "short_score": short_score,
        "margin": margin,
        "valid": valid,
    }


def _feature_dict(features: PaFeatureSnapshot) -> dict[str, Any]:
    data = asdict(features)
    return {
        key: round(value, 6) if isinstance(value, float) else value
        for key, value in data.items()
    }


def _setup_metadata(features: PaFeatureSnapshot) -> dict[str, Any]:
    return {
        "setup_code": features.setup_code,
        "setup_name": features.setup_name,
        "setup_version": features.setup_version,
        "direction": "buy" if features.setup_bias == "bullish" else "sell",
        "score": features.setup_score,
        "long_score": features.long_score,
        "short_score": features.short_score,
        "score_margin": features.score_margin,
        "components": dict(features.setup_components),
    }


def _validated_position_modification(
    request: PositionEvaluateRequest,
    position: PositionSnapshot,
    *,
    sl: float | None,
    tp: float | None,
) -> tuple[float | None, float | None, bool]:
    """Accept only direction-valid changes and never loosen an existing stop."""
    existing_sl = position.sl if position.sl is not None and position.sl > 0 else None
    existing_tp = position.tp if position.tp is not None and position.tp > 0 else None
    tolerance = max(1e-9, abs(position.open_price) * 1e-10)
    valid_sl = existing_sl
    valid_tp = existing_tp
    changed = False

    if sl is not None:
        if position.side == "BUY":
            tightens = existing_sl is None or sl > existing_sl + tolerance
            direction_valid = sl < request.bid - tolerance
        else:
            tightens = existing_sl is None or sl < existing_sl - tolerance
            direction_valid = sl > request.ask + tolerance
        if tightens and direction_valid:
            valid_sl = sl
            changed = True

    if tp is not None:
        direction_valid = tp > request.ask + tolerance if position.side == "BUY" else tp < request.bid - tolerance
        differs = existing_tp is None or abs(tp - existing_tp) > tolerance
        if direction_valid and differs:
            valid_tp = tp
            changed = True

    return valid_sl, valid_tp, changed


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))

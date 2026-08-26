from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any
from uuid import uuid4

from app.models import OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision, UsageSummary
from app.services.ai_service import AiDecisionClient
from app.strategies.pa_agent_lite import _position_size_lot


class CustomAiStrategy:
    code = "CUSTOM_AI_V1"

    def __init__(self, ai_client: AiDecisionClient) -> None:
        self.ai_client = ai_client

    def evaluate_open(self, request: OpenEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        if len(request.candles) < 10:
            return self._hold(request, "K线数量不足，自定义策略至少需要10根已收盘K线")
        result = self.ai_client.custom_open_decision(deployment=deployment, request_payload=request)
        if result is None:
            return self._hold(request, "AI模型未配置，暂不开仓")
        content = result.content
        direction = str(content.get("direction") or "").strip().lower()
        should_open = _truthy_bool(content.get("should_open")) and direction in {"buy", "sell"}
        confidence = _confidence(content.get("confidence"))
        reason = _reason(content, "自定义策略条件未全部满足，继续观望")
        if not should_open:
            return self._hold(request, reason, confidence=confidence, usage=result.usage)

        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        open_logic = str(config.get("open_logic") or config.get("open_prompt_template") or "")
        entry = request.ask if direction == "buy" else request.bid
        sl = _optional_price(content.get("sl"))
        tp = _optional_price(content.get("tp"))
        rule_based_sl = _recent_candle_extreme_stop(open_logic, direction, request.candles)
        if rule_based_sl is not None:
            # This value is deterministic from the user's rule and submitted candles.
            # Always prefer it over an AI-calculated absolute price.
            sl = rule_based_sl
            reason = (
                f"{str(content.get('reason') or '满足用户开仓条件').strip()}；"
                f"止损已按用户规则和K线数据重算为{sl:g}"
            )
        elif sl is None:
            distance = _positive_float(content.get("sl_distance_price"))
            if distance:
                sl = entry - distance if direction == "buy" else entry + distance
        if sl is None and _rule_requires_stop(open_logic):
            return self._hold(
                request,
                "策略要求设置止损，但AI未返回可计算的有效止损价，已阻止无止损开仓",
                confidence=0.2,
                usage=result.usage,
            )
        if tp is None:
            distance = _positive_float(content.get("tp_distance_price"))
            if distance:
                tp = entry + distance if direction == "buy" else entry - distance
        if sl is not None and not _valid_stop(direction, entry, sl):
            return self._hold(request, "AI返回的止损价方向无效，已阻止开仓", confidence=0.2, usage=result.usage)
        if tp is not None and not _valid_target(direction, entry, tp):
            tp = None
        lot = _position_size_lot(config, request, entry=entry, sl=sl)
        return TradeDecision(
            decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
            action="BUY" if direction == "buy" else "SELL", symbol=request.symbol,
            confidence=confidence, reason=reason, expires_at=_expires_at(), lot=lot,
            entry=entry, sl=sl, tp=tp, usage=result.usage,
        )

    def evaluate_position(self, request: PositionEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        target = request.positions[0]
        if len(request.candles) < 10:
            return self._position_hold(request, target.ticket, "K线数量不足，暂不执行持仓操作")
        result = self.ai_client.custom_position_decision(deployment=deployment, request_payload=request)
        if result is None:
            return self._position_hold(request, target.ticket, "AI模型未配置，继续持有")
        content = result.content
        ticket = str(content.get("ticket") or target.ticket)
        target = next((item for item in request.positions if item.ticket == ticket), target)
        action = str(content.get("action") or "hold").strip().lower()
        confidence = _confidence(content.get("confidence"))
        reason = _reason(content, "自定义风控条件未触发，继续持有")
        if action == "close":
            close_scope = str(content.get("close_scope") or "").strip().lower()
            requested_volume = _positive_float(content.get("volume"))
            if close_scope == "partial" and requested_volume is None:
                return self._position_hold(request, target.ticket, "部分平仓未明确手数或比例，未执行操作", usage=result.usage)
            if close_scope == "partial" or requested_volume is not None:
                volume = _normalized_partial_close_volume(requested_volume, target.volume, request.symbol_info or {})
                if volume is None:
                    return self._position_hold(request, target.ticket, "部分平仓手数不符合交易品种限制，未执行操作", usage=result.usage)
            else:
                volume = target.volume
            return TradeDecision(
                decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
                action="CLOSE", symbol=request.symbol, confidence=confidence, reason=reason,
                expires_at=_expires_at(), position_ticket=target.ticket,
                volume=volume, usage=result.usage,
            )
        if action == "modify":
            sl = _optional_price(content.get("sl"))
            tp = _optional_price(content.get("tp"))
            if sl is None and tp is None:
                return self._position_hold(request, target.ticket, "未返回有效止盈止损价，继续持有", usage=result.usage)
            return TradeDecision(
                decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
                action="MODIFY_SL", symbol=request.symbol, confidence=confidence, reason=reason,
                expires_at=_expires_at(), position_ticket=target.ticket, sl=sl, tp=tp, usage=result.usage,
            )
        if action == "add":
            config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
            if not bool(config.get("allow_add")):
                return self._position_hold(request, target.ticket, "策略未开启加仓权限，继续持有", usage=result.usage)
            if len(request.positions) >= int(config.get("max_positions") or 1):
                return self._position_hold(request, target.ticket, "已达到最大持仓数量，未执行加仓", usage=result.usage)
            direction = str(content.get("direction") or target.side).strip().lower()
            direction = "buy" if direction == "buy" else "sell" if direction == "sell" else target.side.lower()
            entry = request.ask if direction == "buy" else request.bid
            sl = _optional_price(content.get("sl"))
            if sl is not None and not _valid_stop(direction, entry, sl):
                return self._position_hold(request, target.ticket, "AI返回的加仓止损价方向无效，未执行加仓", usage=result.usage)
            requested_lot = _positive_float(content.get("lot"))
            if requested_lot is not None:
                lot = _normalized_order_volume(requested_lot, request.symbol_info or {})
            else:
                if config.get("position_size_mode") == "risk" and sl is None:
                    return self._position_hold(request, target.ticket, "缺少有效止损价，无法计算加仓手数", usage=result.usage)
                lot = _position_size_lot(config, request, entry=entry, sl=sl)
            return TradeDecision(
                decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
                action="BUY" if direction == "buy" else "SELL", symbol=request.symbol,
                confidence=confidence, reason=reason, expires_at=_expires_at(),
                lot=lot, entry=entry, sl=sl,
                tp=_optional_price(content.get("tp")), usage=result.usage,
            )
        return self._position_hold(request, target.ticket, reason, confidence=confidence, usage=result.usage)

    @staticmethod
    def _hold(
        request: OpenEvaluateRequest, reason: str, *, confidence: float = 0.2,
        usage: UsageSummary | None = None,
    ) -> TradeDecision:
        return TradeDecision(
            decision_id=_decision_id(), request_id=request.request_id, status="HOLD", action="HOLD",
            symbol=request.symbol, confidence=confidence, reason=reason, expires_at=_expires_at(),
            usage=usage or UsageSummary(),
        )

    @staticmethod
    def _position_hold(
        request: PositionEvaluateRequest, ticket: str, reason: str, *, confidence: float = 0.4,
        usage: UsageSummary | None = None,
    ) -> TradeDecision:
        return TradeDecision(
            decision_id=_decision_id(), request_id=request.request_id, status="HOLD", action="HOLD",
            symbol=request.symbol, confidence=confidence, reason=reason, expires_at=_expires_at(),
            position_ticket=ticket, usage=usage or UsageSummary(),
        )


def _decision_id() -> str:
    return f"dec_{uuid4().hex}"


def _expires_at() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=60)


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.4


def _reason(content: dict[str, Any], fallback: str) -> str:
    return str(content.get("analysis") or content.get("reason") or fallback).strip()[:800]


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_price(value: Any) -> float | None:
    return _positive_float(value)


def _valid_stop(direction: str, entry: float, price: float) -> bool:
    return price < entry if direction == "buy" else price > entry


def _valid_target(direction: str, entry: float, price: float) -> bool:
    return price > entry if direction == "buy" else price < entry


def _recent_candle_extreme_stop(
    rule: str,
    direction: str,
    candles: list[Any],
) -> float | None:
    if not rule or not candles:
        return None
    target_words = r"(?:最低(?:价|点)?|低点)" if direction == "buy" else r"(?:最高(?:价|点)?|高点)"
    pattern = re.compile(
        rf"(\d{{1,4}})\s*根\s*[kKＫｋ]\s*线[^。；;\n]{{0,40}}?{target_words}",
        re.IGNORECASE,
    )
    count = None
    for segment in re.split(r"[。；;\n]", rule):
        if "止损" not in segment and not re.search(r"\b(?:stop\s*loss|sl)\b", segment, re.IGNORECASE):
            continue
        match = pattern.search(segment)
        if match:
            count = int(match.group(1))
            break
    if count is None or count < 1 or len(candles) < count:
        return None
    recent = sorted(candles, key=lambda candle: candle.timestamp)[-count:]
    if direction == "buy":
        return min(float(candle.low) for candle in recent)
    return max(float(candle.high) for candle in recent)


def _rule_requires_stop(rule: str) -> bool:
    return bool(re.search(r"止损|\b(?:stop\s*loss|sl)\b", rule or "", re.IGNORECASE))


def _truthy_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _normalized_order_volume(volume: float, symbol_info: dict[str, Any]) -> float:
    minimum, maximum, step = _volume_limits(symbol_info, fallback_max=volume)
    capped = min(max(volume, minimum), maximum)
    steps = int((capped + step * 1e-9) / step)
    return round(max(minimum, steps * step), 8)


def _normalized_partial_close_volume(
    requested: float | None,
    current: float,
    symbol_info: dict[str, Any],
) -> float | None:
    if requested is None or requested <= 0 or requested >= current:
        return None
    minimum, _, step = _volume_limits(symbol_info, fallback_max=current)
    if current < minimum * 2:
        return None
    maximum_partial = current - minimum
    capped = min(requested, maximum_partial)
    steps = int((capped + step * 1e-9) / step)
    normalized = round(steps * step, 8)
    if normalized < minimum or current - normalized < minimum - 1e-9:
        return None
    return normalized


def _volume_limits(symbol_info: dict[str, Any], *, fallback_max: float) -> tuple[float, float, float]:
    minimum = _first_positive(symbol_info, "volume_min", "lots_min", "min_lot", "minLot") or 0.01
    maximum = _first_positive(symbol_info, "volume_max", "lots_max", "max_lot", "maxLot") or max(fallback_max, minimum)
    step = _first_positive(symbol_info, "volume_step", "lots_step", "lot_step", "lotStep") or 0.01
    return minimum, maximum, step


def _first_positive(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _positive_float(payload.get(key))
        if value is not None:
            return value
    return None

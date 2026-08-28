from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision, UsageSummary
from app.services.ai_service import AiDecisionClient
from app.services.custom_indicators import calculate_indicator_payload
from app.services.custom_rule_engine import (
    RulePlanError,
    evaluate_open_rule_plan,
    evaluate_position_rule_plan,
    normalize_rule_plan,
)
from app.strategies.pa_agent_lite import _position_size_lot


class CustomAiStrategy:
    code = "CUSTOM_AI_V1"

    def __init__(self, ai_client: AiDecisionClient) -> None:
        self.ai_client = ai_client

    def evaluate_open(self, request: OpenEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        data_type = str(config.get("open_data_type") or request.data_type or "kline")
        if data_type in {"kline", "both"} and len(request.candles) < 10:
            return self._hold(request, "K线数量不足，自定义策略至少需要10根已收盘K线")
        if data_type in {"screenshot", "both"} and not request.screenshot_data_url:
            return self._hold(request, "EA未提供策略所需截图，本次分析已停止")
        plan = config.get("open_rule_plan") if isinstance(config.get("open_rule_plan"), dict) else {}
        plan = _runtime_rule_plan(plan, stage="open", config=config, data_type=data_type)
        engine_result = None
        if str(plan.get("mode") or "") == "deterministic":
            indicators = calculate_indicator_payload(
                request.candles,
                list(config.get("open_indicators") or []),
                output_count=max(20, min(int(config.get("indicator_output_count") or 100), 300)),
            )
            engine_result = evaluate_open_rule_plan(plan, request=request, indicators=indicators)
            result = self.ai_client.custom_rule_explanation(
                deployment=deployment,
                endpoint="open",
                calculated_result=engine_result.explanation_payload(stage="open"),
            )
        else:
            result = self.ai_client.custom_open_decision(deployment=deployment, request_payload=request)
        if result is None:
            return self._hold(request, "AI模型未配置，暂不开仓")
        content = result.content
        direction = engine_result.direction if engine_result is not None else str(content.get("direction") or "").strip().lower()
        should_open = (
            engine_result is not None and engine_result.action == "open" and direction in {"buy", "sell"}
        ) or (
            engine_result is None and _truthy_bool(content.get("should_open")) and direction in {"buy", "sell"}
        )
        confidence = 1.0 if engine_result is not None else _confidence(content.get("confidence"))
        reason = _reason(content, "自定义策略条件未全部满足，继续观望")
        if not should_open:
            return self._hold(request, reason, confidence=confidence, usage=result.usage)

        entry = request.ask if direction == "buy" else request.bid
        sl = engine_result.sl if engine_result is not None else _optional_price(content.get("sl"))
        tp = engine_result.tp if engine_result is not None else _optional_price(content.get("tp"))
        if sl is None:
            distance = _positive_float(content.get("sl_distance_price"))
            if distance:
                sl = entry - distance if direction == "buy" else entry + distance
        if tp is None:
            distance = _positive_float(content.get("tp_distance_price"))
            if distance:
                tp = entry + distance if direction == "buy" else entry - distance
        if sl is not None and not _valid_stop(direction, entry, sl):
            source = "服务端计算" if engine_result is not None else "AI返回"
            return self._hold(request, f"{source}的止损价方向无效，已阻止开仓", confidence=0.2, usage=result.usage)
        if tp is not None and not _valid_target(direction, entry, tp):
            return self._hold(request, "AI返回的止盈价方向无效，已阻止开仓", confidence=0.2, usage=result.usage)
        lot = _position_size_lot(config, request, entry=entry, sl=sl)
        return TradeDecision(
            decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
            action="BUY" if direction == "buy" else "SELL", symbol=request.symbol,
            confidence=confidence, reason=reason, expires_at=_expires_at(), lot=lot,
            entry=entry, sl=sl, tp=tp, usage=result.usage,
        )

    def evaluate_position(self, request: PositionEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        target = request.positions[0]
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        data_type = str(config.get("position_data_type") or request.data_type or "kline")
        if data_type in {"kline", "both"} and len(request.candles) < 10:
            return self._position_hold(request, target.ticket, "K线数量不足，暂不执行持仓操作")
        if data_type in {"screenshot", "both"} and not request.screenshot_data_url:
            return self._position_hold(request, target.ticket, "EA未提供策略所需截图，本次分析已停止")
        plan = config.get("position_rule_plan") if isinstance(config.get("position_rule_plan"), dict) else {}
        plan = _runtime_rule_plan(plan, stage="position", config=config, data_type=data_type)
        engine_result = None
        if str(plan.get("mode") or "") == "deterministic":
            indicators = calculate_indicator_payload(
                request.candles,
                list(config.get("position_indicators") or []),
                output_count=max(20, min(int(config.get("indicator_output_count") or 100), 300)),
            )
            engine_result = evaluate_position_rule_plan(plan, request=request, indicators=indicators, position=target)
            result = self.ai_client.custom_rule_explanation(
                deployment=deployment,
                endpoint="position",
                calculated_result=engine_result.explanation_payload(stage="position"),
            )
        else:
            result = self.ai_client.custom_position_decision(deployment=deployment, request_payload=request)
        if result is None:
            return self._position_hold(request, target.ticket, "AI模型未配置，继续持有")
        content = (
            {
                "action": engine_result.action,
                "ticket": engine_result.ticket,
                "direction": engine_result.direction,
                "lot": engine_result.lot,
                "close_scope": engine_result.close_scope,
                "volume": engine_result.volume,
                "sl": engine_result.sl,
                "tp": engine_result.tp,
                "confidence": 1,
                "reason": result.content.get("reason"),
                "analysis": result.content.get("analysis"),
            }
            if engine_result is not None
            else result.content
        )
        ticket = str(content.get("ticket") or target.ticket)
        target = next((item for item in request.positions if item.ticket == ticket), target)
        action = str(content.get("action") or "hold").strip().lower()
        confidence = 1.0 if engine_result is not None else _confidence(content.get("confidence"))
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
            requested_sl = _optional_price(content.get("sl"))
            requested_tp = _optional_price(content.get("tp"))
            if requested_sl is None and requested_tp is None:
                return self._position_hold(request, target.ticket, "未返回有效止盈止损价，继续持有", usage=result.usage)
            # MT5 modifies SL and TP together. Preserve the side that the user
            # rule did not request changing; do not impose a tightening policy.
            sl = requested_sl if requested_sl is not None else target.sl
            tp = requested_tp if requested_tp is not None else target.tp
            return TradeDecision(
                decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
                action="MODIFY_SL", symbol=request.symbol, confidence=confidence, reason=reason,
                expires_at=_expires_at(), position_ticket=target.ticket, sl=sl, tp=tp, usage=result.usage,
            )
        if action == "add":
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


def _runtime_rule_plan(
    plan: dict[str, Any],
    *,
    stage: str,
    config: dict[str, Any],
    data_type: str,
) -> dict[str, Any]:
    # A deterministic plan cannot inspect an attached chart image. Mixed
    # kline+screenshot stages must stay in AI mode so visual rules are not
    # silently skipped while only the numeric subset is executed.
    if data_type in {"screenshot", "both"}:
        return {"version": 1, "mode": "ai", "rules": []}
    specs = config.get(f"{stage}_indicators")
    aliases = {
        str(item.get("alias") or item.get("name") or "").strip().lower()
        for item in (specs if isinstance(specs, list) else [])
        if isinstance(item, dict)
    }
    try:
        return normalize_rule_plan(plan, stage=stage, indicator_aliases=aliases)
    except RulePlanError:
        return {"version": 1, "mode": "ai", "rules": []}


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
    # Show the bounded, auditable decision explanation on the EA panel. The
    # short reason remains useful in the structured AI result, but by itself it
    # does not contain enough evidence for users to verify a custom rule.
    return str(content.get("analysis") or content.get("reason") or fallback).strip()[:500]


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

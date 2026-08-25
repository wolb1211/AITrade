from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision, UsageSummary
from app.services.ai_service import AiDecisionClient
from app.strategies.pa_agent_lite import _fixed_lot, _position_size_lot


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

        entry = request.ask if direction == "buy" else request.bid
        sl = _optional_price(content.get("sl"))
        tp = _optional_price(content.get("tp"))
        if sl is None:
            distance = _positive_float(content.get("sl_distance_price"))
            if distance:
                sl = entry - distance if direction == "buy" else entry + distance
        if tp is None:
            distance = _positive_float(content.get("tp_distance_price"))
            if distance:
                tp = entry + distance if direction == "buy" else entry - distance
        if sl is not None and not _valid_stop(direction, entry, sl):
            return self._hold(request, "AI返回的止损价方向无效，已阻止开仓", confidence=0.2, usage=result.usage)
        if tp is not None and not _valid_target(direction, entry, tp):
            tp = None
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
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
            volume = _positive_float(content.get("volume"))
            return TradeDecision(
                decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
                action="CLOSE", symbol=request.symbol, confidence=confidence, reason=reason,
                expires_at=_expires_at(), position_ticket=target.ticket,
                volume=min(volume, target.volume) if volume else target.volume, usage=result.usage,
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
            return TradeDecision(
                decision_id=_decision_id(), request_id=request.request_id, status="APPROVED",
                action="BUY" if direction == "buy" else "SELL", symbol=request.symbol,
                confidence=confidence, reason=reason, expires_at=_expires_at(),
                lot=_fixed_lot(config), entry=entry, sl=_optional_price(content.get("sl")),
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


def _truthy_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}

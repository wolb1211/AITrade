from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision


class TurtleTrendStrategy:
    """Deterministic current-timeframe Turtle/GL Trend strategy.

    The core signal and risk prices are calculated locally.  AI integration is
    intentionally kept outside this first version so an AI response cannot
    alter the breakout, unit size, or protective stop.
    """

    code = "GL_TREND_V1"

    def evaluate_open(self, request: OpenEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        period = _positive_int(config.get("entry_period"), 20)
        atr_period = _positive_int(config.get("atr_period"), 20)
        candles = _ordered(request.candles)
        if len(candles) < max(period + 1, atr_period + 2):
            return _hold_open(request, "GL趋势策略需要更多已收盘K线")
        atr = _atr(candles, atr_period)
        window = candles[-period - 1:-1]
        close = candles[-1].close
        upper = max(item.high for item in window)
        lower = min(item.low for item in window)
        direction = "buy" if close > upper else "sell" if close < lower else ""
        if not direction:
            return _hold_open(request, "当前未突破唐奇安通道")
        entry = request.ask if direction == "buy" else request.bid
        sl = entry - 2 * atr if direction == "buy" else entry + 2 * atr
        lot = _unit_lot(request, config, atr)
        return TradeDecision(
            decision_id=_id(), request_id=request.request_id, status="APPROVED",
            action="BUY" if direction == "buy" else "SELL", symbol=request.symbol,
            confidence=1.0, reason=f"突破前{period}根K线通道，按GL趋势规则执行",
            expires_at=_expires(), lot=lot, entry=entry, sl=sl, tp=None,
        )

    def evaluate_position(self, request: PositionEvaluateRequest, deployment: dict[str, Any]) -> TradeDecision:
        position = request.positions[0]
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        period = _positive_int(config.get("exit_period"), 10)
        atr_period = _positive_int(config.get("atr_period"), 20)
        candles = _ordered(request.candles)
        if len(candles) < max(period + 1, atr_period + 2):
            return _hold_position(request, position.ticket, "GL趋势策略需要更多已收盘K线")
        atr = _atr(candles, atr_period)
        window = candles[-period - 1:-1]
        price = position.current_price
        if position.side == "BUY" and price < min(item.low for item in window):
            return _close(request, position.ticket, "跌破出场通道")
        if position.side == "SELL" and price > max(item.high for item in window):
            return _close(request, position.ticket, "突破出场通道")
        if position.side == "BUY" and price <= position.open_price - 2 * atr:
            return _close(request, position.ticket, "达到2 ATR保护止损")
        if position.side == "SELL" and price >= position.open_price + 2 * atr:
            return _close(request, position.ticket, "达到2 ATR保护止损")
        return _hold_position(request, position.ticket, "GL趋势策略持仓条件未触发离场")


def _ordered(candles):
    return sorted(candles, key=lambda item: item.timestamp)


def _atr(candles, period: int) -> float:
    sample = candles[-period - 1:]
    ranges = []
    for index in range(1, len(sample)):
        current, previous = sample[index], sample[index - 1]
        ranges.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))
    return max(sum(ranges) / max(len(ranges), 1), 1e-9)


def _unit_lot(request, config: dict[str, Any], atr: float) -> float:
    risk = float(config.get("risk_fraction") or 0.01)
    contract = float(config.get("contract_size") or 1)
    balance = float(request.equity or request.balance or 0)
    raw = balance * risk / max(atr * contract, 1e-9)
    step = float(config.get("lot_step") or 0.01)
    minimum = float(config.get("min_lot") or step)
    return max(minimum, round(raw / step) * step)


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def _id() -> str:
    return f"dec_{uuid4().hex}"


def _expires() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=60)


def _hold_open(request, reason):
    return TradeDecision(decision_id=_id(), request_id=request.request_id, status="HOLD", action="HOLD", symbol=request.symbol, confidence=0.0, reason=reason, expires_at=_expires())


def _hold_position(request, ticket, reason):
    return TradeDecision(decision_id=_id(), request_id=request.request_id, status="HOLD", action="HOLD", symbol=request.symbol, position_ticket=ticket, confidence=0.0, reason=reason, expires_at=_expires())


def _close(request, ticket, reason):
    return TradeDecision(decision_id=_id(), request_id=request.request_id, status="APPROVED", action="CLOSE", symbol=request.symbol, position_ticket=ticket, confidence=1.0, reason=reason, expires_at=_expires())

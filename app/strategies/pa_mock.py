from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.models import (
    OpenEvaluateRequest,
    PositionEvaluateRequest,
    TradeDecision,
)


class PaMockStrategy:
    """Deterministic placeholder used to connect API, EA, and UI."""

    code = "PA_MOCK_V1"

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
        if len(request.candles) < 3:
            return TradeDecision(
                decision_id=self._decision_id(),
                request_id=request.request_id,
                status="HOLD",
                action="HOLD",
                symbol=request.symbol,
                confidence=0.0,
                reason="PA Base requires at least three closed candles",
                expires_at=self._expires_at(),
            )

        closes = [bar.close for bar in request.candles[-3:]]
        if closes[0] < closes[1] < closes[2]:
            action = "BUY"
            entry = request.ask
        elif closes[0] > closes[1] > closes[2]:
            action = "SELL"
            entry = request.bid
        else:
            return TradeDecision(
                decision_id=self._decision_id(),
                request_id=request.request_id,
                status="HOLD",
                action="HOLD",
                symbol=request.symbol,
                confidence=0.25,
                reason="PA Base found no directional candidate",
                expires_at=self._expires_at(),
            )

        config = deployment["config"]
        lot = float(config.get("lot", 0.01))
        spread_price = abs(request.ask - request.bid)
        sl_distance = max(float(config.get("sl_distance", 5.0)), spread_price * 3)
        tp_distance = max(float(config.get("tp_distance", 8.0)), spread_price * 5)
        if action == "BUY":
            sl = entry - sl_distance
            tp = entry + tp_distance
        else:
            sl = entry + sl_distance
            tp = entry - tp_distance

        return TradeDecision(
            decision_id=self._decision_id(),
            request_id=request.request_id,
            status="APPROVED",
            action=action,
            symbol=request.symbol,
            confidence=0.65,
            reason="PA Base deterministic trend candidate",
            expires_at=self._expires_at(),
            lot=lot,
            entry=entry,
            sl=sl,
            tp=tp,
        )

    def evaluate_position(
        self,
        request: PositionEvaluateRequest,
        deployment: dict[str, Any],
    ) -> TradeDecision:
        position = request.positions[0]
        config = deployment["config"]
        max_loss = abs(float(config.get("max_loss_per_position", 100.0)))
        take_profit = abs(float(config.get("take_profit_per_position", 150.0)))

        reason: str | None = None
        if position.profit <= -max_loss:
            reason = "Position reached configured maximum loss"
        elif position.profit >= take_profit:
            reason = "Position reached configured profit target"
        elif len(request.candles) >= 3:
            closes = [bar.close for bar in request.candles[-3:]]
            reversed_down = closes[0] > closes[1] > closes[2]
            reversed_up = closes[0] < closes[1] < closes[2]
            if position.side == "BUY" and reversed_down:
                reason = "PA Base detected bearish reversal"
            elif position.side == "SELL" and reversed_up:
                reason = "PA Base detected bullish reversal"

        if reason:
            return TradeDecision(
                decision_id=self._decision_id(),
                request_id=request.request_id,
                status="APPROVED",
                action="CLOSE",
                symbol=request.symbol,
                confidence=0.7,
                reason=reason,
                expires_at=self._expires_at(),
                position_ticket=position.ticket,
            )

        return TradeDecision(
            decision_id=self._decision_id(),
            request_id=request.request_id,
            status="HOLD",
            action="HOLD",
            symbol=request.symbol,
            confidence=0.5,
            reason="Position management conditions remain valid",
            expires_at=self._expires_at(),
            position_ticket=position.ticket,
        )

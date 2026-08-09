from __future__ import annotations

from typing import Any, Protocol

from app.models import OpenEvaluateRequest, PositionEvaluateRequest, TradeDecision


class StrategyEngine(Protocol):
    code: str

    def evaluate_open(
        self,
        request: OpenEvaluateRequest,
        deployment: dict[str, Any],
    ) -> TradeDecision: ...

    def evaluate_position(
        self,
        request: PositionEvaluateRequest,
        deployment: dict[str, Any],
    ) -> TradeDecision: ...


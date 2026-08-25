from __future__ import annotations

from typing import Any

import pytest

from app.models import (
    AccountIdentity,
    Candle,
    PositionEvaluateRequest,
    PositionSnapshot,
    UsageSummary,
)
from app.services.ai_service import AiCallResult
from app.strategies.pa_agent_lite import PaAgentLiteStrategy


class _PositionAi:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content

    def pa_position_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(
            content=self.content,
            usage=UsageSummary(ai_called=True, input_tokens=100, output_tokens=20),
        )


def _request(*, side: str, current: float, sl: float) -> PositionEvaluateRequest:
    candles = [
        Candle(
            timestamp=1_700_000_000 + index * 60,
            open=100,
            high=105,
            low=95,
            close=100,
            volume=100,
        )
        for index in range(50)
    ]
    return PositionEvaluateRequest(
        deployment_key="gl_pa_trailing_test",
        request_id=f"request_{side.lower()}_{current}",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M1",
        bar_time=candles[-1].timestamp,
        bid=current if side == "BUY" else current - 0.1,
        ask=current + 0.1 if side == "BUY" else current,
        spread_points=10,
        candles=candles,
        balance=10_000,
        equity=10_000,
        positions=[PositionSnapshot(
            ticket="123",
            symbol="XAUUSD",
            side=side,
            volume=0.1,
            open_price=100,
            current_price=current,
            profit=5,
            sl=sl,
            tp=130 if side == "BUY" else 70,
        )],
    )


def _deployment() -> dict[str, Any]:
    return {
        "strategy_code": "PA_AGENT_V1",
        "strategy_name": "GL趋势自动分析策略",
        "config": {
            "position_size_mode": "fixed",
            "fixed_volume": 0.1,
            "max_loss_per_position": 100,
            "take_profit_per_position": 150,
        },
    }


@pytest.mark.parametrize(
    ("side", "current", "existing_sl", "expected_sl"),
    [
        ("BUY", 105, 88, 100),
        ("BUY", 110, 100, 105),
        ("SELL", 95, 112, 100),
        ("SELL", 90, 100, 95),
    ],
)
def test_ai_hold_applies_official_atr_protective_stop(
    side: str,
    current: float,
    existing_sl: float,
    expected_sl: float,
) -> None:
    strategy = PaAgentLiteStrategy(_PositionAi({
        "action": "hold",
        "ticket": "123",
        "confidence": 0.6,
        "reason": "继续持仓",
    }))

    decision = strategy.evaluate_position(
        _request(side=side, current=current, sl=existing_sl),
        _deployment(),
    )

    assert decision.action == "MODIFY_SL"
    assert decision.sl == pytest.approx(expected_sl)
    assert decision.usage.ai_called is True


def test_ai_close_has_priority_over_official_atr_protective_stop() -> None:
    strategy = PaAgentLiteStrategy(_PositionAi({
        "action": "close",
        "ticket": "123",
        "confidence": 0.8,
        "reason": "结构反转，立即平仓",
    }))

    request = _request(side="BUY", current=110, sl=88)
    request.positions[0].open_time = request.bar_time
    decision = strategy.evaluate_position(request, _deployment())

    assert decision.action == "CLOSE"
    assert decision.position_ticket == "123"

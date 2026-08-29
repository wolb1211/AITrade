from __future__ import annotations

from typing import Any

import pytest

from app.api.router import _candles
from app.models import (
    AccountIdentity,
    Candle,
    Mt5Bar,
    OpenEvaluateRequest,
    PositionEvaluateRequest,
    PositionSnapshot,
    UsageSummary,
)
from app.services.ai_service import AiCallResult
from app.strategies.pa_agent_lite import (
    PaAgentLiteStrategy,
    _breakout_event,
    _pullback_metrics,
)


class _OpenAi:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content

    def pa_open_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(
            content=self.content,
            usage=UsageSummary(ai_called=True, input_tokens=10, output_tokens=5),
        )


class _PositionAi:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content

    def pa_position_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(
            content=self.content,
            usage=UsageSummary(ai_called=True, input_tokens=10, output_tokens=5),
        )


def _trend_candles() -> list[Candle]:
    candles: list[Candle] = []
    for index in range(60):
        open_price = 100 + index
        close_price = open_price + 1
        candles.append(Candle(
            timestamp=1_700_000_000 + index * 300,
            open=open_price,
            high=close_price,
            low=open_price - 0.5,
            close=close_price,
            volume=100,
        ))
    return candles


def _flat_candles() -> list[Candle]:
    return [
        Candle(
            timestamp=1_700_000_000 + index * 300,
            open=100,
            high=105,
            low=95,
            close=100,
            volume=100,
        )
        for index in range(50)
    ]


def _open_request(*, risk_symbol_info: bool = False) -> OpenEvaluateRequest:
    candles = _trend_candles()
    return OpenEvaluateRequest(
        deployment_key="gl_pa_safety_open",
        request_id="pa_safety_open_request",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=candles[-1].timestamp,
        bid=160,
        ask=160.1,
        spread_points=0.1,
        candles=candles,
        symbol_info={
            "contract_size": 100,
            "volume_min": 0.01,
            "volume_max": 100,
            "volume_step": 0.01,
        } if risk_symbol_info else {},
        balance=10_000,
        equity=10_000,
    )


def _position_request(*, profit: float = 5, current: float = 105, sl: float = 100) -> PositionEvaluateRequest:
    candles = _flat_candles()
    return PositionEvaluateRequest(
        deployment_key="gl_pa_safety_position",
        request_id="pa_safety_position_request",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=candles[-1].timestamp,
        bid=current,
        ask=current + 0.1,
        spread_points=0.1,
        candles=candles,
        balance=10_000,
        equity=10_000,
        positions=[PositionSnapshot(
            ticket="123",
            symbol="XAUUSD",
            side="BUY",
            volume=0.1,
            open_price=100,
            current_price=current,
            profit=profit,
            sl=sl,
            tp=130,
        )],
    )


def _deployment(**config: Any) -> dict[str, Any]:
    return {
        "strategy_code": "PA_AGENT_V1",
        "strategy_name": "GL趋势自动分析策略",
        "config": {
            "position_size_mode": "fixed",
            "fixed_volume": 0.1,
            "max_positions": 1,
            "allow_add": False,
            **config,
        },
    }


def test_mt_candles_are_normalized_oldest_to_newest() -> None:
    bars = [
        Mt5Bar(time=300, open=3, high=3, low=3, close=3),
        Mt5Bar(time=100, open=1, high=1, low=1, close=1),
        Mt5Bar(time=200, open=2, high=2, low=2, close=2),
    ]

    assert [item.timestamp for item in _candles(bars)] == [100, 200, 300]


def test_breakout_event_uses_range_before_latest_candle() -> None:
    previous = [
        Candle(timestamp=index, open=100, high=110, low=90, close=100)
        for index in range(4)
    ]
    failed_breakout = Candle(timestamp=4, open=100, high=112, low=99, close=109)
    clean_breakout = Candle(timestamp=4, open=110.5, high=112, low=110.5, close=111)

    assert _breakout_event([*previous, failed_breakout], 110, 90) == "failed_breakout_up"
    assert _breakout_event([*previous, clean_breakout], 110, 90) == "range_breakout_up"


def test_pullback_bars_means_bars_since_latest_pivot() -> None:
    depth, bars = _pullback_metrics(
        [{"kind": "low", "price": 95.0, "index": 35}],
        close=100,
        atr14=10,
        window_size=40,
    )

    assert depth == pytest.approx(0.5)
    assert bars == 4


def test_ai_cannot_reverse_the_server_open_candidate() -> None:
    strategy = PaAgentLiteStrategy(_OpenAi({
        "should_open": True,
        "direction": "sell",
        "confidence": 0.9,
        "sl_distance_price": 1,
        "tp_distance_price": 2,
        "reason": "建议反向开仓",
    }))

    decision = strategy.evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "方向冲突" in decision.reason


def test_ai_open_keeps_structure_stop_and_minimum_risk_reward() -> None:
    request = _open_request()
    deployment = _deployment()
    local = PaAgentLiteStrategy().evaluate_open(request, deployment)
    strategy = PaAgentLiteStrategy(_OpenAi({
        "should_open": True,
        "direction": "buy",
        "confidence": 0.9,
        "sl_distance_price": 0.1,
        "tp_distance_price": 0.1,
        "reason": "确认多头候选",
    }))

    decision = strategy.evaluate_open(request, deployment)

    assert decision.action == "BUY"
    assert decision.sl == pytest.approx(local.sl)
    assert (decision.tp - decision.entry) / (decision.entry - decision.sl) >= 1.8 - 1e-9


def test_risk_sizing_rejects_trade_when_minimum_lot_exceeds_budget() -> None:
    decision = PaAgentLiteStrategy().evaluate_open(
        _open_request(risk_symbol_info=True),
        _deployment(
            position_size_mode="risk",
            risk_base_mode="fixed_loss",
            risk_amount=1,
        ),
    )

    assert decision.action == "HOLD"
    assert "最小交易手数" in decision.reason


def test_ai_cannot_add_when_strategy_disables_adding() -> None:
    strategy = PaAgentLiteStrategy(_PositionAi({
        "action": "add",
        "ticket": "123",
        "direction": "buy",
        "confidence": 0.8,
        "reason": "继续加仓",
    }))

    decision = strategy.evaluate_position(_position_request(), _deployment())

    assert decision.action == "HOLD"
    assert "不允许本次加仓" in decision.reason


@pytest.mark.parametrize(
    "config, direction",
    [
        ({"allow_add": True, "max_positions": 1}, "buy"),
        ({"allow_add": True, "max_positions": 2}, "sell"),
    ],
)
def test_ai_add_obeys_position_limit_and_existing_direction(
    config: dict[str, Any],
    direction: str,
) -> None:
    strategy = PaAgentLiteStrategy(_PositionAi({
        "action": "add",
        "ticket": "123",
        "direction": direction,
        "confidence": 0.8,
        "reason": "继续加仓",
    }))

    decision = strategy.evaluate_position(_position_request(), _deployment(**config))

    assert decision.action == "HOLD"
    assert "不允许本次加仓" in decision.reason


def test_ai_cannot_loosen_an_existing_stop() -> None:
    strategy = PaAgentLiteStrategy(_PositionAi({
        "action": "modify",
        "ticket": "123",
        "confidence": 0.8,
        "sl": 99,
        "tp": 130,
        "reason": "修改止损",
    }))

    decision = strategy.evaluate_position(_position_request(), _deployment())

    assert decision.action == "HOLD"
    assert "未收紧" in decision.reason


def test_ai_can_tighten_an_existing_stop() -> None:
    strategy = PaAgentLiteStrategy(_PositionAi({
        "action": "modify",
        "ticket": "123",
        "confidence": 0.8,
        "sl": 103,
        "tp": 130,
        "reason": "收紧止损",
    }))

    decision = strategy.evaluate_position(_position_request(), _deployment())

    assert decision.action == "MODIFY_SL"
    assert decision.sl == pytest.approx(103)


def test_fixed_volume_has_no_hidden_money_close_thresholds() -> None:
    decision = PaAgentLiteStrategy().evaluate_position(
        _position_request(profit=200, current=100, sl=90),
        _deployment(max_loss_per_position=100, take_profit_per_position=150),
    )

    assert decision.action == "HOLD"


def test_risk_sizing_keeps_configured_money_loss_limit() -> None:
    decision = PaAgentLiteStrategy().evaluate_position(
        _position_request(profit=-101, current=100, sl=90),
        _deployment(
            position_size_mode="risk",
            risk_base_mode="fixed_loss",
            risk_amount=100,
        ),
    )

    assert decision.action == "CLOSE"
    assert decision.position_ticket == "123"

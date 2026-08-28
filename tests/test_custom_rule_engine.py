from __future__ import annotations

import pytest

from app.models import AccountIdentity, Candle, OpenEvaluateRequest, PositionEvaluateRequest, PositionSnapshot
from app.models import UsageSummary
from app.services.ai_service import AiCallResult
from app.services.custom_rule_engine import (
    RulePlanError,
    evaluate_open_rule_plan,
    evaluate_position_rule_plan,
    normalize_rule_plan,
)
from app.strategies.custom_ai import CustomAiStrategy


def _candles() -> list[Candle]:
    return [
        Candle(timestamp=index, open=100 + index, high=102 + index, low=99 + index, close=101 + index, volume=1)
        for index in range(10)
    ]


def _open_request() -> OpenEvaluateRequest:
    return OpenEvaluateRequest(
        deployment_key="gl_test_key",
        request_id="request-open-001",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=10,
        bid=110,
        ask=110.2,
        spread_points=0,
        candles=_candles(),
        balance=10000,
        equity=10000,
    )


def _position_request() -> PositionEvaluateRequest:
    return PositionEvaluateRequest(
        deployment_key="gl_test_key",
        request_id="request-position-001",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=10,
        bid=96,
        ask=96.2,
        spread_points=0,
        candles=_candles(),
        balance=10000,
        equity=10000,
        positions=[
            PositionSnapshot(
                ticket="123",
                symbol="XAUUSD",
                side="SELL",
                volume=0.1,
                open_price=100,
                current_price=96.2,
                profit=38,
                sl=105,
            )
        ],
    )


def test_open_rule_engine_uses_latest_cross_and_candle_extreme() -> None:
    plan = normalize_rule_plan(
        {
            "mode": "deterministic",
            "rules": [
                {
                    "when": "latest_cross('ema5', 'ema30', 3) == 1",
                    "action": {"type": "open", "direction": "buy", "sl": "lowest_low(5)"},
                    "description": "最近3根EMA5上穿EMA30时开多",
                },
                {
                    "when": "latest_cross('ema5', 'ema30', 3) == -1",
                    "action": {"type": "open", "direction": "sell", "sl": "highest_high(5)"},
                    "description": "最近3根EMA5下穿EMA30时开空",
                },
            ],
        },
        stage="open",
        indicator_aliases={"ema5", "ema30"},
    )
    result = evaluate_open_rule_plan(
        plan,
        request=_open_request(),
        indicators={"values": {"ema5": [99, 101, 103], "ema30": [100, 100.5, 101]}},
    )

    assert result.action == "open"
    assert result.direction == "buy"
    assert result.sl == 104


def test_open_rule_engine_does_not_treat_below_and_falling_as_cross() -> None:
    plan = normalize_rule_plan(
        {
            "mode": "deterministic",
            "rules": [{
                "when": "latest_cross('ema5', 'ema30', 3) == -1",
                "action": {"type": "open", "direction": "sell", "sl": "highest_high(5)"},
                "description": "EMA5下穿EMA30时开空",
            }],
        },
        stage="open",
        indicator_aliases={"ema5", "ema30"},
    )
    result = evaluate_open_rule_plan(
        plan,
        request=_open_request(),
        indicators={"values": {"ema5": [97, 96, 95], "ema30": [101, 100, 99]}},
    )

    assert result.action == "hold"


def test_open_rule_engine_uses_recent_candles_when_ea_sends_newest_first() -> None:
    plan = normalize_rule_plan(
        {
            "mode": "deterministic",
            "rules": [{
                "when": "latest_cross('ema5', 'ema30', 3) == 1",
                "action": {"type": "open", "direction": "buy", "sl": "lowest_low(5)"},
                "description": "EMA crossover buy",
            }],
        },
        stage="open",
        indicator_aliases={"ema5", "ema30"},
    )
    request = _open_request()
    request.candles = list(reversed(request.candles))
    result = evaluate_open_rule_plan(
        plan,
        request=request,
        indicators={"values": {"ema5": [99, 101, 103], "ema30": [100, 100.5, 101]}},
    )

    assert result.action == "open"
    assert result.sl == 104


def test_position_rule_engine_calculates_sell_break_even_from_open_price() -> None:
    plan = normalize_rule_plan(
        {
            "mode": "deterministic",
            "rules": [{
                "when": "side == 'SELL' and sl > open_price and favorable_move >= 0.5 * atr14",
                "action": {"type": "modify", "sl": "open_price - 0.2"},
                "description": "空单达到保本条件时移动止损",
            }],
        },
        stage="position",
        indicator_aliases={"atr14"},
    )
    request = _position_request()
    result = evaluate_position_rule_plan(
        plan,
        request=request,
        position=request.positions[0],
        indicators={"values": {"atr14": [5, 5.2, 5.4]}},
    )

    assert result.action == "modify"
    assert result.sl == 99.8


def test_rule_plan_rejects_unknown_variables_and_missing_position_side() -> None:
    with pytest.raises(RulePlanError, match="unknown_rule_name"):
        normalize_rule_plan(
            {"mode": "deterministic", "rules": [{
                "when": "current_profit > 0 and atr14 > 0",
                "action": {"type": "modify", "sl": "open_price - 0.2"},
                "description": "空单达到保本条件时移动止损",
            }]},
            stage="position",
            indicator_aliases={"atr14"},
        )

    with pytest.raises(RulePlanError, match="sell_position_rule_requires_side_guard"):
        normalize_rule_plan(
            {"mode": "deterministic", "rules": [{
                "when": "profit > 0 and atr14 > 0",
                "action": {"type": "modify", "sl": "open_price - 0.2"},
                "description": "空单达到保本条件时移动止损",
            }]},
            stage="position",
            indicator_aliases={"atr14"},
        )


def test_rule_plan_accepts_string_null_stop_constraint() -> None:
    plan = normalize_rule_plan(
        {
            "mode": "deterministic",
            "rules": [{
                "when": "side == 'BUY' and current_price > open_price",
                "action": {
                    "type": "modify",
                    "sl": "open_price",
                    "tp": "none",
                    "sl_constraint": "null",
                },
                "description": "BUY position moves stop loss",
            }],
        },
        stage="position",
    )

    assert plan["mode"] == "deterministic"
    assert plan["rules"][0]["action"]["sl_constraint"] is None
    assert plan["rules"][0]["action"]["tp"] is None


def test_rule_plan_rejects_incomplete_cross_function_arguments() -> None:
    with pytest.raises(RulePlanError, match="invalid_rule_function_arity:latest_cross"):
        normalize_rule_plan(
            {
                "mode": "deterministic",
                "rules": [{
                    "when": "side == 'BUY' and latest_cross('ema', -1) == -1",
                    "action": {"type": "close", "close_scope": "full"},
                    "description": "多单视觉信号平仓",
                }],
            },
            stage="position",
        )


class _ContradictingExplanationAi:
    def custom_rule_explanation(self, *, endpoint: str, **_: object) -> AiCallResult:
        content = (
            {"should_open": False, "direction": None, "reason": "AI尝试改为等待", "analysis": "系统规则已经满足。"}
            if endpoint == "open"
            else {"action": "hold", "reason": "AI尝试改为持有", "analysis": "系统风控规则已经满足。"}
        )
        return AiCallResult(content=content, usage=UsageSummary(ai_called=True))


def test_custom_strategy_executes_engine_open_action_even_if_explanation_ai_disagrees() -> None:
    strategy = CustomAiStrategy(_ContradictingExplanationAi())
    decision = strategy.evaluate_open(
        _open_request(),
        {
            "config": {
                "open_rule_plan": normalize_rule_plan(
                    {"mode": "deterministic", "rules": [{
                        "when": "ask > bid",
                        "action": {"type": "open", "direction": "buy", "sl": "lowest_low(5)"},
                        "description": "满足测试开多条件",
                    }]},
                    stage="open",
                ),
                "open_indicators": [],
                "position_size_mode": "fixed",
                "fixed_volume": 0.1,
            }
        },
    )

    assert decision.action == "BUY"
    assert decision.sl == 104
    assert decision.reason == "系统规则已经满足。"


def test_custom_strategy_executes_engine_stop_price_even_if_explanation_ai_disagrees() -> None:
    strategy = CustomAiStrategy(_ContradictingExplanationAi())
    request = _position_request()
    decision = strategy.evaluate_position(
        request,
        {
            "config": {
                "position_rule_plan": normalize_rule_plan(
                    {"mode": "deterministic", "rules": [{
                        "when": "side == 'SELL' and sl > open_price and favorable_move >= 0.5 * atr14",
                        "action": {"type": "modify", "sl": "open_price - 0.2"},
                        "description": "空单达到保本条件时移动止损",
                    }]},
                    stage="position",
                    indicator_aliases={"atr14"},
                ),
                "position_indicators": [{"name": "atr", "alias": "atr14", "source": "close", "params": {"length": 2}}],
            }
        },
    )

    assert decision.action == "MODIFY_SL"
    assert decision.sl == 99.8
    assert decision.reason == "系统风控规则已经满足。"

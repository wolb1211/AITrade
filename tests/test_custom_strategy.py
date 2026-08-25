from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    AccountIdentity,
    Candle,
    OpenEvaluateRequest,
    PositionEvaluateRequest,
    PositionSnapshot,
    UsageSummary,
)
from app.services.ai_service import AiCallResult, AiDecisionClient
from app.services.custom_indicators import (
    INDICATOR_DEFINITIONS,
    calculate_indicator_payload,
    normalize_indicator_specs,
    required_candle_count,
)
from app.store import SqliteStore
from app.strategies.custom_ai import CustomAiStrategy


def _candles(count: int = 360) -> list[Candle]:
    return [
        Candle(
            timestamp=1_700_000_000 + index * 60,
            open=1900 + index * 0.1,
            high=1901 + index * 0.1,
            low=1899 + index * 0.1,
            close=1900.5 + index * 0.1,
            volume=100 + index,
        )
        for index in range(count)
    ]


def _open_request() -> OpenEvaluateRequest:
    return OpenEvaluateRequest(
        deployment_key="gl_custom_test",
        request_id="request_custom_open",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M15",
        bar_time=1_700_030_000,
        bid=1935.9,
        ask=1936.1,
        spread_points=20,
        candles=_candles(),
        balance=10_000,
        equity=10_000,
        symbol_info={"tick_size": 0.01, "tick_value": 1, "volume_min": 0.01, "volume_step": 0.01},
    )


def test_indicator_arrays_are_aligned_and_keep_100_effective_values() -> None:
    specs, unsupported = normalize_indicator_specs([
        {"name": "ema", "params": {"length": 10}},
        {"name": "ema", "params": {"length": 20}},
        {"name": "unknown_custom_indicator"},
    ])
    assert unsupported == ["unknown_custom_indicator"]
    assert required_candle_count(specs) >= 160
    payload = calculate_indicator_payload(_candles(), specs, output_count=100)
    assert payload["order"] == "oldest_to_latest"
    assert len(payload["timestamps"]) == 100
    assert set(payload["values"]) == {"ema10", "ema20"}
    assert all(len(values) == 100 for values in payload["values"].values())


def test_single_series_indicators_support_derived_price_sources() -> None:
    specs, unsupported = normalize_indicator_specs([
        {"name": "ema", "source": "open", "params": {"length": 10}},
        {"name": "ema", "source": "oc2", "params": {"length": 10}},
        {"name": "ema", "source": "ohlc4", "params": {"length": 10}},
    ])
    assert unsupported == []
    payload = calculate_indicator_payload(_candles(), specs, output_count=100)
    assert set(payload["values"]) == {"ema10_open", "ema10_oc2", "ema10_ohlc4"}
    assert all(len(values) == 100 for values in payload["values"].values())


def test_every_published_indicator_has_runtime_output() -> None:
    for definition in INDICATOR_DEFINITIONS:
        specs, unsupported = normalize_indicator_specs([{"name": definition.name}])
        payload = calculate_indicator_payload(_candles(500), specs, output_count=100)
        assert unsupported == [], definition.name
        assert payload["values"], definition.name
        assert payload["timestamps"], definition.name


class _FakeAiClient:
    def custom_open_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(
            content={
                "should_open": True,
                "direction": "buy",
                "confidence": 0.82,
                "sl": 1899.0,
                "tp": 1945.0,
                "reason": "连续下跌后出现看涨吞没",
                "analysis": "最近十根K线总体下降，最新已收盘K线形成看涨吞没，按规则开多并以近期最低价设置止损。",
            },
            usage=UsageSummary(ai_called=True, input_tokens=500, output_tokens=100, charged_points=600),
        )

    def custom_position_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(
            content={"action": "close", "ticket": "123", "volume": 0.03, "confidence": 0.8, "reason": "部分平仓"},
            usage=UsageSummary(ai_called=True),
        )


def test_custom_strategy_can_open_from_candlestick_rule_and_partial_close() -> None:
    strategy = CustomAiStrategy(_FakeAiClient())  # type: ignore[arg-type]
    deployment = {
        "strategy_name": "K线形态策略",
        "config": {"fixed_volume": 0.1, "position_size_mode": "fixed", "allow_add": True, "max_positions": 3},
    }
    opened = strategy.evaluate_open(_open_request(), deployment)
    assert opened.action == "BUY"
    assert opened.sl == 1899.0
    assert opened.lot == 0.1

    source = _open_request()
    position_request = PositionEvaluateRequest(
        **source.model_dump(exclude={"balance", "equity"}),
        balance=source.balance,
        equity=source.equity,
        positions=[PositionSnapshot(
            ticket="123", symbol="XAUUSD", side="BUY", volume=0.1,
            open_price=1930, current_price=1936, profit=60,
        )],
    )
    closed = strategy.evaluate_position(position_request, deployment)
    assert closed.action == "CLOSE"
    assert closed.volume == 0.03


def test_runtime_payload_contains_closed_candles_and_calculated_indicators(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-runtime.db")
    store.initialize()
    client = AiDecisionClient(store)
    captured: dict[str, Any] = {}

    def fake_chat_json(**kwargs: Any) -> None:
        captured.update(kwargs)
        return None

    client._chat_json = fake_chat_json  # type: ignore[method-assign]
    client.custom_open_decision(
        deployment={
            "strategy_name": "EMA策略",
            "config": {
                "open_logic": "EMA10上穿EMA20时开多",
                "open_prompt_template": "严格判断均线交叉",
                "open_indicators": [
                    {"name": "ema", "source": "close", "params": {"length": 10}, "alias": "ema10"},
                    {"name": "ema", "source": "close", "params": {"length": 20}, "alias": "ema20"},
                ],
                "indicator_output_count": 100,
            },
        },
        request_payload=_open_request(),
    )
    payload = captured["user_payload"]
    assert len(payload["candles"]) == 100
    assert len(payload["indicators"]["timestamps"]) == 100
    assert payload["data_convention"]["last_item"] == "latest_closed_candle"

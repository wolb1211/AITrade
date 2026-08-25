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


def _position_request() -> PositionEvaluateRequest:
    source = _open_request()
    return PositionEvaluateRequest(
        **source.model_dump(exclude={"balance", "equity"}),
        balance=source.balance,
        equity=source.equity,
        positions=[PositionSnapshot(
            ticket="123", symbol="XAUUSD", side="BUY", volume=0.1,
            open_price=1930, current_price=1936, profit=60,
        )],
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
    assert all(len(values) == 3 for values in payload["recent_values"].values())


def test_indicator_payload_marks_latest_closed_bar_crossover() -> None:
    candles = [
        Candle(
            timestamp=1_700_000_000 + index * 60,
            open=100,
            high=121 if index == 39 else 101,
            low=99,
            close=120 if index == 39 else 100,
            volume=100,
        )
        for index in range(40)
    ]
    specs, _ = normalize_indicator_specs([
        {"name": "ema", "params": {"length": 5}},
        {"name": "ema", "params": {"length": 30}},
    ])

    payload = calculate_indicator_payload(candles, specs, output_count=100)

    assert payload["crossovers"] == [{
        "left": "ema5",
        "right": "ema30",
        "previous": {"left": 100.0, "right": 100.0},
        "latest": {"left": payload["values"]["ema5"][-1], "right": payload["values"]["ema30"][-1]},
        "event": "left_crossed_above",
    }]


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

    closed = strategy.evaluate_position(_position_request(), deployment)
    assert closed.action == "CLOSE"
    assert closed.volume == 0.03


class _PositionResultAiClient:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content

    def custom_position_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(content=self.content, usage=UsageSummary(ai_called=True))


def test_add_uses_user_lot_or_open_sizing_default_and_partial_close_never_guesses() -> None:
    deployment = {
        "strategy_name": "加减仓测试",
        "config": {"fixed_volume": 0.1, "position_size_mode": "fixed", "allow_add": True, "max_positions": 3},
    }
    explicit = CustomAiStrategy(_PositionResultAiClient({
        "action": "add", "direction": "buy", "lot": 0.2, "sl": 1920, "reason": "前一单两倍加仓",
    }))  # type: ignore[arg-type]
    explicit_result = explicit.evaluate_position(_position_request(), deployment)
    assert explicit_result.action == "BUY"
    assert explicit_result.lot == 0.2

    default = CustomAiStrategy(_PositionResultAiClient({
        "action": "add", "direction": "buy", "lot": None, "sl": 1920, "reason": "默认仓位加仓",
    }))  # type: ignore[arg-type]
    default_result = default.evaluate_position(_position_request(), deployment)
    assert default_result.action == "BUY"
    assert default_result.lot == 0.1

    missing_partial_size = CustomAiStrategy(_PositionResultAiClient({
        "action": "close", "ticket": "123", "close_scope": "partial", "volume": None,
        "reason": "部分平仓但未指定数量",
    }))  # type: ignore[arg-type]
    missing_result = missing_partial_size.evaluate_position(_position_request(), deployment)
    assert missing_result.action == "HOLD"
    assert "未明确" in missing_result.reason


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


def test_compilation_reconciles_indicators_per_stage_and_removes_false_add_warning(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-compile-review.db")
    store.initialize()
    client = AiDecisionClient(store)
    normalized = client.normalize_custom_strategy_compilation(
        {
            "summary": "EMA 与 ATR 策略",
            "open_prompt_template": "EMA5 上穿 EMA30 时开多，下破时开空。",
            "position_prompt_template": "ATR 移动止损，EMA 反向交叉时平仓。",
            "open_indicators": [
                {"name": "ema", "params": {"length": 5}},
                {"name": "ema", "params": {"length": 30}},
            ],
            "position_indicators": [{"name": "atr", "params": {"length": 14}}],
            "unsupported_indicators": [],
            "warnings": ["未定义加仓公式，系统将使用默认的开仓 sizing 算法。"],
        },
        open_logic="EMA5上穿EMA30时开多，EMA5下破EMA30时开空",
        position_logic="每盈利1个ATR移动止损；多单EMA5下破EMA30时平仓",
    )
    assert {item["alias"] for item in normalized["position_indicators"]} == {"atr14", "ema5", "ema30"}
    assert normalized["warnings"] == []


def test_compilation_adds_only_relevant_sizing_warnings(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-compile-warnings.db")
    store.initialize()
    client = AiDecisionClient(store)
    base = {
        "summary": "风控策略",
        "open_prompt_template": "满足条件时开仓。",
        "position_prompt_template": "严格执行风控规则。",
        "open_indicators": [],
        "position_indicators": [],
        "unsupported_indicators": [],
        "warnings": [],
    }
    missing_add_size = client.normalize_custom_strategy_compilation(
        base,
        position_logic="盈利达到2个ATR时加仓",
    )
    assert any("默认使用策略的开仓仓位算法" in item for item in missing_add_size["warnings"])
    explicit_add_size = client.normalize_custom_strategy_compilation(
        base,
        position_logic="盈利达到2个ATR时加仓，加仓手数为前一单的2倍",
    )
    assert not any("默认使用策略的开仓仓位算法" in item for item in explicit_add_size["warnings"])
    missing_partial_size = client.normalize_custom_strategy_compilation(
        base,
        position_logic="出现反向吞没时部分平仓",
    )
    assert any("未指定平仓比例或手数" in item for item in missing_partial_size["warnings"])


def test_custom_strategy_compiles_open_and_position_with_separate_calls(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-split-compile.db")
    store.initialize()
    client = AiDecisionClient(store)
    endpoints: list[str] = []

    def fake_chat_json(**kwargs: Any) -> AiCallResult:
        endpoint = str(kwargs["endpoint"])
        endpoints.append(endpoint)
        if endpoint == "compile_open":
            return AiCallResult(content={
                "summary": "EMA交叉开仓",
                "prompt_template": "EMA5上穿EMA30开多，下破开空。",
                "indicators": [{"name": "ema", "params": {"length": 5}}, {"name": "ema", "params": {"length": 30}}],
                "data_type": "kline", "unsupported_indicators": [], "warnings": [],
            }, usage=UsageSummary(ai_called=True))
        return AiCallResult(content={
            "summary": "ATR移动止损与EMA反向平仓",
            "prompt_template": "按ATR移动止损，EMA反向交叉时平仓。",
            "indicators": [{"name": "atr", "params": {"length": 14}}],
            "data_type": "kline", "unsupported_indicators": [], "warnings": [],
        }, usage=UsageSummary(ai_called=True))

    client._chat_json = fake_chat_json  # type: ignore[method-assign]
    compiled = client.compile_custom_strategy({
        "config": {
            "open_logic": "EMA5上穿EMA30开多，下破开空",
            "position_logic": "按ATR移动止损，EMA5和EMA30反向交叉时平仓",
        },
    })
    assert endpoints == ["compile_open", "compile_position"]
    assert {item["alias"] for item in compiled["open_indicators"]} == {"ema5", "ema30"}
    assert {item["alias"] for item in compiled["position_indicators"]} == {"atr14", "ema5", "ema30"}
    assert "开仓：EMA交叉开仓" in compiled["summary"]
    assert "持仓风控：ATR移动止损与EMA反向平仓" in compiled["summary"]


def test_split_compile_selects_the_corresponding_custom_model(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-split-model.db")
    store.initialize()
    client = AiDecisionClient(store)
    deployment = {"user_id": "1", "config": {
        "open_ai_mode": "custom", "open_ai_base_url": "https://open.example/v1", "open_ai_model": "open-model", "open_ai_key": "open-key",
        "position_ai_mode": "custom", "position_ai_base_url": "https://position.example/v1", "position_ai_model": "position-model", "position_ai_key": "position-key",
    }}
    assert client._select_model(deployment, "compile_open")["model"] == "open-model"  # type: ignore[index]
    assert client._select_model(deployment, "compile_position")["model"] == "position-model"  # type: ignore[index]


def test_position_template_normalizes_atr_units_and_filters_internal_notes(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-position-guardrails.db")
    store.initialize()
    client = AiDecisionClient(store)
    normalized = client.normalize_custom_strategy_compilation(
        {
            "summary": "ATR移动止损",
            "open_prompt_template": "EMA5上穿EMA30开多。",
            "position_prompt_template": "if profit > 0.5 * atr[14][-1]:\n    new_sl = entry_price",
            "open_indicators": [{"name": "ema", "params": {"length": 5}}, {"name": "ema", "params": {"length": 30}}],
            "position_indicators": [{"name": "atr", "params": {"length": 14}}],
            "unsupported_indicators": [],
            "warnings": [
                "ATR和EMA均基于收盘价计算，符合默认源；用户未指定其他价格源，故不变更input。",
                "ATR使用最新值（索引-1），交叉判断使用倒数第二根与最后一根K线以确保闭合性。",
            ],
        },
        open_logic="EMA5上穿EMA30开多",
        position_logic="盈利半个ATR后移动止损到保本",
    )
    template = normalized["position_prompt_template"]
    assert "atr14[-1]" in template
    assert "favorable_price_move > 0.5 * atr14[-1]" in template
    assert "ATR是由最高价、最低价和收盘价计算的价格距离" in template
    assert "移动止损只能收紧" in template
    assert normalized["warnings"] == []

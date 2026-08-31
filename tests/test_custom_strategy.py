from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.models import (
    AccountIdentity,
    Candle,
    OpenEvaluateRequest,
    PositionEvaluateRequest,
    PositionSnapshot,
    UsageSummary,
)
from app.services.ai_service import AiCallResult, AiDecisionClient
from app.services.ai_service import (
    _custom_runtime_prompt,
    _custom_strategy_stage_compile_prompt,
    _json_api_system_prompt,
)
from app.services.custom_indicators import (
    INDICATOR_DEFINITIONS,
    calculate_indicator_payload,
    indicator_output_capability,
    normalize_indicator_specs,
    public_indicator_catalog,
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


def test_indicator_payload_exposes_recent_values_without_preclassified_crossover() -> None:
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

    assert "crossovers" not in payload
    assert payload["recent_values"]["ema5"] == payload["values"]["ema5"][-3:]
    assert payload["recent_values"]["ema30"] == payload["values"]["ema30"][-3:]


def test_custom_open_runtime_prompt_forbids_invented_entry_filters() -> None:
    prompt = _custom_runtime_prompt("open")

    assert "Use only conditions explicitly written by the user" in prompt
    assert "Never add confirmation, breakout, trend" in prompt
    assert "should_open must be true" in prompt
    assert "Evaluate crossovers directly from supplied indicator arrays" in prompt
    assert "return the resulting absolute sl price" in prompt


def test_custom_json_prompt_contains_no_generic_market_analysis_requirements() -> None:
    prompt = _json_api_system_prompt("open", _custom_runtime_prompt("open"), literal_user_rules=True)

    assert "sole source of trading logic" in prompt
    assert "only the supplied user rule" in prompt
    assert "include market structure" not in prompt
    assert "setup_score" not in prompt
    assert "price/risk context" not in prompt
    assert "Complete valid JSON has highest priority" in prompt
    assert "never put step-by-step reasoning" in prompt
    assert "analysis must be a clear, natural and compact Chinese strategy explanation" in prompt
    assert "identify the exact unmet condition" in prompt


def test_custom_position_compile_prompt_preserves_staged_stop_rules() -> None:
    prompt = _custom_strategy_stage_compile_prompt("position")

    assert "explicit stage conditions" in prompt
    assert "earlier stage is completed" in prompt
    assert "Never flatten a user-defined sequence" in prompt
    assert "Do not invent a default priority" in prompt
    assert "or any indicator, threshold, stage, trigger or risk rule" in prompt


def test_custom_position_runtime_prompt_prevents_completed_stage_reentry() -> None:
    prompt = _custom_runtime_prompt("position")

    assert "determine whether a user-defined prior stage has completed" in prompt
    assert "Apply a one-way stop restriction only if the user" in prompt
    assert "otherwise a requested stop may tighten or loosen" in prompt
    assert "structured sl value must exactly match" in prompt
    assert "must not create a new trigger, filter, stage, priority or risk condition" in prompt


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


def test_every_published_indicator_has_editor_output_capabilities() -> None:
    catalog = public_indicator_catalog()
    assert {item["name"] for item in catalog} == {item.name for item in INDICATOR_DEFINITIONS}
    for indicator in catalog:
        assert indicator["outputs"], indicator["name"]
        components = [output["component"] for output in indicator["outputs"]]
        assert len(components) == len(set(components)), indicator["name"]
        for output in indicator["outputs"]:
            assert output["title"]
            assert output["value_type"]
            assert output["comparison_group"]
            assert output["operators"]
            assert output["right_operand_kinds"]
            assert output["condition_kinds"]
            assert output["minimum_points"] >= 1


def test_indicator_capabilities_restrict_comparison_targets_by_semantics() -> None:
    ema = indicator_output_capability("ema")
    assert ema is not None
    assert ema.comparison_group == "price"
    assert {"market_price", "candle", "indicator", "constant"} <= set(ema.right_operand_kinds)
    assert "cross" in ema.condition_kinds

    macd_histogram = indicator_output_capability("macd", "histogram")
    assert macd_histogram is not None
    assert macd_histogram.default_constant == 0
    assert set(macd_histogram.right_operand_kinds) == {"constant", "indicator"}
    assert "price" not in macd_histogram.compatible_groups

    supertrend_direction = indicator_output_capability("supertrend", "direction")
    assert supertrend_direction is not None
    assert supertrend_direction.operators == ("eq", "neq")
    assert supertrend_direction.right_operand_kinds == ("constant",)
    assert supertrend_direction.constant_options == ((1, "多头"), (-1, "空头"))


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


class _PositionModifyAi:
    def __init__(self, sl: float, tp: float | None = None) -> None:
        self.sl = sl
        self.tp = tp

    def custom_position_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(
            content={
                "action": "modify",
                "ticket": "123",
                "sl": self.sl,
                "tp": self.tp,
                "confidence": 0.8,
                "reason": "移动止损",
                "analysis": "分析文字可能与结构化止损字段不一致",
            },
            usage=UsageSummary(ai_called=True),
        )


@pytest.mark.parametrize(
    ("side", "current_sl", "requested_sl"),
    [
        ("BUY", 1932.0, 1925.0),
        ("BUY", 1932.0, 1934.0),
        ("SELL", 1940.0, 1945.0),
        ("SELL", 1940.0, 1938.0),
    ],
)
def test_custom_position_stop_follows_ai_rule_without_direction_override(
    side: str,
    current_sl: float,
    requested_sl: float,
) -> None:
    request = _position_request()
    request.positions[0].side = side
    request.positions[0].sl = current_sl
    strategy = CustomAiStrategy(_PositionModifyAi(requested_sl))

    decision = strategy.evaluate_position(request, {"config": {}})

    assert decision.action == "MODIFY_SL"
    assert decision.sl == requested_sl
    assert decision.reason == "分析文字可能与结构化止损字段不一致"


def test_custom_position_modify_preserves_existing_tp() -> None:
    request = _position_request()
    request.positions[0].sl = 1932.0
    request.positions[0].tp = 1950.0
    strategy = CustomAiStrategy(_PositionModifyAi(1934.0))

    decision = strategy.evaluate_position(request, {"config": {}})

    assert decision.action == "MODIFY_SL"
    assert decision.sl == 1934.0
    assert decision.tp == 1950.0


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


class _OpenResultAiClient:
    def __init__(self, content: dict[str, Any]) -> None:
        self.content = content

    def custom_open_decision(self, **_: Any) -> AiCallResult:
        return AiCallResult(content=self.content, usage=UsageSummary(ai_called=True))


def test_open_stop_uses_ai_result_without_local_natural_language_override() -> None:
    request = _open_request()
    request.candles = list(reversed(request.candles))
    strategy = CustomAiStrategy(_OpenResultAiClient({
        "should_open": True,
        "direction": "buy",
        "confidence": 0.9,
        "sl": 1899.25,
        "reason": "EMA5上穿EMA30",
    }))  # type: ignore[arg-type]
    deployment = {
        "strategy_name": "EMA交叉策略",
        "config": {
            "fixed_volume": 0.1,
            "position_size_mode": "fixed",
            "open_logic": "EMA5上穿EMA30开多，止损在最近5根K线的低点",
        },
    }

    decision = strategy.evaluate_open(request, deployment)

    assert decision.action == "BUY"
    assert decision.sl == pytest.approx(1899.25)


def test_server_does_not_parse_natural_language_to_override_missing_stop() -> None:
    strategy = CustomAiStrategy(_OpenResultAiClient({
        "should_open": True,
        "direction": "buy",
        "confidence": 0.9,
        "sl": None,
        "reason": "满足开仓条件",
    }))  # type: ignore[arg-type]
    deployment = {
        "strategy_name": "复杂止损策略",
        "config": {
            "fixed_volume": 0.1,
            "position_size_mode": "fixed",
            "open_logic": "满足形态时开多，止损使用外部指标确认价",
        },
    }

    decision = strategy.evaluate_open(_open_request(), deployment)

    assert decision.action == "BUY"
    assert decision.sl is None


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
    assert [item["t"] for item in payload["candles"]] == sorted(item["t"] for item in payload["candles"])
    assert payload["candles"][-1]["t"] == max(item.timestamp for item in _open_request().candles)
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


def test_compilation_collects_unsupported_conditions_and_count(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-unsupported-conditions.db")
    store.initialize()
    client = AiDecisionClient(store)
    normalized = client.normalize_custom_strategy_compilation(
        {
            "summary": "主观形态策略",
            "open_prompt_template": "出现强势形态时开仓。",
            "position_prompt_template": "没有条件时继续持有。",
            "open_rule_plan": {"version": 1, "mode": "ai", "rules": []},
            "position_rule_plan": {"version": 1, "mode": "deterministic", "rules": [{
                "when": "side == 'BUY' and current_price < open_price",
                "action": {"type": "close", "close_scope": "full"},
                "description": "多单价格低于开仓价时平仓",
            }]},
            "unsupported_conditions": [{
                "stage": "open",
                "code": "subjective_strength",
                "text": "出现明显强势形态",
                "reason": "强势程度属于主观判断",
            }],
        },
        open_logic="出现明显强势形态时开多",
        position_logic="多单价格低于开仓价时平仓",
    )

    assert normalized["unsupported_condition_count"] == 1
    assert normalized["unsupported_conditions"][0]["code"] == "subjective_strength"


def test_compilation_keeps_supported_visual_conditions_out_of_unsupported_count(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "custom-visual-conditions.db")
    store.initialize()
    client = AiDecisionClient(store)
    normalized = client.normalize_custom_strategy_compilation(
        {
            "summary": "截图趋势线策略",
            "open_prompt_template": "截图中突破手工趋势线时开多。",
            "position_prompt_template": "截图中跌破手工趋势线时平仓。",
            "open_rule_plan": {"version": 1, "mode": "ai", "rules": []},
            "position_rule_plan": {"version": 1, "mode": "ai", "rules": []},
            "visual_conditions": [
                {"stage": "open", "code": "drawn_trendline_break", "text": "突破手工趋势线"},
                {"stage": "position", "code": "drawn_trendline_break", "text": "跌破手工趋势线"},
            ],
        },
        open_logic="截图中突破手工趋势线时开多",
        position_logic="截图中跌破手工趋势线时平仓",
    )

    assert normalized["unsupported_condition_count"] == 0
    assert len(normalized["visual_conditions"]) == 2


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
    assert "移动止损只能收紧" not in template
    assert "优先执行平仓" not in template
    assert normalized["warnings"] == []

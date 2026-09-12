"""Tests for the two-stage PA Agent port: diagnosis -> order decision."""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from app.api.router import _mt5_open_response
from app.models import (
    AccountIdentity,
    Candle,
    OpenEvaluateRequest,
    PositionEvaluateRequest,
    PositionSnapshot,
    UsageSummary,
)
from app.services.ai_service import AiCallResult
from app.strategies import pa_knowledge
from app.strategies.pa_agent_lite import (
    PaAgentLiteStrategy,
    _ai_bool,
    _blocking_gate_reason,
    _features_unavailable_reason,
    _knowledge_setup_codes,
    _merge_usage,
    _order_type_code,
    _resolve_entry_price,
)

ANALYSIS = "结构清晰、动能跟随良好，风险回报满足要求，按计划执行本次交易。"


class _TwoStageAi:
    """Fake AI client that records which stages were called."""

    def __init__(
        self,
        order: dict[str, Any],
        *,
        diagnosis: dict[str, Any] | None = None,
    ) -> None:
        self.order = order
        self.diagnosis = diagnosis if diagnosis is not None else {
            "cycle": "normal_channel",
            "direction": "bullish",
            "gates": {},
        }
        self.diagnosis_calls = 0
        self.order_calls = 0
        self.strategy_texts: list[str] = []

    def pa_open_diagnosis(self, **_: Any) -> AiCallResult:
        self.diagnosis_calls += 1
        return AiCallResult(
            content=self.diagnosis,
            usage=UsageSummary(ai_called=True, input_tokens=8, output_tokens=4, charged_points=12),
        )

    def pa_open_decision(self, **kwargs: Any) -> AiCallResult:
        self.order_calls += 1
        self.strategy_texts.append(str(kwargs.get("strategy_text") or ""))
        return AiCallResult(
            content=self.order,
            usage=UsageSummary(ai_called=True, input_tokens=10, output_tokens=5, charged_points=15),
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


def _open_request(*, bid: float = 160, ask: float = 160.1) -> OpenEvaluateRequest:
    candles = _trend_candles()
    return OpenEvaluateRequest(
        deployment_key="gl_pa_port_open",
        request_id="pa_port_open_request",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=candles[-1].timestamp,
        bid=bid,
        ask=ask,
        spread_points=ask - bid,
        candles=candles,
        symbol_info={"contract_size": 100, "volume_min": 0.01, "volume_max": 100, "volume_step": 0.01},
        balance=10_000,
        equity=10_000,
    )


def _deployment(**config: Any) -> dict[str, Any]:
    return {
        "strategy_code": "PA_AGENT_V1",
        "strategy_name": "PA Agent",
        "config": {
            "position_size_mode": "fixed",
            "fixed_volume": 0.1,
            "max_positions": 1,
            "allow_add": False,
            **config,
        },
    }


def _two_position_request() -> PositionEvaluateRequest:
    candles = _trend_candles()
    return PositionEvaluateRequest(
        deployment_key="gl_pa_port_position",
        request_id="pa_port_position_request",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=candles[-1].timestamp,
        bid=160,
        ask=160.1,
        spread_points=0.1,
        candles=candles,
        balance=10_000,
        equity=10_000,
        positions=[
            PositionSnapshot(
                ticket="111",
                symbol="XAUUSD",
                side="BUY",
                volume=0.1,
                open_price=150,
                current_price=160,
                sl=148,
                tp=170,
                profit=10,
                open_time=candles[0].timestamp,
            ),
            PositionSnapshot(
                ticket="222",
                symbol="XAUUSD",
                side="BUY",
                volume=0.1,
                open_price=155,
                current_price=160,
                sl=153,
                tp=175,
                profit=5,
                open_time=candles[1].timestamp,
            ),
        ],
    )


# ── order type mapping ────────────────────────────────────────────────────────


def test_order_wording_maps_onto_mt5_order_types() -> None:
    assert _order_type_code("市价单") == "market"
    assert _order_type_code("限价单") == "limit"
    assert _order_type_code("突破单") == "stop"
    assert _order_type_code("limit") == "limit"
    assert _order_type_code("不下单") == ""
    assert _order_type_code("none") == ""
    assert _order_type_code(None) == "market"
    # Wording the model invented still opens, at market, as it did before.
    assert _order_type_code("随便看看") == "market"


def test_pending_entry_prices_are_validated_against_the_market() -> None:
    # A limit buy must sit below the ask; a limit sell above the bid.
    assert _resolve_entry_price(order_type="limit", direction="buy", market_entry=160.1, ai_entry=159.0)[1] == ""
    assert _resolve_entry_price(order_type="limit", direction="sell", market_entry=160.0, ai_entry=161.0)[1] == ""
    assert "不利一侧" in _resolve_entry_price(order_type="limit", direction="buy", market_entry=160.1, ai_entry=161.0)[1]
    assert "不利一侧" in _resolve_entry_price(order_type="limit", direction="sell", market_entry=160.0, ai_entry=159.0)[1]

    # A stop buy must sit above the ask; a stop sell below the bid.
    assert _resolve_entry_price(order_type="stop", direction="buy", market_entry=160.1, ai_entry=161.5)[1] == ""
    assert _resolve_entry_price(order_type="stop", direction="sell", market_entry=160.0, ai_entry=158.5)[1] == ""
    assert "尚未越过" in _resolve_entry_price(order_type="stop", direction="buy", market_entry=160.1, ai_entry=159.0)[1]

    # Market orders ignore any AI price.
    assert _resolve_entry_price(order_type="market", direction="buy", market_entry=160.1, ai_entry=1.0)[0] == 160.1
    # A pending order without a usable price is refused.
    assert "缺少有效价格" in _resolve_entry_price(order_type="limit", direction="buy", market_entry=160.1, ai_entry=None)[1]


# ── stage 1 gates ─────────────────────────────────────────────────────────────


def test_extreme_range_diagnosis_blocks_before_the_order_stage() -> None:
    ai = _TwoStageAi(
        {
            "should_open": True,
            "direction": "buy",
            "confidence": 0.9,
            "reason": "想开仓",
        },
        diagnosis={"cycle": "extreme_tr", "direction": "neutral", "gates": {}},
    )

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "极端震荡" in decision.reason
    assert ai.order_calls == 0, "a closed gate must not spend a second AI call"


def test_explicit_failed_gate_blocks_and_names_itself() -> None:
    ai = _TwoStageAi(
        {"should_open": True, "direction": "buy", "confidence": 0.9, "reason": "想开仓"},
        diagnosis={
            "cycle": "normal_channel",
            "direction": "bullish",
            "gates": {
                "gate2_direction_clear": {"passed": False, "reason": "背景与最近方向相反"},
            },
        },
    )

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "方向不明确" in decision.reason
    assert "背景与最近方向相反" in decision.reason
    assert ai.order_calls == 0


def test_missing_or_malformed_gates_never_block_trading() -> None:
    assert _blocking_gate_reason({"cycle": "normal_channel"}) == ""
    assert _blocking_gate_reason({"cycle": "normal_channel", "gates": None}) == ""
    assert _blocking_gate_reason({"cycle": "normal_channel", "gates": {"gate1_no_trade_environment": "yes"}}) == ""
    # Only an explicit boolean false closes a gate.
    assert _blocking_gate_reason(
        {"cycle": "normal_channel", "gates": {"gate1_no_trade_environment": {"passed": True}}}
    ) == ""
    assert _blocking_gate_reason(
        {"cycle": "normal_channel", "gates": {"gate1_no_trade_environment": {"passed": False, "reason": "铁丝网"}}}
    ) != ""


# ── stage 2 order decision ────────────────────────────────────────────────────


def test_limit_order_reaches_the_ea_with_the_ai_price() -> None:
    ai = _TwoStageAi({
        "order_type": "限价单",
        "direction": "buy",
        "entry_price": 158.0,
        "sl_price": 156.0,
        "tp_price": 165.0,
        "estimated_win_rate": 72,
        "should_open": True,
        "confidence": 0.5,
        "reason": "回踩限价做多",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "BUY"
    assert decision.entry == pytest.approx(158.0)
    assert decision.metadata["order_type"] == "limit"
    # 72% win rate drives confidence, overriding the model's own confidence field.
    assert decision.confidence == pytest.approx(0.72)
    assert "限价单" in decision.reason

    response = _mt5_open_response(decision, spread=0.1)
    assert response.should_open is True
    assert response.orders[0].order_type == "limit"
    assert response.orders[0].price == pytest.approx(158.0)
    assert response.orders[0].sl == pytest.approx(decision.sl)
    assert response.orders[0].tp == pytest.approx(decision.tp)


def test_breakout_order_on_the_wrong_side_is_refused() -> None:
    ai = _TwoStageAi({
        "order_type": "突破单",
        "direction": "buy",
        # A stop buy below the market can never trigger.
        "entry_price": 150.0,
        "should_open": True,
        "confidence": 0.9,
        "reason": "突破做多",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "尚未越过" in decision.reason


def test_explicit_no_order_holds_even_though_should_open_is_true() -> None:
    ai = _TwoStageAi({
        "order_type": "不下单",
        "should_open": True,
        "direction": "buy",
        "confidence": 0.9,
        "reason": "等待更好的位置",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "等待更好的位置" in decision.reason


def test_stop_distance_beyond_the_atr_cap_blocks_the_trade() -> None:
    """An outsized stop is skipped, not clamped: the stop bounds the loss."""
    from app.strategies.pa_agent_lite import MAX_STOP_ATR

    ai = _TwoStageAi({
        "order_type": "市价单",
        "direction": "buy",
        # 50x ATR away: not a structure stop, just an outsized risk.
        "sl_distance_price": 400.0,
        "should_open": True,
        "confidence": 0.9,
        "reason": "止损放得很远",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "止损距离过大" in decision.reason
    assert f"上限 {MAX_STOP_ATR:g} 倍" in decision.reason


def test_absolute_stop_price_beyond_the_cap_blocks_the_trade() -> None:
    """The cap must apply to the absolute price form too, not only the distance."""
    ai = _TwoStageAi({
        "order_type": "市价单",
        "direction": "buy",
        "entry_price": 160.1,
        "sl_price": 1.0,             # absurdly far below entry
        "should_open": True,
        "confidence": 0.9,
        "reason": "止损给错",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "止损距离过大" in decision.reason


def test_stop_within_the_cap_still_opens() -> None:
    """The cap must not block ordinary structure stops."""
    ai = _TwoStageAi({
        "order_type": "市价单",
        "direction": "buy",
        "sl_distance_price": 5.0,
        "should_open": True,
        "confidence": 0.9,
        "reason": "正常结构止损",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "BUY"
    assert decision.sl is not None and decision.sl < decision.entry


def test_stop_is_never_tightened_inside_the_structure_stop() -> None:
    request = _open_request()
    deployment = _deployment()
    local = PaAgentLiteStrategy().evaluate_open(request, deployment)
    ai = _TwoStageAi({
        "order_type": "市价单",
        "direction": "buy",
        "sl_distance_price": 0.01,   # far tighter than the structure stop
        "tp_distance_price": 0.01,
        "should_open": True,
        "confidence": 0.9,
        "reason": "确认多头候选",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(request, deployment)

    assert decision.action == "BUY"
    assert decision.sl == pytest.approx(local.sl)
    assert (decision.tp - decision.entry) / (decision.entry - decision.sl) >= 1.8 - 1e-9


def test_both_stages_are_reported_in_one_usage_summary() -> None:
    ai = _TwoStageAi({
        "order_type": "市价单",
        "direction": "buy",
        "should_open": True,
        "confidence": 0.9,
        "reason": "确认多头候选",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert ai.diagnosis_calls == 1
    assert ai.order_calls == 1
    assert decision.usage.input_tokens == 18
    assert decision.usage.output_tokens == 9
    assert decision.usage.charged_points == 27
    assert decision.usage.ai_called is True


def test_merge_usage_handles_cache_and_empty_sources() -> None:
    merged = _merge_usage(
        UsageSummary(ai_called=True, input_tokens=100, output_tokens=10, charged_points=110, cached_input_tokens=80),
        UsageSummary(ai_called=False),
    )
    assert merged.input_tokens == 100
    assert merged.cached_input_tokens == 80
    assert merged.ai_called is True
    assert _merge_usage().input_tokens == 0


def test_merge_usage_sums_the_time_of_both_stages() -> None:
    """The panel shows one figure, and it must be the whole analysis, not one stage."""
    merged = _merge_usage(
        UsageSummary(ai_called=True, input_tokens=100, output_tokens=10, elapsed_ms=1200),
        UsageSummary(ai_called=True, input_tokens=50, output_tokens=5, elapsed_ms=800),
    )

    assert merged.elapsed_ms == 2000


def test_diagnosis_selects_the_playbooks_sent_to_the_order_stage() -> None:
    ai = _TwoStageAi(
        {
            "order_type": "市价单",
            "direction": "buy",
            "should_open": True,
            "confidence": 0.9,
            "reason": "确认多头候选",
            "analysis": ANALYSIS,
        },
        diagnosis={"cycle": "trading_range", "direction": "bearish", "gates": {}},
    )

    PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert len(ai.strategy_texts) == 1
    assert "交易区间" in ai.strategy_texts[0]
    assert "空头方向附加规则" in ai.strategy_texts[0]


# ── knowledge routing ─────────────────────────────────────────────────────────


def test_knowledge_route_only_carries_the_selected_playbooks() -> None:
    text = pa_knowledge.route(cycle="normal_channel", direction="bullish", setup_codes=("breakout", "pullback"))

    assert "普通通道" in text
    assert "突破与突破回踩" in text
    assert "回调与二次入场" in text
    # Unselected cycle playbooks must not ride along. The stop-placement table in
    # the always-on rules may *mention* a cycle, so match the playbook heading.
    assert "## 极速行情" not in text
    assert "主要趋势反转" not in text


def test_always_on_blocks_are_present_for_every_route() -> None:
    text = pa_knowledge.route(cycle="trading_range", direction="bearish")

    assert "逐棒检查单" in text
    assert "信号棒判读" in text
    assert "下单与风控规则" in text


def test_knowledge_stats_keep_the_prompt_budget_bounded() -> None:
    stats = pa_knowledge.knowledge_stats()

    assert len(stats["cycles"]) == 8
    assert len(stats["setups"]) >= 10
    # The routed block stays small: this is what keeps the two-stage port cheap.
    assert stats["largest_route_chars"] < 4000
    assert stats["always_on_chars"] < 1500


def test_unknown_cycle_degrades_to_direction_rules_only() -> None:
    assert pa_knowledge.cycle_playbook("not_a_cycle") == ""
    assert pa_knowledge.direction_playbook("bullish")
    assert pa_knowledge.route(cycle="not_a_cycle", direction="bullish") != ""


def test_setup_codes_follow_the_detected_patterns() -> None:
    patterns = ("breakout_pullback", "barbwire", "mtr", "h2", "climax_warning")

    class _Features:
        detected_patterns = patterns
        setup_code = "breakout_retest_long"

    codes = _knowledge_setup_codes(_Features())

    assert codes[0] == "breakout", "the chosen candidate is injected first"
    assert codes[1] == "measured_move", "a breakout trade needs its target playbook"
    assert "pullback" in codes
    assert "barbwire" in codes
    assert "mtr" in codes


# ── AI output safety ──────────────────────────────────────────────────────────


def test_string_false_is_not_read_as_an_approval() -> None:
    """A model returning the string "false" must not open a trade."""
    ai = _TwoStageAi({
        "order_type": "市价单",
        "direction": "buy",
        "should_open": "false",     # the string, not the boolean
        "confidence": 0.9,
        "reason": "模型否决",
        "analysis": ANALYSIS,
    })

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"


def test_ai_boolean_parsing_covers_the_shapes_models_actually_emit() -> None:
    assert _ai_bool(True) is True
    assert _ai_bool("true") is True
    assert _ai_bool("是") is True
    assert _ai_bool(1) is True
    assert _ai_bool(False) is False
    assert _ai_bool("false") is False
    assert _ai_bool("False") is False
    assert _ai_bool("否") is False
    assert _ai_bool(0) is False
    assert _ai_bool(None) is False
    assert _ai_bool(None, True) is True
    # An unparseable value falls back to the caller's default, never to truthy.
    assert _ai_bool("maybe") is False
    assert _ai_bool("maybe", True) is True


def test_features_failure_reason_distinguishes_the_two_causes() -> None:
    too_few = _features_unavailable_reason(_trend_candles()[:20])
    assert "K 线少于 30 根" in too_few
    # Enough candles but no measurable volatility is a different problem.
    flat = [
        Candle(timestamp=1_700_000_000 + index * 300, open=100, high=100, low=100, close=100, volume=1)
        for index in range(40)
    ]
    no_volatility = _features_unavailable_reason(flat)
    assert "ATR 为 0" in no_volatility
    assert too_few != no_volatility


def _synthetic_candles(kind: str, *, count: int = 80, seed: int = 0) -> list[Candle]:
    rnd = random.Random(seed)
    candles: list[Candle] = []
    price = 100.0
    for index in range(count):
        if kind == "trend_up":
            delta = 1.0
        elif kind == "trend_dn":
            delta = -1.0
        elif kind == "spike":
            delta = 3.0 if index > count - 8 else 0.6
        elif kind == "spike_dn":
            delta = -3.0 if index > count - 8 else -0.6
        elif kind == "flat":
            delta = 0.0
        elif kind == "range":
            delta = 1.5 if (index // 5) % 2 == 0 else -1.5
        elif kind == "noise":
            delta = rnd.uniform(-1.0, 1.0)
        elif kind == "pullback":
            delta = 1.0 if index % 4 else -2.5
        elif kind == "gap":
            delta = 5.0 if index == count - 2 else 0.2
        else:
            delta = 0.3
        open_price = price
        close_price = price + delta
        wick = 0.9 if kind == "doji" else 0.5
        candles.append(Candle(
            timestamp=1_700_000_000 + index * 300,
            open=open_price,
            high=max(open_price, close_price) + wick,
            low=min(open_price, close_price) - wick,
            close=close_price,
            volume=100,
        ))
        price = close_price
    return candles


_MARKET_SHAPES = (
    "trend_up", "trend_dn", "spike", "spike_dn", "flat",
    "range", "noise", "pullback", "gap", "slow", "doji",
)


def test_panel_wording_never_leaks_internal_tokens() -> None:
    """No market shape may push a raw enum token onto the EA panel.

    This is the guard that catches a newly added enum value: it runs the real
    feature pipeline over synthetic markets instead of trusting the table.
    """
    from app.strategies.pa_agent_lite import _compute_features, _describe_hold, _describe_open, _trace_summary

    checked = 0
    for shape in _MARKET_SHAPES:
        for seed in range(4):
            features = _compute_features(_synthetic_candles(shape, seed=seed))
            if features is None:
                continue
            checked += 1
            texts = [
                _describe_hold(features),
                _describe_open(features, "BUY"),
                _trace_summary(features),
            ]
            for text in texts:
                assert "=" not in text, f"key=value dump on the panel ({shape}): {text}"
                assert "n/a" not in text
                for token in (
                    "insufficient", "triggered", "warning", "mixed", "aligned",
                    "neutral_background", "trading_range", "trending_tr", "spike",
                    "barbwire", "overlap", "middle_range", "trend_structure",
                    "breakout_", "climax_", "always_in", "HH_HL", "LL_LH",
                    "bullish", "bearish", "neutral", "upper", "lower", "middle",
                    "strong", "weak", "invalid", "pending", "stable", "transitioning",
                ):
                    assert token not in text, f"raw token {token} leaked ({shape}): {text}"
    assert checked > 20, "the guard must actually exercise the feature pipeline"


def test_every_panel_term_is_translated() -> None:
    """Guards against a new enum value reaching the EA in English."""
    from app.strategies.pa_agent_lite import _PA_TERMS, _SWING_TERMS

    for table in (_PA_TERMS, _SWING_TERMS):
        untranslated = [
            token for token, rendered in table.items()
            if rendered == token and any("a" <= ch.lower() <= "z" for ch in token)
        ]
        assert not untranslated, f"untranslated panel terms: {untranslated}"


def test_swing_structure_does_not_share_the_trend_rendering() -> None:
    """`mixed` means two different things; a shared key would misrender one."""
    from app.strategies.pa_agent_lite import _pa_text, _swing_text, _trend_text

    assert _pa_text("mixed") == "方向混合"
    assert _swing_text("mixed") == "高低点结构混合"
    assert _trend_text("mixed") == "背景与近期方向混合"
    assert _swing_text("insufficient") != "insufficient"
    assert _swing_text("something_new") == "高低点结构不明确"
    assert _trend_text("something_new") == "方向结构不明确"


def test_position_ai_cannot_act_on_a_ticket_that_is_not_in_the_request() -> None:
    from app.strategies.pa_agent_lite import PaAgentLiteStrategy as _Strategy
    from app.strategies.pa_agent_lite import _compute_features

    class _PositionAi:
        def pa_position_decision(self, **_: Any) -> AiCallResult:
            return AiCallResult(
                content={
                    "action": "close",
                    "ticket": "999999",          # not one of the open positions
                    "reason": "建议平仓",
                    "analysis": ANALYSIS,
                },
                usage=UsageSummary(ai_called=True, input_tokens=10, output_tokens=5),
            )

    request = _two_position_request()
    strategy = _Strategy(_PositionAi())
    features = _compute_features(request.candles)
    assert features is not None

    decision = strategy._evaluate_position_with_ai(request, _deployment(), features)

    assert decision is not None
    assert decision.action == "HOLD", "a hallucinated ticket must never be acted on"
    assert "999999" in decision.reason
    assert decision.position_ticket == "111", "the guard reports against the first real position"


def test_position_ai_may_target_any_ticket_that_is_supplied() -> None:
    from app.strategies.pa_agent_lite import PaAgentLiteStrategy as _Strategy
    from app.strategies.pa_agent_lite import _compute_features

    class _PositionAi:
        def pa_position_decision(self, **_: Any) -> AiCallResult:
            return AiCallResult(
                content={"action": "close", "ticket": "222", "reason": "建议平仓", "analysis": ANALYSIS},
                usage=UsageSummary(ai_called=True, input_tokens=10, output_tokens=5),
            )

    request = _two_position_request()
    features = _compute_features(request.candles)
    assert features is not None

    decision = _Strategy(_PositionAi())._evaluate_position_with_ai(request, _deployment(), features)

    assert decision is not None
    assert decision.action == "CLOSE"
    assert decision.position_ticket == "222"


# ── endpoint wiring ───────────────────────────────────────────────────────────


def test_diagnosis_stage_resolves_the_open_model_not_the_position_model(tmp_path) -> None:
    from app.services.ai_service import AiDecisionClient, _json_api_system_prompt
    from app.store import SqliteStore

    store = SqliteStore(tmp_path / "pa-diag-model.db")
    store.initialize()
    for endpoint_id in ("aie_open_model", "aie_position_model"):
        store.save_ai_endpoint({
            "id": endpoint_id,
            "owner_type": "gl",
            "name": endpoint_id,
            "base_url": "https://example.com/v1",
            "model": endpoint_id,
            "api_key": f"sk-{endpoint_id}",
        })
    client = AiDecisionClient(store)
    deployment = {
        "strategy_code": "PA_AGENT_V1",
        "strategy_name": "PA Agent",
        "config": {
            "open_ai_endpoint_id": "aie_open_model",
            "position_ai_endpoint_id": "aie_position_model",
        },
    }

    assert client._select_model(deployment, "open")["id"] == "aie_open_model"
    assert client._select_model(deployment, "pa_diag")["id"] == "aie_open_model"
    assert client._select_model(deployment, "position")["id"] == "aie_position_model"

    # The diagnosis prompt must ask for the cycle schema, never for an action.
    prompt = _json_api_system_prompt("pa_diag", "market_diagnosis")
    assert "cycle" in prompt
    assert "probabilities" in prompt
    assert "gate1_no_trade_environment" in prompt
    assert '"action"' not in prompt


def test_diagnosis_log_has_no_fields_the_model_never_wrote() -> None:
    """The call log is the customer's proof of what the AI said, so it must not
    be padded with placeholders the diagnosis stage never returns."""
    from app.services.ai_service import _extract_json_object

    content = json.dumps({
        "cycle": "normal_channel",
        "probabilities": {
            "spike": 5, "micro_channel": 5, "tight_channel": 10, "normal_channel": 40,
            "broad_channel": 15, "trending_tr": 10, "trading_range": 10, "extreme_tr": 5,
        },
        "direction": "bullish",
        "gates": {"gate1_no_trade_environment": {"passed": True, "reason": "有趋势"}},
        "reasoning": "回撤受限，节奏清晰，属于普通通道。",
    }, ensure_ascii=False)

    parsed = _extract_json_object(content, endpoint="pa_diag")

    assert parsed["cycle"] == "normal_channel"
    assert "reason" not in parsed, "the diagnosis stage must not gain a placeholder reason"
    assert "analysis" not in parsed


# ── stage 1 in the panel ──────────────────────────────────────────────────────


def test_order_panel_shows_the_diagnosis_between_the_verdict_and_the_analysis() -> None:
    ai = _TwoStageAi(
        {
            "order_type": "限价单",
            "direction": "buy",
            "entry_price": 158.0,
            "sl_price": 156.0,
            "tp_price": 165.0,
            "should_open": True,
            "reason": "回踩限价做多",
            "analysis": ANALYSIS,
        },
        diagnosis={"cycle": "normal_channel", "direction": "bullish", "gates": {}},
    )

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "BUY"
    parts = decision.reason.split("；")
    assert parts[0] == "限价单"
    assert parts[1] == "回踩限价做多"
    assert parts[2] == "阶段一：普通通道 / 看多"
    assert parts[3].startswith("AI 分析：")
    assert ANALYSIS in decision.reason


def test_hold_panel_also_carries_the_diagnosis() -> None:
    ai = _TwoStageAi(
        {
            "order_type": "不下单",
            "should_open": True,
            "direction": "buy",
            "reason": "等待更好的位置",
            "analysis": ANALYSIS,
        },
        diagnosis={"cycle": "trading_range", "direction": "neutral", "gates": {}},
    )

    decision = PaAgentLiteStrategy(ai).evaluate_open(_open_request(), _deployment())

    assert decision.action == "HOLD"
    assert "等待更好的位置" in decision.reason
    assert "阶段一：交易区间 / 中性" in decision.reason
    assert ANALYSIS in decision.reason


def test_stage1_panel_text_degrades_gracefully() -> None:
    from app.strategies.pa_agent_lite import _stage1_panel_text

    assert _stage1_panel_text(None) == ""
    assert _stage1_panel_text({}) == ""
    assert _stage1_panel_text({"cycle": "spike", "direction": None}) == "阶段一：极速行情"
    assert _stage1_panel_text({"cycle": None, "direction": "bearish"}) == "阶段一：看空"
    # An invented cycle name must not leak into the panel as a raw token.
    assert _stage1_panel_text({"cycle": "some_new_state"}) == ""


def test_every_cycle_state_has_a_panel_label() -> None:
    """A playbook without a label would silently vanish from the EA panel."""
    for cycle in pa_knowledge.CYCLE_PLAYBOOKS:
        assert pa_knowledge.cycle_label(cycle), f"{cycle} has no Chinese label"
    for direction in pa_knowledge.DIRECTION_PLAYBOOKS:
        assert pa_knowledge.direction_label(direction), f"{direction} has no Chinese label"


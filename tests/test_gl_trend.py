"""Regression tests for the GL_TREND_V1 strategy.

Covers the basket-wide exits, the strategy-level unit cap, the two entry
systems running in parallel, the break-even spread buffer, and the AI risk
gates for entries and add-ons.
"""

from __future__ import annotations

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
from app.services.ai_service import AiCallResult
from app.strategies import turtle_agent
from app.strategies.turtle_agent import MAX_UNITS, TurtleTrendStrategy, _max_units

# Flat 10-wide bars: ATR(20) == 10.0, channel low/high over the exit window are
# 100 / 110 because the window excludes the newest (still forming) bar.
_PRICE_LOW = 100.0
_PRICE_HIGH = 110.0


def _candles(count: int = 25) -> list[Candle]:
    return [
        Candle(
            timestamp=1_700_000_000 + index * 900,
            open=105.0,
            high=_PRICE_HIGH,
            low=_PRICE_LOW,
            close=105.0,
            volume=1.0,
        )
        for index in range(count)
    ]


def _position(
    ticket: str,
    *,
    side: str = "BUY",
    open_price: float = 105.0,
    current_price: float = 106.0,
    volume: float = 0.1,
    open_time: int | None = None,
) -> PositionSnapshot:
    return PositionSnapshot(
        ticket=ticket,
        symbol="XAUUSD",
        side=side,
        volume=volume,
        open_price=open_price,
        current_price=current_price,
        profit=0.0,
        open_time=open_time,
    )


def _position_request(
    positions: list[PositionSnapshot],
    *,
    bid: float = 106.0,
    ask: float = 106.1,
) -> PositionEvaluateRequest:
    return PositionEvaluateRequest(
        deployment_key="gl_test_key",
        request_id="gl-position-request-001",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M15",
        bar_time=1_700_000_000 + 25 * 900,
        bid=bid,
        ask=ask,
        spread_points=1.0,
        candles=_candles(),
        positions=positions,
    )


def test_effective_unit_limit_never_exceeds_strategy_cap() -> None:
    assert MAX_UNITS == 4
    assert _max_units({}) == MAX_UNITS
    assert _max_units({"max_positions": 2}) == 2
    # A user may lower the limit, never raise it above the strategy's design.
    assert _max_units({"max_positions": 6}) == MAX_UNITS
    # A non-positive or malformed value falls back to the strategy cap.
    assert _max_units({"max_positions": 0}) == MAX_UNITS
    assert _max_units({"max_positions": "abc"}) == MAX_UNITS


def test_channel_exit_closes_the_whole_basket() -> None:
    """One unit breaking the channel closes every unit, not just one."""
    request = _position_request(
        [
            _position("1001", current_price=106.0),
            _position("1002", current_price=99.0),
        ]
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": {}})

    assert decision.action == "CLOSE"
    assert "趋势转弱" in decision.reason
    closes = decision.metadata["batch_actions"]
    assert [item["action"] for item in closes] == ["close", "close"]
    assert [item["ticket"] for item in closes] == ["1001", "1002"]


def test_unified_stop_closes_the_whole_basket() -> None:
    """The 2-ATR stop is one level for the basket, anchored at the furthest entry."""
    request = _position_request(
        [
            _position("2001", current_price=104.0),
            # open 125 is the furthest entry, so the basket stop sits at 125 - 2*ATR.
            _position("2002", open_price=125.0, current_price=104.0),
        ]
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": {}})

    assert decision.action == "CLOSE"
    assert "触发保护止损" in decision.reason
    closes = decision.metadata["batch_actions"]
    assert [item["action"] for item in closes] == ["close", "close"]
    assert [item["ticket"] for item in closes] == ["2001", "2002"]


def test_channel_exit_closes_the_whole_basket_for_sell_side() -> None:
    request = _position_request(
        [
            _position("3001", side="SELL", current_price=104.0),
            _position("3002", side="SELL", current_price=111.0),
        ]
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": {}})

    assert decision.action == "CLOSE"
    assert "趋势转强" in decision.reason
    closes = decision.metadata["batch_actions"]
    assert [item["ticket"] for item in closes] == ["3001", "3002"]


def test_add_is_refused_once_the_strategy_cap_is_reached() -> None:
    """A user setting above MAX_UNITS must not raise the effective cap."""
    config = {
        "allow_add": True,
        "max_positions": 6,
        "position_size_mode": "fixed",
        "fixed_volume": 0.01,
        "add_step_atr": 0.5,
    }
    request = _position_request(
        [_position(f"40{index}", current_price=111.0) for index in range(MAX_UNITS)],
        bid=111.0,
        ask=111.1,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": config})

    # 4 units open == the strategy cap, so the add-on extension must be refused.
    assert decision.action == "HOLD"


def test_add_is_refused_when_the_user_lowered_the_limit() -> None:
    config = {
        "allow_add": True,
        "max_positions": 2,
        "position_size_mode": "fixed",
        "fixed_volume": 0.01,
        "add_step_atr": 0.5,
    }
    request = _position_request(
        [_position(f"50{index}", current_price=111.0) for index in range(2)],
        bid=111.0,
        ask=111.1,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": config})

    assert decision.action == "HOLD"


def test_add_below_the_cap_uses_the_user_limit() -> None:
    config = {
        "allow_add": True,
        "max_positions": 2,
        "position_size_mode": "fixed",
        "fixed_volume": 0.01,
        "add_step_atr": 0.5,
    }
    request = _position_request(
        [_position("6001", current_price=111.0)],
        bid=111.0,
        ask=111.1,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": config})

    assert decision.action == "BUY"
    assert decision.lot == 0.01


def _open_request(
    *,
    last_close: float,
    last_high: float | None = None,
    last_low: float | None = None,
    count: int = 35,
) -> OpenEvaluateRequest:
    """Open request whose final (forming) bar carries the given close."""
    candles = _candles(count)[:-1] + [
        Candle(
            timestamp=1_700_000_000 + (count - 1) * 900,
            open=105.0,
            high=last_high if last_high is not None else max(last_close, 105.0),
            low=last_low if last_low is not None else min(last_close, 105.0),
            close=last_close,
            volume=1.0,
        )
    ]
    return OpenEvaluateRequest(
        deployment_key="gl_test_key",
        request_id="gl-open-request-001",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M15",
        bar_time=1_700_000_000 + count * 900,
        bid=last_close - 0.05,
        ask=last_close + 0.05,
        spread_points=1.0,
        candles=candles,
        balance=10_000.0,
        equity=10_000.0,
    )


def test_add_moves_every_unit_onto_the_new_unified_stop() -> None:
    config = {
        "allow_add": True,
        "max_positions": 4,
        "position_size_mode": "fixed",
        "fixed_volume": 0.01,
        "add_step_atr": 0.5,
    }
    request = _position_request(
        [_position("6001", current_price=111.0)],
        bid=111.0,
        ask=111.1,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": config})

    assert decision.action == "BUY"
    assert decision.lot == 0.01
    unified_stop = decision.metadata["unified_stop"]
    assert unified_stop == pytest.approx(decision.sl)
    actions = decision.metadata["batch_actions"]
    assert actions[0]["action"] == "add"
    assert actions[0]["direction"] == "buy"
    # The existing unit is moved onto the same level in the same response.
    assert actions[1] == {
        "action": "modify",
        "ticket": "6001",
        "sl": unified_stop,
        "tp": None,
        "comment": "加仓后统一保护止损",
    }


def test_add_does_not_loosen_an_already_tighter_stop() -> None:
    config = {
        "allow_add": True,
        "max_positions": 4,
        "position_size_mode": "fixed",
        "fixed_volume": 0.01,
        "add_step_atr": 0.5,
    }
    # This unit already sits above the level the new add would impose.
    protected = _position("6101", current_price=111.0)
    protected.sl = 110.0
    request = _position_request([protected], bid=111.0, ask=111.1)

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": config})

    assert decision.action == "BUY"
    actions = decision.metadata["batch_actions"]
    assert [item["action"] for item in actions] == ["add"]


def test_router_expands_a_basket_close_into_one_action_per_ticket() -> None:
    """The EA receives one close action per ticket of a basket exit."""
    from app.api.router import _mt5_position_response
    from app.models import Mt5Position

    decision = TurtleTrendStrategy().evaluate_position(
        _position_request(
            [
                _position("8001", current_price=99.0),
                _position("8002", current_price=99.0, volume=0.2),
            ]
        ),
        {"config": {}},
    )
    response = _mt5_position_response(
        decision,
        spread=1.0,
        positions=[
            Mt5Position(ticket="8001", symbol="XAUUSD", mt_type=0, volume=0.1, open_price=105.0),
            Mt5Position(ticket="8002", symbol="XAUUSD", mt_type=0, volume=0.2, open_price=105.0),
        ],
    )

    assert response.has_action is True
    assert [action.action for action in response.actions] == ["close", "close"]
    assert [action.ticket for action in response.actions] == ["8001", "8002"]
    assert [action.volume for action in response.actions] == [0.1, 0.2]


def test_channel_breakout_opens_and_records_the_channel_signal() -> None:
    request = _open_request(last_close=112.5, last_high=113.0, last_low=104.0)

    decision = TurtleTrendStrategy().evaluate_open(
        request, {"config": {"entry_period": 20, "atr_period": 20}}
    )

    assert decision.action == "BUY"
    assert decision.sl is not None and decision.sl < decision.entry
    assert decision.tp is None
    assert decision.metadata["donchian_direction"] == "buy"
    assert decision.metadata["swing_direction"] == ""


def test_conflicting_entry_signals_stand_aside(monkeypatch) -> None:
    """A swing signal fighting the channel signal must not place an order."""
    monkeypatch.setattr(
        turtle_agent,
        "_swing_pullback_signal",
        lambda candles, atr, config: ("buy", "synthetic swing buy"),
    )
    # Closing below the 20-bar channel low makes the channel system say "sell".
    request = _open_request(last_close=99.0)

    decision = TurtleTrendStrategy().evaluate_open(request, {"config": {}})

    assert decision.action == "HOLD"
    assert "冲突" in decision.reason
    assert "回调结构偏做多" in decision.reason
    assert "区间突破偏做空" in decision.reason


def test_break_even_clears_the_spread() -> None:
    """The break-even stop must sit beyond entry by at least the spread."""
    request = _position_request(
        [_position("7001", current_price=116.0)],
        bid=116.0,
        ask=116.2,  # spread 0.2 -> offset must clear it
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": {}})

    assert decision.action == "MODIFY_SL"
    # entry 105 + 0.2 * BREAK_EVEN_SPREAD_BUFFER
    assert decision.sl == pytest.approx(105.0 + 0.2 * 1.5)
    # The batch wrapper only forwards ticket/sl/tp/comment, so the applied
    # offset is visible through the action comment.
    assert "保本+0.3" in decision.metadata["batch_actions"][0]["comment"]


def test_larger_configured_break_even_offset_wins() -> None:
    request = _position_request(
        [_position("7002", current_price=116.0)],
        bid=116.0,
        ask=116.2,
    )

    decision = TurtleTrendStrategy().evaluate_position(
        request, {"config": {"break_even_offset": 2.0}}
    )

    assert decision.action == "MODIFY_SL"
    assert decision.sl == pytest.approx(107.0)


def test_break_even_fires_for_sell_side_clearing_the_spread() -> None:
    request = _position_request(
        [
            _position(
                "7003",
                side="SELL",
                open_price=115.0,
                current_price=104.0,
            )
        ],
        bid=103.8,
        ask=104.0,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": {}})

    assert decision.action == "MODIFY_SL"
    assert decision.sl == pytest.approx(115.0 - 0.2 * 1.5)


def test_stop_distance_follows_the_module_constant(monkeypatch) -> None:
    """The protective stop must come from STOP_ATR, not a stray literal."""
    request = _open_request(last_close=112.5, last_high=113.0, last_low=104.0)
    config = {"entry_period": 20, "atr_period": 20}

    default_stop = TurtleTrendStrategy().evaluate_open(request, {"config": config})
    monkeypatch.setattr(turtle_agent, "STOP_ATR", 1.0)
    halved_stop = TurtleTrendStrategy().evaluate_open(request, {"config": config})

    assert default_stop.sl is not None and halved_stop.sl is not None
    distance_default = default_stop.entry - default_stop.sl
    distance_halved = halved_stop.entry - halved_stop.sl
    assert distance_halved == pytest.approx(distance_default / 2)


def test_matching_entry_signals_still_open(monkeypatch) -> None:
    """Agreement between both systems keeps the normal behaviour."""
    monkeypatch.setattr(
        turtle_agent,
        "_swing_pullback_signal",
        lambda candles, atr, config: ("buy", "synthetic swing buy"),
    )
    request = _open_request(last_close=112.5, last_high=113.0, last_low=104.0)

    decision = TurtleTrendStrategy().evaluate_open(request, {"config": {}})

    assert decision.action == "BUY"
    assert decision.metadata["swing_direction"] == "buy"
    assert decision.metadata["donchian_direction"] == "buy"


class _FakeRiskGate:
    """Canned AI verdicts for the entry gate and the position review."""

    def __init__(
        self,
        *,
        open_content: dict[str, Any] | None = None,
        position_content: dict[str, Any] | None = None,
        fail: bool = False,
    ) -> None:
        self.open_content = open_content
        self.position_content = position_content
        self.fail = fail
        self.open_calls = 0
        self.position_calls = 0
        self.last_position_signal: dict[str, Any] = {}

    def turtle_open_risk_decision(self, **_: Any) -> AiCallResult | None:
        self.open_calls += 1
        if self.fail:
            return None
        return AiCallResult(content=self.open_content or {}, usage=UsageSummary(ai_called=True))

    def turtle_position_review(self, **kwargs: Any) -> AiCallResult | None:
        self.position_calls += 1
        self.last_position_signal = kwargs.get("signal") or {}
        if self.fail:
            return None
        return AiCallResult(content=self.position_content or {}, usage=UsageSummary(ai_called=True))


_ADD_CONFIG = {
    "allow_add": True,
    "max_positions": 4,
    "position_size_mode": "fixed",
    "fixed_volume": 0.01,
    "add_step_atr": 0.5,
}


def _breakout_request() -> OpenEvaluateRequest:
    return _open_request(last_close=112.5, last_high=113.0, last_low=104.0)


def _add_ready_request() -> PositionEvaluateRequest:
    return _position_request([_position("9001", current_price=111.0)], bid=111.0, ask=111.1)


def test_open_ai_approval_keeps_the_order() -> None:
    gate = _FakeRiskGate(
        open_content={"allow_open": True, "risk_level": "low", "reason": "结构干净"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert gate.open_calls == 1
    assert "AI 风险评估通过" in decision.metadata["ai_risk"]
    # The AI verdict is visible on the EA panel too, not only in metadata.
    assert "突破趋势确认" in decision.reason
    assert "AI 风险评估通过（风险正常）：结构干净" in decision.reason
    assert decision.usage.ai_called is True


def test_open_ai_cautious_verdict_is_reported_on_the_panel() -> None:
    gate = _FakeRiskGate(
        open_content={"allow_open": False, "risk_level": "medium", "reason": "上影线偏长"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert "AI 提示谨慎（中等）" in decision.reason
    assert "上影线偏长" in decision.reason


def test_open_ai_high_risk_veto_blocks_the_order() -> None:
    gate = _FakeRiskGate(
        open_content={"allow_open": False, "risk_level": "high", "reason": "过度延伸"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "HOLD"
    assert "过度延伸" in decision.reason


def test_open_ai_mild_rejection_is_overridden() -> None:
    """A model 'no' that is not high risk must not veto a qualified entry."""
    gate = _FakeRiskGate(
        open_content={"allow_open": False, "risk_level": "medium", "reason": "趋势不明"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert "未判定高风险" in decision.metadata["ai_risk"]


def test_open_ai_text_false_is_not_treated_as_approval() -> None:
    """The string "false" must not pass the gate as an approval."""
    gate = _FakeRiskGate(
        open_content={"allow_open": "false", "risk_level": "low", "reason": "文本布尔"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert "未判定高风险" in decision.metadata["ai_risk"]


def test_open_ai_failure_still_opens() -> None:
    gate = _FakeRiskGate(fail=True)

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert "AI 未返回结果" in decision.metadata["ai_risk"]


def test_open_ai_cannot_reshape_the_order() -> None:
    gate = _FakeRiskGate(
        open_content={
            "allow_open": True,
            "risk_level": "low",
            "direction": "sell",
            "entry": 999.0,
            "sl": 1.0,
            "lot": 5.0,
            "reason": "模型试图改单",
        }
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert decision.lot == 0.01
    assert decision.sl is not None and decision.sl < decision.entry


def test_add_ai_mild_rejection_is_overridden() -> None:
    gate = _FakeRiskGate(
        position_content={"close_now": False, "allow_add": False, "risk_level": "medium", "reason": "已延伸"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_position(
        _add_ready_request(), {"config": _ADD_CONFIG}
    )

    assert decision.action == "BUY"
    assert "未判定高风险" in decision.metadata["ai_risk"]


def test_add_ai_high_risk_blocks() -> None:
    gate = _FakeRiskGate(
        position_content={
            "close_now": False,
            "allow_add": False,
            "risk_level": "high",
            "reason": "climax risk",
        }
    )

    decision = TurtleTrendStrategy(gate).evaluate_position(
        _add_ready_request(), {"config": _ADD_CONFIG}
    )

    assert decision.action == "HOLD"
    assert "climax risk" in decision.reason


def test_ai_proactive_close_closes_the_whole_basket() -> None:
    gate = _FakeRiskGate(
        position_content={"close_now": True, "allow_add": True, "risk_level": "high", "reason": "趋势转弱"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_position(
        _add_ready_request(), {"config": _ADD_CONFIG}
    )

    assert decision.action == "CLOSE"
    assert "止盈离场" in decision.reason
    assert "趋势转弱" in decision.reason
    assert [item["action"] for item in decision.metadata["batch_actions"]] == ["close"]
    assert decision.usage.ai_called is True


def test_ai_proactive_close_waits_for_one_closed_bar() -> None:
    """A basket opened on the newest bar is never closed proactively."""
    newest_bar = 1_700_000_000 + 34 * 900
    gate = _FakeRiskGate(
        position_content={"close_now": True, "allow_add": True, "risk_level": "high", "reason": "刚开就反转"}
    )
    request = _position_request(
        [_position("9101", current_price=106.0, open_time=newest_bar)],
        bid=106.0,
        ask=106.2,
    )

    decision = TurtleTrendStrategy(gate).evaluate_position(request, {"config": {}})

    assert decision.action == "HOLD"
    assert "本根不提前离场" in decision.metadata["ai_risk"]


def test_position_review_runs_on_every_poll() -> None:
    """The AI is consulted even when there is no add-on candidate."""
    gate = _FakeRiskGate(
        position_content={"close_now": False, "allow_add": True, "risk_level": "low", "reason": "继续持有"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_position(
        _position_request([_position("9201", current_price=106.0)], bid=106.0, ask=106.2),
        {"config": {}},
    )

    assert gate.position_calls == 1
    assert decision.action == "HOLD"
    assert "AI 风险评估通过" in decision.metadata["ai_risk"]


def test_position_review_sends_the_context_the_ai_needs() -> None:
    """The AI must see how far the trade has run and what the server keeps in force."""
    gate = _FakeRiskGate(
        position_content={"close_now": False, "allow_add": True, "risk_level": "low", "reason": "ok"}
    )

    TurtleTrendStrategy(gate).evaluate_position(_add_ready_request(), {"config": _ADD_CONFIG})

    signal = gate.last_position_signal
    assert signal["protective_stop_levels"]["BUY"] == pytest.approx(105.0 - 2 * signal["atr"])
    summary = signal["position_summary"][0]
    assert summary["favorable_atr"] > 0  # scale-free "how far has it run" figure
    assert summary["bars_since_open"] == -1  # open_time unknown in this fixture
    assert signal["add_candidate"]["direction"] == "buy"
    assert signal["add_candidate"]["units_open"] == 1
    # No add-on candidate => the AI is still asked about taking profit.
    gate.position_calls = 0
    TurtleTrendStrategy(gate).evaluate_position(
        _position_request([_position("9501", current_price=106.0)], bid=106.0, ask=106.2),
        {"config": {}},
    )
    assert gate.position_calls == 1
    assert gate.last_position_signal["add_candidate"] is None


def test_protection_still_runs_after_the_ai_review() -> None:
    """The AI is asked even when a break-even/trailing update is due."""
    gate = _FakeRiskGate(
        position_content={"close_now": False, "allow_add": True, "risk_level": "low", "reason": "继续持有"}
    )
    request = _position_request([_position("9301", current_price=116.0)], bid=116.0, ask=116.2)

    decision = TurtleTrendStrategy(gate).evaluate_position(request, {"config": {}})

    assert gate.position_calls == 1  # profit-taking was checked
    assert decision.action == "MODIFY_SL"  # the strategy still moves the stop
    assert decision.usage.ai_called is True


def test_ai_profit_taking_wins_over_a_pending_protection_update() -> None:
    gate = _FakeRiskGate(
        position_content={"close_now": True, "allow_add": True, "risk_level": "high", "reason": "涨够了"}
    )
    request = _position_request([_position("9302", current_price=116.0)], bid=116.0, ask=116.2)

    decision = TurtleTrendStrategy(gate).evaluate_position(request, {"config": {}})

    assert decision.action == "CLOSE"
    assert "涨够了" in decision.reason
    assert [item["action"] for item in decision.metadata["batch_actions"]] == ["close"]


# ── the AI's full analysis must reach the EA panel ────────────────────────────

_FULL_ANALYSIS = (
    "市场结构呈窄幅震荡，价格在区间内波动，无突破性高低点；两仓均小幅浮亏，"
    "但亏损幅度很小；近期K线多空动能接近平衡，未形成看跌延续或看涨反转的组合；"
    "ATR显示当前波动率中性；保护性止损仍远于市价，风险可控。"
)


def test_position_panel_shows_the_full_analysis_not_only_the_short_reason() -> None:
    gate = _FakeRiskGate(position_content={
        "close_now": False,
        "allow_add": True,
        "risk_level": "low",
        "reason": "未明确反转信号",
        "analysis": _FULL_ANALYSIS,
    })

    decision = TurtleTrendStrategy(gate).evaluate_position(
        _position_request([_position("9401", current_price=105.0)], bid=105.0, ask=105.2),
        {"config": {}},
    )

    assert "趋势结构未被破坏，继续持有" in decision.reason
    assert "AI 分析：" in decision.reason
    assert _FULL_ANALYSIS in decision.reason


def test_open_panel_shows_the_full_analysis_when_the_entry_is_approved() -> None:
    gate = _FakeRiskGate(open_content={
        "allow_open": True,
        "risk_level": "low",
        "reason": "结构干净",
        "analysis": _FULL_ANALYSIS,
    })

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert "AI 风险评估通过（风险正常）" in decision.reason
    assert _FULL_ANALYSIS in decision.reason


def test_proactive_exit_panel_shows_the_full_analysis() -> None:
    gate = _FakeRiskGate(position_content={
        "close_now": True,
        "allow_add": True,
        "risk_level": "high",
        "reason": "涨够了",
        "analysis": _FULL_ANALYSIS,
    })

    decision = TurtleTrendStrategy(gate).evaluate_position(
        _position_request([_position("9402", current_price=116.0)], bid=116.0, ask=116.2),
        {"config": {}},
    )

    assert decision.action == "CLOSE"
    assert "止盈离场" in decision.reason
    assert _FULL_ANALYSIS in decision.reason


def test_panel_falls_back_to_the_short_reason_when_no_analysis_is_returned() -> None:
    """Older verdicts carry only `reason`; they must keep working."""
    gate = _FakeRiskGate(position_content={
        "close_now": False,
        "allow_add": True,
        "risk_level": "low",
        "reason": "未明确反转信号",
    })

    decision = TurtleTrendStrategy(gate).evaluate_position(
        _position_request([_position("9403", current_price=105.0)], bid=105.0, ask=105.2),
        {"config": {}},
    )

    assert "AI 分析：未明确反转信号" in decision.reason


def test_analysis_is_not_duplicated_when_the_reason_already_contains_it() -> None:
    from app.strategies.turtle_agent import _with_ai_analysis

    assert _with_ai_analysis("继续持有", {"reason": "", "analysis": ""}) == "继续持有"
    # The reason already being the analysis must not append it a second time.
    text = _with_ai_analysis("继续持有；AI 分析：结构完好", {"analysis": "结构完好"})
    assert text == "继续持有；AI 分析：结构完好"


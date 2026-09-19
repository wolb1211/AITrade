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


def _legacy(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The protection thresholds these tests were written against.

    Break-even and the trailing start are deployment settings now, and their
    defaults were tightened once the entry fixes were in. A test that exercises
    the add rule, the break-even rule or the trailing rule should not start
    behaving differently because a default moved, so it pins the values it means.
    """
    return {
        "break_even_atr": 1.0,
        "trailing_start_atr": 1.5,
        "trailing_distance_atr": 1.0,
        **(config or {}),
    }


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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy()})

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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy()})

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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy()})

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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

    assert decision.action == "BUY"
    actions = decision.metadata["batch_actions"]
    assert [item["action"] for item in actions] == ["add"]


def test_add_wins_over_a_simultaneous_trailing_update() -> None:
    """The add and the protection ladder compete for the same bar.

    A bar that prints a new high also makes the trailing target tighter, so
    returning the protection update first skipped the add on every advancing bar
    - exactly the bars a pyramid is meant to fire on - and the strategy could
    rarely reach its unit cap.
    """
    config = {
        "allow_add": True,
        "max_positions": 4,
        "position_size_mode": "fixed",
        "fixed_volume": 0.01,
        "add_step_atr": 0.5,
    }
    # open 105, ATR 10: a bid of 120 is +1.5 ATR (trailing due) and also well
    # past the 0.5 ATR add step, so both candidate actions exist on this bar.
    request = _position_request(
        [_position("6201", current_price=120.0)],
        bid=120.0,
        ask=120.1,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

    assert decision.action == "BUY", "the add must win over the trailing update"
    assert decision.metadata["batch_actions"][0]["action"] == "add"


def test_protection_still_fires_when_no_add_candidate_exists() -> None:
    """Reordering must not disable the ladder once the unit cap is reached."""
    config = {
        "allow_add": True,
        "max_positions": 1,
        "position_size_mode": "fixed",
        "fixed_volume": 0.01,
        "add_step_atr": 0.5,
    }
    request = _position_request(
        [_position("6202", current_price=120.0)],
        bid=120.0,
        ask=120.1,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

    assert decision.action == "MODIFY_SL"
    assert decision.sl == pytest.approx(110.0)


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
        lambda candles, atr, config, engulf_tolerance=0.0: ("buy", "synthetic swing buy"),
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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy()})

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
        request, {"config": _legacy({"break_even_offset": 2.0})}
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

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy()})

    assert decision.action == "MODIFY_SL"
    assert decision.sl == pytest.approx(115.0 - 0.2 * 1.5)


def test_stop_distance_follows_the_module_constant(monkeypatch) -> None:
    """The protective stop must come from STOP_ATR, not a stray literal."""
    request = _open_request(last_close=112.5, last_high=113.0, last_low=104.0)
    config = {"entry_period": 20, "atr_period": 20}

    default_stop = TurtleTrendStrategy().evaluate_open(request, {"config": _legacy(config)})
    monkeypatch.setattr(turtle_agent, "STOP_ATR", 1.0)
    halved_stop = TurtleTrendStrategy().evaluate_open(request, {"config": _legacy(config)})

    assert default_stop.sl is not None and halved_stop.sl is not None
    distance_default = default_stop.entry - default_stop.sl
    distance_halved = halved_stop.entry - halved_stop.sl
    assert distance_halved == pytest.approx(distance_default / 2)


def test_matching_entry_signals_still_open(monkeypatch) -> None:
    """Agreement between both systems keeps the normal behaviour."""
    monkeypatch.setattr(
        turtle_agent,
        "_swing_pullback_signal",
        lambda candles, atr, config, engulf_tolerance=0.0: ("buy", "synthetic swing buy"),
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
    """A level without an explicit no keeps the lenient wording."""
    gate = _FakeRiskGate(
        open_content={"risk_level": "medium", "reason": "上影线偏长"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert "AI 持保留意见（风险中等）" in decision.reason
    assert "上影线偏长" in decision.reason


def test_open_ai_high_risk_veto_blocks_the_order() -> None:
    gate = _FakeRiskGate(
        open_content={"allow_open": False, "risk_level": "high", "reason": "过度延伸"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "HOLD"
    assert "过度延伸" in decision.reason


def test_open_ai_explicit_rejection_blocks_the_entry() -> None:
    """A model that answers no is obeyed, whatever level it reports.

    Only a high level used to veto, so an answer that objected while rating the
    risk lower still opened - the combination customers asked about. A level on
    its own, with no approval flag, keeps the lenient wording instead.
    """
    gate = _FakeRiskGate(
        open_content={"allow_open": False, "risk_level": "medium", "reason": "趋势不明"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "HOLD"
    assert "本次不开仓" in decision.reason


def test_open_ai_text_false_is_not_treated_as_approval() -> None:
    """The string "false" must be read as a no, not as an approval."""
    gate = _FakeRiskGate(
        open_content={"allow_open": "false", "risk_level": "low", "reason": "文本布尔"}
    )

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "HOLD"
    assert "本次不开仓" in decision.reason


def test_open_ai_failure_holds_unless_the_deployment_says_otherwise() -> None:
    """The gate is the risk check, so a provider outage defaults to no entry.

    It used to fall through and open on the local rules alone, which is the one
    outcome a risk gate must not produce. A deployment that would rather keep
    trading can set ai_gate_fail_open.
    """
    gate = _FakeRiskGate(fail=True)

    held = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})
    assert held.action == "HOLD"
    assert "保守不开仓" in held.reason

    opened = TurtleTrendStrategy(gate).evaluate_open(
        _breakout_request(), {"config": {"ai_gate_fail_open": True}}
    )
    assert opened.action == "BUY"
    assert "放行" in opened.metadata["ai_risk"]


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


def test_risk_per_lot_skips_a_branch_that_cannot_be_right() -> None:
    """A partial symbol_info must never size an order from a nonsense figure.

    Production: the deployment's size mode was risk, so the per-lot risk had to
    be about 1438 for a 14.38 move, but it came out at 2.16 and the add-on was
    sized at 46.2 lots where roughly 0.07 was intended.
    """
    price_risk = 14.38
    broken_tick = {"tick_size": 0.01, "tick_value": 0.0015, "contract_size": 100}

    # The broken tick figure is ignored and the contract size carries the sizing.
    value, source, _spread = turtle_agent._risk_per_lot(price_risk, broken_tick)
    assert value == pytest.approx(1438.0)
    assert source == "contract_size"
    # A healthy payload still uses the tick branch, unchanged.
    healthy = {"tick_size": 0.01, "tick_value": 1.0, "contract_size": 100}
    value, source, _spread = turtle_agent._risk_per_lot(price_risk, healthy)
    assert value == pytest.approx(1438.0)
    assert source == "tick_size"
    # Nothing usable: refuse to size rather than guess.
    assert turtle_agent._risk_per_lot(price_risk, {"tick_size": 0.01, "tick_value": 0.0015})[0] == 0.0
    assert turtle_agent._risk_per_lot(0.0, healthy)[0] == 0.0


def test_unit_lot_stays_sane_when_the_tick_figure_is_broken() -> None:
    """The 46-lot add-on: the same payload must now size about 0.07 lots."""
    from types import SimpleNamespace

    config = {
        "position_size_mode": "risk",
        "risk_base_mode": "fixed_loss",
        "risk_amount": 100,
    }
    request = SimpleNamespace(
        symbol_info={
            "tick_size": 0.01,
            "tick_value": 0.0015,
            "contract_size": 100,
            "volume_min": 0.01,
            "volume_step": 0.01,
        },
        balance=10_000.0,
        equity=10_000.0,
    )

    lot, sizing = turtle_agent._unit_lot(
        request, config, entry=4316.05, stop_loss=4301.67,
    )

    assert sizing["mode"] == "risk"
    assert sizing["risk_source"] == "contract_size"
    assert sizing["risk_per_lot"] == pytest.approx(1438.0)
    assert lot == pytest.approx(0.07, abs=0.005)


def test_a_wide_spread_between_risk_figures_is_conservative_not_fatal() -> None:
    """Figures that disagree must not block trading; the larger one wins.

    The same lot size can show tens of one currency on BTC and cents on ETH, and
    a broker quoting in another currency or an EA deriving value_per_price from
    the contract size makes tick_value/tick_size and value_per_price diverge by
    the conversion factor. Refusing on that spread would stop a healthy client
    from trading, so the larger figure is used instead: a bigger per-lot risk
    means a smaller position, so a wrong branch can only under-size the order and
    never blow it up. The spread is reported so a real mix-up stays visible.
    """
    price_risk = 14.38
    # 1438 from the tick fields against 215.7 from a value_per_price ten times small.
    mixed = {"tick_size": 0.01, "tick_value": 1.0, "value_per_price": 15.0}
    value, source, spread = turtle_agent._risk_per_lot(price_risk, mixed)
    assert value == pytest.approx(1438.0)
    assert source == "tick_size"
    assert spread == pytest.approx(1438.0 / 215.7, rel=1e-3)

    # A figure from another currency is far larger: it wins, and under-sizes.
    foreign = {"tick_size": 0.01, "tick_value": 1.0, "value_per_price": 100_000.0}
    value, source, spread = turtle_agent._risk_per_lot(price_risk, foreign)
    assert value == pytest.approx(price_risk * 100_000.0)
    assert source == "value_per_price"
    assert spread > 5.0

    # Agreement is the normal case and reports no spread.
    consistent = {"tick_size": 0.01, "tick_value": 1.0, "value_per_price": 100}
    value, source, spread = turtle_agent._risk_per_lot(price_risk, consistent)
    assert value == pytest.approx(1438.0)
    assert source == "tick_size"
    assert spread == pytest.approx(1.0)


def test_add_explains_why_the_position_size_could_not_be_computed() -> None:
    """A client EA without contract metadata must not fail silently.

    Refusing to size the order is the safe outcome, but the operator has to be
    able to tell "no setup" apart from "we could not size this".
    """
    config = {
        "allow_add": True,
        "max_positions": 4,
        "position_size_mode": "risk",
        "risk_base_mode": "fixed_loss",
        "risk_amount": 100,
        "add_step_atr": 0.5,
    }
    # _position_request carries no symbol_info, as an un-updated client would.
    request = _position_request(
        [_position("7001", current_price=111.0)],
        bid=111.0,
        ask=111.1,
    )

    decision = TurtleTrendStrategy().evaluate_position(request, {"config": _legacy(config)})

    assert decision.action == "HOLD"
    assert "加仓手数无法确定" in decision.reason


def test_config_contract_size_can_no_longer_size_an_order() -> None:
    """The 40-million-lot order: the config fallback is gone.

    The deployment carried contract_size = 0.1505, which _risk_per_lot used as if
    it were an amount of money per price unit, multiplying the position size by
    roughly 665. A contract size is a quantity of the underlying, not money, so
    it is no longer read from the config at all.
    """
    from types import SimpleNamespace

    request = SimpleNamespace(symbol_info={}, balance=6_643_600.0, equity=6_643_600.0)

    def size(contract_size: float) -> tuple[float, dict]:
        config = {
            "position_size_mode": "risk",
            "risk_base_mode": "fixed_loss",
            "risk_amount": 100,
            "contract_size": contract_size,
        }
        return turtle_agent._unit_lot(request, config, entry=4316.05, stop_loss=4301.67)

    # The production value: refused as implausible even before the fallback went.
    lot, sizing = size(0.1505)
    assert lot == 0.0
    assert sizing["risk_source"] == "none"
    assert sizing["price_risk"] == pytest.approx(14.38)

    # A value large enough to look plausible must not be used either: the config
    # is not consulted, so an empty symbol_info simply means no position size.
    lot, sizing = size(1000.0)
    assert lot == 0.0
    assert sizing["risk_source"] == "none"
    assert sizing["risk_per_lot"] == 0.0


def _gate_decision(content: dict[str, Any]) -> Any:
    client = _FakeRiskGate(open_content=content)
    return TurtleTrendStrategy(client).evaluate_open(_breakout_request(), {"config": {}})


def test_unreadable_risk_level_does_not_open() -> None:
    """An answer with no verdict at all must not be read as "not high".

    A live case warned in prose about exhaustion and a false breakout while no
    approval flag or level came back, and the entry went through because only an
    explicit high level vetoed. Refusing is the safe reading of a broken answer.
    """
    decision = _gate_decision({
        "reason": "超卖反抽，动能不持续",
        "analysis": "上涨末端承压，超卖反抽，面临假突破与浮亏扩大压力",
    })

    assert decision.action == "HOLD"
    assert "风险等级无法识别" in decision.reason


def test_a_generic_should_open_verdict_is_understood() -> None:
    """The model answers should_open; reading only allow_open lost every verdict.

    Production: the generic prompt shape asks for should_open, so allow_open was
    never present and the gate read defaults on every call.
    """
    approved = _gate_decision({"should_open": True, "analysis": "风险可控"})
    assert approved.action in {"BUY", "SELL"}
    assert "风险评估通过" in approved.reason

    refused = _gate_decision({"should_open": False, "analysis": "动能衰竭，假突破"})
    assert refused.action == "HOLD"
    assert "本次不开仓" in refused.reason

    # An explicit no blocks even when the model rates the risk lower: that is the
    # combination customers complained about, "it warned yet it still opened".
    blocked = _gate_decision({"allow_open": False, "risk_level": "medium", "analysis": "动能转弱"})
    assert blocked.action == "HOLD"
    assert "本次不开仓" in blocked.reason


def test_the_panel_omits_a_risk_level_that_never_arrived() -> None:
    """A model following the generic shape never sends risk_level.

    Every entry then read "风险未分级", which looks like a fault the operator
    cannot act on. The approval is the signal that matters, so the level is only
    mentioned when the model actually supplied one.
    """
    without_level = _gate_decision({"should_open": True, "analysis": "风险可控"})
    assert without_level.action in {"BUY", "SELL"}
    assert "风险评估通过" in without_level.reason
    assert "未分级" not in without_level.reason

    with_level = _gate_decision(
        {"should_open": True, "risk_level": "low", "analysis": "风险可控"}
    )
    assert "（风险正常）" in with_level.reason


def test_a_verdict_missing_every_signal_is_asked_again() -> None:
    """Only a reply with no flag and no level is treated as unreadable."""
    gate = _FormatFixedOnRetry({"should_open": True, "analysis": "风险可控"})

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "BUY"
    assert gate.open_calls == 2
    assert "correction" in gate.open_kwargs[1]
    assert "correction" not in gate.open_kwargs[0]
    assert "AI 风险评估通过" in decision.reason


def test_cautious_verdict_still_opens_and_explains_the_gate() -> None:
    """A deliberate low/medium answer keeps the original lenient behaviour.

    "The AI warned yet it still opened" is the question customers ask, so the
    panel has to state the rule instead of only reporting the verdict.
    """
    decision = _gate_decision({
        "risk_level": "medium",
        "reason": "结构一般",
        "analysis": "趋势尚可但不够理想",
    })

    assert decision.action in {"BUY", "SELL"}
    assert "AI 仅在判定高风险时才阻止开仓" in decision.reason


def _confirm_bar(open_: float, high: float, low: float, close: float, index: int = 0) -> Candle:
    return Candle(
        timestamp=1_700_000_000 + index * 900, open=open_, high=high, low=low,
        close=close, volume=1.0,
    )


def _flat_bars(count: int, start: int = 0) -> list[Candle]:
    return [_confirm_bar(100.0, 100.5, 99.5, 100.0, start + i) for i in range(count)]


def test_a_single_condition_is_enough_when_it_is_on_the_newest_bar() -> None:
    """The count is the signal's strength and goes to the AI, not a local cut-off.

    One condition is allowed through as long as it is the newest bar, so the entry
    is not late; whether one condition is strong enough to trade is the AI's call.
    """
    from app.strategies import turtle_agent

    candles = _flat_bars(6)
    candles.append(_confirm_bar(101.0, 101.2, 99.8, 100.0, 6))
    candles.append(_confirm_bar(99.9, 101.5, 99.5, 101.3, 7))     # engulfing on the newest

    assert turtle_agent.SWING_CONFIRM_MIN_SIGNALS == 1
    assert turtle_agent._confirmation_text(candles, 5, "buy", 1) != ""


def test_the_risk_gate_prompt_weighs_the_confirmation_strength() -> None:
    """The AI is told how to use one condition versus several."""
    from app.services.ai_service import _turtle_open_risk_system_prompt

    prompt = _turtle_open_risk_system_prompt()
    assert "That list is the signal's strength" in prompt
    assert "single condition is weak" in prompt
    assert "already gone stale" in prompt


def test_two_conditions_with_one_on_the_newest_bar_confirm() -> None:
    """The source design: at least two conditions inside the window, one of them now."""
    from app.strategies import turtle_agent

    # A bullish pin bar two bars back, then a bearish bar, then a bullish engulfing
    # on the newest bar. Two distinct conditions, and the newest bar carries one.
    candles = _flat_bars(5)
    candles.append(_confirm_bar(100.0, 100.5, 95.0, 100.4, 5))    # pin bar
    candles.append(_confirm_bar(101.0, 101.2, 99.8, 100.0, 6))    # bearish
    candles.append(_confirm_bar(99.9, 101.5, 99.5, 101.3, 7))     # engulfing (newest)

    text = turtle_agent._confirmation_text(candles, 5, "buy", 2)

    assert "Pin Bar" in text
    assert "吞没" in text


def test_a_single_condition_is_no_longer_enough() -> None:
    """One condition used to be enough, which is what made entries late."""
    from app.strategies import turtle_agent

    candles = _flat_bars(6)
    candles.append(_confirm_bar(101.0, 101.2, 99.8, 100.0, 6))
    candles.append(_confirm_bar(99.9, 101.5, 99.5, 101.3, 7))     # engulfing only

    assert turtle_agent._confirmation_text(candles, 5, "buy", 2) == ""
    # The setting restores the old behaviour when a deployment wants it.
    assert turtle_agent._confirmation_text(candles, 5, "buy", 1) != ""


def test_two_conditions_both_in_the_past_do_not_confirm() -> None:
    """A condition from several bars ago must not count as a fresh trigger."""
    from app.strategies import turtle_agent

    candles = _flat_bars(3)
    candles.append(_confirm_bar(100.0, 100.5, 95.0, 100.4, 3))    # pin bar, 4 bars back
    candles.append(_confirm_bar(101.0, 101.2, 99.8, 100.0, 4))
    candles.append(_confirm_bar(99.9, 101.5, 99.5, 101.3, 5))     # engulfing, 2 bars back
    candles.append(_confirm_bar(101.0, 101.2, 100.0, 100.5, 6))    # nothing on the newest

    assert turtle_agent._confirmation_text(candles, 5, "buy", 2) == ""


def test_the_breakout_wording_follows_the_direction() -> None:
    """A sell breaks the low, and worded as a high it reads as a calculation error.

    Production showed sells reported as 收盘价 7646.59 上破…区间高点 7672.84 - a close
    below the level it supposedly broke.
    """
    from app.strategies import turtle_agent

    # A close under the 20-bar low is the sell side of the turtle breakout.
    request = _open_request(last_close=98.0, last_high=99.5, last_low=97.5)

    decision = turtle_agent.TurtleTrendStrategy().evaluate_open(request, {"config": {}})

    assert decision.status == "APPROVED"
    assert decision.action == "SELL"
    assert "下破" in decision.reason
    assert "区间低点" in decision.reason
    assert "上破" not in decision.reason


def _retest_candles(pullback: str) -> list[Candle]:
    """A channel at 100.5, a close above it, then one bar that behaves a certain way.

    The channel comes from twenty flat bars, so the level is exact and the test
    says nothing about how the channel is calculated.
    """
    bars = _flat_bars(20)
    bars.append(_confirm_bar(100.4, 101.5, 100.3, 101.2, 20))     # closes above 100.5
    if pullback == "holds":
        bars.append(_confirm_bar(101.1, 101.3, 100.55, 100.9, 21))
    elif pullback == "fails":
        bars.append(_confirm_bar(101.1, 101.3, 100.55, 99.8, 21))
    else:                                                        # never comes back
        bars.append(_confirm_bar(101.2, 101.6, 101.1, 101.5, 21))
    bars.append(_confirm_bar(101.4, 101.8, 101.2, 101.7, 22))
    bars.append(_confirm_bar(101.7, 102.0, 101.5, 101.9, 23))
    return bars


def test_a_pullback_to_the_broken_level_is_a_confirmation() -> None:
    """The anti-chase evidence: the level was given back once and held."""
    from app.strategies import turtle_agent

    hit = turtle_agent._retest_hit(
        _retest_candles("holds"), "buy", config={"entry_period": 20, "retest_max_wait_bars": 3}, tolerance=0.5
    )

    assert hit is not None
    assert "回踩" in hit[1]


def test_a_breakout_that_never_comes_back_is_not_a_retest() -> None:
    from app.strategies import turtle_agent

    assert turtle_agent._retest_hit(
        _retest_candles("runs"), "buy", config={"entry_period": 20, "retest_max_wait_bars": 3}, tolerance=0.5
    ) is None


def test_a_pullback_that_closes_back_below_the_level_is_not_a_retest() -> None:
    from app.strategies import turtle_agent

    assert turtle_agent._retest_hit(
        _retest_candles("fails"), "buy", config={"entry_period": 20, "retest_max_wait_bars": 3}, tolerance=0.5
    ) is None


def test_the_retest_condition_can_be_switched_off() -> None:
    from app.strategies import turtle_agent

    assert turtle_agent._retest_hit(
        _retest_candles("holds"), "buy",
        config={"entry_period": 20, "retest_enabled": False, "entry_period": 20, "retest_max_wait_bars": 3}, tolerance=0.5,
    ) is None


def test_a_retest_is_evidence_and_never_the_trigger() -> None:
    """It is reported at the bar that held the level, not the newest bar."""
    from app.strategies import turtle_agent

    candles = _retest_candles("holds")
    hits = turtle_agent._confirmation_signals(
        candles, 5, "buy", 0.0, 0.0, None, {"entry_period": 20, "retest_max_wait_bars": 3}, 0.5
    )
    retest = [item for item in hits if "回踩" in item[1]]

    assert retest
    assert not any(index == len(candles) - 1 for index, _ in retest)


def _star_candles() -> list[Candle]:
    """A decline, then a small-bodied bar at the low, then a reversal bar."""
    bars = _flat_bars(4)
    bars.append(_confirm_bar(106.0, 106.2, 105.0, 105.0, 4))     # downtrend leg
    bars.append(_confirm_bar(105.0, 105.2, 103.0, 103.5, 5))     # first: bearish
    bars.append(_confirm_bar(103.4, 103.6, 102.9, 103.45, 6))    # star: small body, lowest low
    bars.append(_confirm_bar(103.5, 105.4, 103.4, 105.2, 7))     # reversal: bullish, recovers
    return bars


def _closes_to_candles(closes: list[float]) -> list[Candle]:
    """One candle per close, each with a small wick of its own.

    The wick matters: with open equal to the previous close the bounce bar repeats
    the low of the bar before it, and a pivot needs to be strictly the lowest, so
    the series would contain no pivots at all.
    """
    return [
        Candle(
            timestamp=1_700_000_000 + index * 900, open=close, high=close + 0.1,
            low=close - 0.1, close=close, volume=1.0,
        )
        for index, close in enumerate(closes)
    ]


def _divergence_closes() -> list[float]:
    """A steep drop, a bounce, then a gentle slide to a lower low.

    The second leg loses less per bar than the first, so momentum is firmer while
    the price is lower - which is what a bottom divergence is. The flat lead-in
    exists because RSI needs its period before the first reading means anything.
    """
    closes = [100.0] * 30
    closes += [100 - index * 1.0 for index in range(1, 9)]
    closes += [93, 94, 95, 96]
    closes += [95.8 - index * 0.2 for index in range(25)]
    closes += [92, 93, 94]
    return closes


def _top_divergence_closes() -> list[float]:
    """The mirror of the bottom case: a steep rise, then a gentler one to a top."""
    closes = [100.0] * 30
    closes += [100 + index * 1.0 for index in range(1, 9)]
    closes += [107, 106, 105, 104]
    closes += [104.2 + index * 0.2 for index in range(25)]
    closes += [108, 107, 106]
    return closes


def test_the_opposing_divergence_warns_against_the_side_held() -> None:
    """For a long, the warning is the bearish one - the mirror of the supporting."""
    from app.strategies import turtle_agent

    warning = turtle_agent._opposing_divergence(
        _closes_to_candles(_top_divergence_closes()), "buy", config={}
    )

    assert "顶背离" in warning
    # And the same series supports a short rather than warning it.
    assert turtle_agent._opposing_divergence(
        _closes_to_candles(_top_divergence_closes()), "sell", config={}
    ) == ""


def test_both_ai_prompts_weigh_the_opposing_divergence() -> None:
    """The strategy can only offer evidence; the prompt has to say what it means."""
    from app.services.ai_service import (
        _turtle_open_risk_system_prompt,
        _turtle_position_review_prompt,
    )

    assert "opposing_divergence" in _turtle_open_risk_system_prompt()
    assert "opposing_divergence" in _turtle_position_review_prompt()


def _drawdown_candles(high: float, low: float = 4290.0) -> list[Candle]:
    return [
        _confirm_bar(4300.0, 4302.0, 4298.0, 4300.5, 0),
        _confirm_bar(4300.5, high, 4299.0, high - 0.5, 1),
        _confirm_bar(high - 0.5, high - 0.2, low, low + 1.0, 2),
    ]


def _basket(entry: float = 4300.0, side: str = "BUY") -> list[Any]:
    class _P:
        pass

    item = _P()
    item.side = side
    item.open_price = entry
    item.volume = 0.1
    return [item]


def test_the_basket_drawdown_is_a_share_of_the_best_level() -> None:
    """A pause from +5 ATR and a round trip from +1.5 are not the same thing."""
    from app.strategies import turtle_agent

    # Entry 4300, ATR 4: best +5.0 ATR, now +3.5 ATR, so 30% given back.
    paused = turtle_agent._basket_drawdown(
        _basket(), _drawdown_candles(4320.0), 4314.0, 4314.2, 4.0
    )
    assert paused["peak"] == pytest.approx(5.0)
    assert paused["current"] == pytest.approx(3.5)
    assert paused["give_back"] == pytest.approx(0.3)

    # The same 1.5 ATR off a smaller peak is a full round trip.
    spent = turtle_agent._basket_drawdown(
        _basket(), _drawdown_candles(4306.0), 4300.0, 4300.2, 4.0
    )
    assert spent["peak"] == pytest.approx(1.5)
    assert spent["give_back"] == pytest.approx(1.0)


def _drawdown_request(high: float, bid: float) -> Any:
    """The add-ready fixture, rescaled so the candles match the entry."""
    request = _add_ready_request()
    request.positions[0].open_price = 4300.0
    request.candles = _drawdown_candles(high)
    request.bid, request.ask = bid, bid + 0.2
    return request


def test_a_basket_that_gave_its_profit_back_is_not_added_to() -> None:
    """The spacing rule alone bought the pullback; this is what stops it."""
    from app.strategies import turtle_agent

    # Best +5 ATR, now +0.5 ATR: 90% of the move given back.
    decision, reason = turtle_agent._maybe_add(
        _drawdown_request(4320.0, 4302.0), dict(_ADD_CONFIG), 4.0
    )

    assert decision is None
    assert "回吐" in reason


def test_a_basket_still_near_its_best_may_add() -> None:
    from app.strategies import turtle_agent

    decision, _ = turtle_agent._maybe_add(
        _drawdown_request(4320.0, 4319.0), dict(_ADD_CONFIG), 4.0
    )

    assert decision is not None


def test_the_drawdown_guard_does_not_hold_a_small_peak_against_the_basket() -> None:
    """A basket barely in profit must still be allowed to add."""
    from app.strategies import turtle_agent

    decision, _ = turtle_agent._maybe_add(
        _drawdown_request(4305.0, 4302.0), dict(_ADD_CONFIG), 4.0
    )

    assert decision is not None


def test_rsi_reads_a_one_way_market() -> None:
    from app.strategies import turtle_agent

    rising = turtle_agent._rsi_series([100 + index for index in range(30)], 14)
    falling = turtle_agent._rsi_series([100 - index for index in range(30)], 14)
    flat = turtle_agent._rsi_series([100.0] * 30, 14)

    assert rising[-1] > 95
    assert falling[-1] < 5
    assert flat[-1] == pytest.approx(50.0)


def test_a_lower_low_with_firmer_momentum_is_a_bottom_divergence() -> None:
    """Price makes the new low; momentum does not. That is the leading warning."""
    from app.strategies import turtle_agent

    candles = _closes_to_candles(_divergence_closes())

    hit = turtle_agent._divergence_hit(candles, "buy", config={})

    assert hit is not None
    assert "底背离" in hit[1]


def test_a_clean_trend_reports_no_divergence() -> None:
    from app.strategies import turtle_agent

    candles = _closes_to_candles([100.0] * 30 + [100 - index for index in range(20)])

    assert turtle_agent._divergence_hit(candles, "buy", config={}) is None


def test_a_divergence_is_a_warning_and_never_the_trigger() -> None:
    """It is reported at the pivot, so it cannot satisfy the newest-bar rule."""
    from app.strategies import turtle_agent

    candles = _closes_to_candles(_divergence_closes())

    hits = turtle_agent._confirmation_signals(candles, 5, "buy", 0.0, 0.0, None, {})
    divergence = [item for item in hits if "背离" in item[1]]

    assert divergence
    assert not any(index == len(candles) - 1 for index, _ in divergence)


def test_the_divergence_condition_can_be_switched_off() -> None:
    from app.strategies import turtle_agent

    candles = _closes_to_candles(_divergence_closes())

    assert turtle_agent._divergence_hit(
        candles, "buy", config={"divergence_enabled": False}
    ) is None


def test_a_morning_star_is_a_confirmation() -> None:
    """A trend into a star at the low, then a reversal bar: three bars as a group.

    A doji on its own only says the market is undecided, so it is judged with the
    bars around it rather than as a lone candle.
    """
    from app.strategies import turtle_agent

    text = turtle_agent._confirmation_text(
        _star_candles(), 5, "buy", 1, 0.0, 0.0, {"star_trend_bars": 3}
    )

    assert "启明星" in text


def test_a_star_without_the_reversal_bar_is_not_counted() -> None:
    from app.strategies import turtle_agent

    candles = _star_candles()
    # The third bar closes below where it opened, so there is no reversal.
    candles[-1] = _confirm_bar(105.0, 105.2, 103.4, 103.6, 7)

    text = turtle_agent._confirmation_text(
        candles, 5, "buy", 1, 0.0, 0.0, {"star_trend_bars": 3}
    )

    assert "启明星" not in text


def test_a_star_that_is_not_the_low_is_not_counted() -> None:
    from app.strategies import turtle_agent

    candles = _star_candles()
    # The middle bar is no longer the lowest of the three.
    candles[-2] = _confirm_bar(103.4, 103.6, 103.2, 103.45, 6)

    text = turtle_agent._confirmation_text(
        candles, 5, "buy", 1, 0.0, 0.0, {"star_trend_bars": 3}
    )

    assert "启明星" not in text


def test_a_pin_bar_wick_also_has_to_reach_a_share_of_the_atr() -> None:
    """One tick of body and two of wick used to count as a pattern."""
    from app.strategies import turtle_agent

    candles = _flat_bars(7)
    # Body 0.01, wick 0.02: twice the body, and closed in the upper half.
    candles.append(_confirm_bar(100.0, 100.05, 99.97, 100.01, 7))

    assert "Pin Bar" in turtle_agent._confirmation_text(candles, 5, "buy", 1)
    # With a floor of 0.5 the two-tick wick no longer qualifies.
    assert "Pin Bar" not in turtle_agent._confirmation_text(
        candles, 5, "buy", 1, 0.0, 0.5
    )


def test_an_engulfing_may_miss_the_previous_body_by_a_few_ticks() -> None:
    """The gap is whatever the market left: one tick, or three.

    Live case: one bar closed 4374.60 and the next opened 4374.63. The first
    version of this allowance was two ticks and still rejected it.
    """
    from app.strategies import turtle_agent

    candles = _flat_bars(6)
    candles.append(_confirm_bar(4375.10, 4375.20, 4374.00, 4374.60, 6))
    candles.append(_confirm_bar(4374.63, 4375.90, 4374.40, 4375.60, 7))

    assert turtle_agent._confirmation_text(candles, 5, "buy", 1) == ""
    assert "吞没" in turtle_agent._confirmation_text(candles, 5, "buy", 1, 0.05)


def test_the_engulf_tolerance_takes_the_larger_of_ticks_and_atr() -> None:
    """Ticks alone are too tight on a coarse symbol; the ATR share stretches it."""
    from app.strategies import turtle_agent

    # 5 ticks of a 0.01-point symbol = 0.05.
    assert turtle_agent._engulf_tolerance({"point": 0.01}, {}) == pytest.approx(0.05)
    # A 0.03 share of ATR 20 = 0.60, which is larger and therefore used.
    assert turtle_agent._engulf_tolerance({"point": 0.01}, {}, 20.0) == pytest.approx(0.60)
    # Either side can be switched off.
    assert turtle_agent._engulf_tolerance(
        {"point": 0.01}, {"engulf_tolerance_points": 0}, 20.0
    ) == pytest.approx(0.60)
    assert turtle_agent._engulf_tolerance(
        {"point": 0.01}, {"engulf_tolerance_atr": 0}
    ) == pytest.approx(0.05)
    # Both off restores the exact rule.
    assert turtle_agent._engulf_tolerance(
        {"point": 0.01}, {"engulf_tolerance_points": 0, "engulf_tolerance_atr": 0}, 20.0
    ) == 0.0
    assert turtle_agent._engulf_tolerance({}, {"engulf_tolerance_atr": 0}) == 0.0


def test_the_default_protection_thresholds_are_the_tightened_ones() -> None:
    """The operator's settings: break even at 0.5, trail from 1.0 at 0.5 behind.

    Every test that needs the older thresholds pins them through _legacy, so this
    is the one place that says what a deployment gets without any configuration.
    """
    from app.strategies import turtle_agent

    assert turtle_agent.DEFAULT_BREAK_EVEN_ATR == 0.5
    assert turtle_agent.DEFAULT_TRAILING_START_ATR == 1.0
    assert turtle_agent.DEFAULT_TRAILING_DISTANCE_ATR == 0.5
    assert turtle_agent.DEFAULT_TRAILING_MIN_STEP_ATR == 0.2


def test_the_position_review_prompt_binds_its_answer_to_its_analysis() -> None:
    """The review must not describe a broken structure while answering false.

    Production watched a basket give back more than six hundred to break-even
    while its analysis read like a breakdown, and a competitor's model on the
    same bars locked in several hundred.
    """
    from app.services.ai_service import _turtle_position_review_prompt

    prompt = _turtle_position_review_prompt()
    assert "Your level and your analysis must agree" in prompt
    assert "never describe a broken structure" in prompt
    assert "giving back profit" in prompt
    # The sentence that invited a decline is gone.
    assert "Declining is safe" not in prompt


def test_risk_gate_prompt_requires_a_consistent_answer() -> None:
    from app.services.ai_service import _turtle_open_risk_system_prompt

    prompt = _turtle_open_risk_system_prompt()
    assert "Your level and your analysis must agree" in prompt
    assert "risk_level high" in prompt
    # The level is optional in practice, so the prompt must not ask for a
    # placeholder when the model has none to report.
    assert "风险等级为？" in prompt
    assert "risk_level is optional" in prompt


class _FormatFixedOnRetry(_FakeRiskGate):
    """First answer ignores the contract, the corrective retry follows it."""

    def __init__(self, second_content: dict[str, Any]) -> None:
        super().__init__(open_content={"reason": "只有理由，没有任何判定字段"})
        self.second_content = second_content
        self.open_kwargs: list[dict[str, Any]] = []

    def turtle_open_risk_decision(self, **kwargs: Any) -> Any:
        self.open_kwargs.append(kwargs)
        self.open_calls += 1
        content = self.open_content if len(self.open_kwargs) == 1 else self.second_content
        return AiCallResult(content=content, usage=UsageSummary(ai_called=True))


def test_a_verdict_that_stays_malformed_after_the_retry_is_refused() -> None:
    """Asking again is a second chance, not a way to open regardless."""
    gate = _FakeRiskGate(open_content={"reason": "只有理由，没有任何判定字段"})

    decision = TurtleTrendStrategy(gate).evaluate_open(_breakout_request(), {"config": {}})

    assert decision.action == "HOLD"
    assert "风险等级无法识别" in decision.reason
    assert gate.open_calls == 2


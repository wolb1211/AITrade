from __future__ import annotations

from pathlib import Path

import pytest

from app.api.router import _mt5_open_response
from app.models import Candle, TradeDecision
from app.store import SqliteStore
from app.strategies.pa_agent_lite import (
    PaSetupCandidate,
    _compute_features,
    _pullback_attempt_pattern,
    _select_setup_candidate,
)
from tests.test_pa_agent_safety import _trend_candles


def _candidate(direction: str, score: int) -> PaSetupCandidate:
    return PaSetupCandidate(
        code=f"test_{direction}",
        label=f"test {direction}",
        direction=direction,
        context_score=20,
        structure_score=20,
        trigger_score=20,
        space_score=15,
        penalty_score=max(0, 75 - score),
        total_score=score,
        hard_blocks=(),
        evidence=(),
    )


def test_v2_strong_trend_selects_one_named_candidate() -> None:
    features = _compute_features(_trend_candles())

    assert features is not None
    assert features.setup_version == 2
    assert features.setup_code == "trend_continuation_long"
    assert features.candidate_valid is True
    assert features.long_score >= 70
    assert features.short_score == 0
    assert features.setup_components.keys() == {"context", "structure", "trigger", "space", "penalty"}


def test_direction_margin_blocks_conflicting_candidates() -> None:
    selected = _select_setup_candidate([
        _candidate("bullish", 80),
        _candidate("bearish", 73),
    ])

    assert selected["score"] == 80
    assert selected["margin"] == 7
    assert selected["valid"] is False


def test_direction_margin_accepts_clear_candidate() -> None:
    selected = _select_setup_candidate([
        _candidate("bullish", 82),
        _candidate("bearish", 60),
    ])

    assert selected["margin"] == 22
    assert selected["valid"] is True


def test_h2_is_second_distinct_high_attempt_inside_pullback() -> None:
    values = [
        (100, 103, 99, 102),
        (102, 105, 101, 104),
        (104, 107, 103, 106),
        (106, 106, 102, 103),
        (103, 107, 102.5, 104),
        (104, 104.5, 101, 102),
        (102, 105, 101.5, 104.5),
    ]
    bars = [
        Candle(timestamp=index, open=o, high=h, low=l, close=c)
        for index, (o, h, l, c) in enumerate(values)
    ]

    pattern = _pullback_attempt_pattern(bars, direction="long", invalidation=90)

    assert pattern["pattern"] == "H2"
    assert pattern["triggered"] is True
    assert pattern["trigger_bar_index"] == -1
    assert pattern["structure_valid"] is True


def test_v2_order_comment_contains_setup_and_decision_token() -> None:
    decision = TradeDecision(
        decision_id="dec_12345678abcdef12",
        request_id="request_v2_comment",
        status="APPROVED",
        action="BUY",
        symbol="XAUUSD",
        confidence=0.8,
        reason="趋势延续做多",
        expires_at="2026-08-29T12:00:00+00:00",
        lot=0.1,
        entry=4500,
        sl=4490,
        tp=4518,
        metadata={"setup_code": "trend_continuation_long", "setup_version": 2},
    )

    response = _mt5_open_response(decision, spread=0.2)

    assert response.orders_count == 1
    assert response.orders[0].comment == "GL2-TCL-abcdef12"


def test_synced_v2_orders_produce_setup_performance_stats(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "setup-stats.db")
    store.initialize()
    deployment = store.upsert_web_deployment(
        "gl_setup_stats_key",
        user_id="1",
        strategy_code="PA_AGENT_V1",
        strategy_name="GL趋势自动分析策略",
        status="active",
        symbol="*",
        timeframe="*",
        config={"deployment_key": "gl_setup_stats_key"},
    )
    store.save_decision(
        deployment["id"],
        "open",
        "request_setup_stats",
        {
            "decision_id": "dec_12345678abcdef12",
            "action": "BUY",
            "metadata": {
                "setup_code": "breakout_long",
                "setup_name": "突破做多",
                "setup_version": 2,
                "direction": "buy",
                "score": 82,
                "components": {"context": 20, "structure": 25, "trigger": 25, "space": 12, "penalty": 0},
            },
        },
        symbol="XAUUSD",
        timeframe="M5",
    )
    profits = [100, -40, -70]
    store.sync_mt5_history_deals(
        deployment["id"],
        account_login="10001",
        account_server="Broker-Demo",
        orders=[
            {
                "order_id": f"order-{index}",
                "deal_id": f"order-{index}",
                "symbol": "XAUUSD",
                "mt_type": 0,
                "volume": 0.1,
                "open_price": 4500,
                "close_price": 4510,
                "profit": profit,
                "commission": 0,
                "swap": 0,
                "net_profit": profit,
                "open_time": 1_700_000_000 + index * 600,
                "close_time": 1_700_000_300 + index * 600,
                "comment": "GL2-BOL-abcdef12",
            }
            for index, profit in enumerate(profits)
        ],
    )

    result = store.strategy_setup_stats(deployment["id"])
    row = result["list"][0]

    assert result["attributed_orders"] == 3
    assert row["setup_code"] == "breakout_long"
    assert row["order_count"] == 3
    assert row["win_rate"] == pytest.approx(33.33)
    assert row["average_win_loss_ratio"] == pytest.approx(1.82)
    assert row["profit_factor"] == pytest.approx(0.91)
    assert row["expectancy"] == pytest.approx(-3.33)
    assert row["max_drawdown"] == pytest.approx(110)
    assert row["max_consecutive_losses"] == 2


def test_setup_attribution_falls_back_to_execution_report_when_comment_is_missing(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "setup-report-fallback.db")
    store.initialize()
    deployment = store.upsert_web_deployment(
        "gl_setup_report_fallback_key",
        user_id="1",
        strategy_code="PA_AGENT_V1",
        strategy_name="GL趋势自动分析策略",
        status="active",
        symbol="*",
        timeframe="*",
        config={"deployment_key": "gl_setup_report_fallback_key"},
    )
    decision_id = "dec_12345678feedbeef"
    store.save_decision(
        deployment["id"],
        "open",
        "request_setup_report_fallback",
        {
            "decision_id": decision_id,
            "action": "BUY",
            "metadata": {
                "setup_code": "pullback_h2_long",
                "setup_name": "回调H2做多",
                "setup_version": 2,
                "direction": "buy",
                "score": 78,
                "components": {"context": 20, "structure": 24, "trigger": 22, "space": 12, "penalty": 0},
            },
        },
        symbol="XAUUSD",
        timeframe="M5",
    )
    store.save_execution_report(
        deployment["id"],
        {
            "decision_id": decision_id,
            "success": True,
            "order_id": "broker-order-1",
            "deal_id": "broker-deal-1",
            "payload": {},
        },
    )
    store.sync_mt5_history_deals(
        deployment["id"],
        account_login="10001",
        account_server="Broker-Demo",
        orders=[
            {
                "order_id": "broker-order-1",
                "deal_id": "broker-deal-1",
                "symbol": "XAUUSD",
                "mt_type": 0,
                "volume": 0.1,
                "open_price": 4500,
                "close_price": 4510,
                "profit": 50,
                "commission": 0,
                "swap": 0,
                "net_profit": 50,
                "open_time": 1_700_000_000,
                "close_time": 1_700_000_300,
                "comment": "",
            }
        ],
    )

    result = store.strategy_setup_stats(deployment["id"])
    assert result["attributed_orders"] == 1
    assert result["list"][0]["setup_code"] == "pullback_h2_long"
    assert result["list"][0]["average_score"] == 78

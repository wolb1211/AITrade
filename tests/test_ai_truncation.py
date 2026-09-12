"""A response cut off at the output limit must be distinguishable from garbage.

Reasoning models charge their thinking against `max_tokens`, so they run out of
budget before writing the JSON. Before this was tracked, both cases produced the
same "AI返回格式异常" fallback and there was no way to tell them apart.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import TradeDecision, UsageSummary
from app.services.ai_service import (
    REPAIR_TIMEOUT_SECONDS,
    AiDecisionClient,
    _elapsed_note,
    _max_tokens_for_endpoint,
    _truncation_note,
)
from app.store import SqliteStore


def _client(tmp_path: Path) -> AiDecisionClient:
    store = SqliteStore(tmp_path / "truncation.db")
    store.initialize()
    store.save_ai_endpoint({
        "id": "aie_trunc",
        "owner_type": "gl",
        "name": "Truncation model",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "sk-trunc",
        "is_default": True,
        "input_price_per_million": "2",
        "output_price_per_million": "8",
    })
    store.save_user({"email": "trunc@example.com", "status": "active"})
    return AiDecisionClient(store)


def _deployment(store: SqliteStore) -> dict:
    user = store.list_users(page=1, size=1)["list"][0]
    return {"id": "dep_trunc", "user_id": str(user["id"]), "strategy_code": "PA_AGENT_V1", "config": {}}


def _response(content: str, *, finish_reason: str, completion_tokens: int) -> str:
    return json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": completion_tokens,
            "total_tokens": 100 + completion_tokens,
        },
    }, ensure_ascii=False)


# ── the token budget ──────────────────────────────────────────────────────────


def test_decision_endpoints_get_a_budget_reasoning_models_can_fit_in() -> None:
    # Reasoning eats the same budget as the answer, so 1000 truncated DeepSeek.
    assert _max_tokens_for_endpoint("open") == 3000
    assert _max_tokens_for_endpoint("position") == 3000
    assert _max_tokens_for_endpoint("pa_diag") == 3000
    # Workflow compilation legitimately returns large JSON.
    assert _max_tokens_for_endpoint("workflow_open") == 6000
    # Anything unrecognised keeps the small default.
    assert _max_tokens_for_endpoint("something_else") == 500


def test_truncation_note_is_empty_unless_truncated() -> None:
    assert _truncation_note(truncated=False, completion_tokens=500, max_tokens=3000) == ""
    note = _truncation_note(truncated=True, completion_tokens=3000, max_tokens=3000)
    assert "输出被截断" in note
    assert "finish_reason=length" in note
    assert "3000/3000" in note


# ── end to end ────────────────────────────────────────────────────────────────


def test_truncated_response_is_recorded_as_truncation_not_as_bad_format(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    client = _client(tmp_path)
    store = client.store
    deployment = _deployment(store)
    # Exactly the shape seen in production: reasoning prose, cut mid-sentence.
    truncated = '{"reason": "市场结构呈震荡下行，最近5根K线高点逐步下移，但是价格在77250'
    monkeypatch.setattr(
        client,
        "_post_chat_completion",
        lambda **_kwargs: _response(truncated, finish_reason="length", completion_tokens=3000),
    )

    with caplog.at_level("WARNING"):
        result = client._chat_json(
            deployment=deployment,
            endpoint="open",
            system_prompt="test",
            user_payload={"account": {"login": "123456"}, "symbol": "XAUUSD", "timeframe": "M5"},
        )

    assert result is not None
    assert "truncated at max_tokens" in caplog.text

    usage = store.list_ai_usage_logs(page=1, size=10, user_id=deployment["user_id"])["list"][0]
    preview = str(usage["response_preview"])
    assert "输出被截断" in preview
    assert "finish_reason=length" in preview
    # The customer still gets a safe decision rather than a half-parsed one.
    assert result.content["should_open"] is False


def test_normal_completion_carries_no_truncation_marker(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    store = client.store
    deployment = _deployment(store)
    body = json.dumps({
        "should_open": False,
        "direction": None,
        "confidence": 0.4,
        "reason": "条件不足",
        "analysis": "当前结构不清晰，继续等待。",
    }, ensure_ascii=False)
    monkeypatch.setattr(
        client,
        "_post_chat_completion",
        lambda **_kwargs: _response(body, finish_reason="stop", completion_tokens=120),
    )

    result = client._chat_json(
        deployment=deployment,
        endpoint="open",
        system_prompt="test",
        user_payload={"account": {"login": "123456"}, "symbol": "XAUUSD", "timeframe": "M5"},
    )

    assert result is not None
    usage = store.list_ai_usage_logs(page=1, size=10, user_id=deployment["user_id"])["list"][0]
    assert "输出被截断" not in str(usage["response_preview"])


def test_missing_finish_reason_is_not_treated_as_truncation(tmp_path: Path, monkeypatch) -> None:
    """A gateway that omits finish_reason must not get the marker by default."""
    client = _client(tmp_path)
    store = client.store
    deployment = _deployment(store)
    body = json.dumps({"should_open": False, "direction": None, "confidence": 0.4,
                       "reason": "条件不足", "analysis": "结构不清晰。"}, ensure_ascii=False)
    monkeypatch.setattr(
        client,
        "_post_chat_completion",
        lambda **_kwargs: json.dumps({
            "choices": [{"message": {"content": body}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }, ensure_ascii=False),
    )

    result = client._chat_json(
        deployment=deployment,
        endpoint="open",
        system_prompt="test",
        user_payload={"account": {"login": "123456"}, "symbol": "XAUUSD", "timeframe": "M5"},
    )

    assert result is not None
    usage = store.list_ai_usage_logs(page=1, size=10, user_id=deployment["user_id"])["list"][0]
    assert "输出被截断" not in str(usage["response_preview"])


# ── the timeout budget ────────────────────────────────────────────────────────

# The EA gives a whole decision request 180 seconds and never waits longer.
# Keep this in sync with the EA's hard-coded value, otherwise the guard below
# validates something other than the real constraint.
EA_REQUEST_TIMEOUT_SECONDS = 180.0


def test_worst_case_call_chain_fits_inside_the_ea_timeout() -> None:
    """The longest chain is PA: two stages, each possibly followed by one repair.

    This is the guarantee that the server always answers before the EA gives up.
    Raising `ai_timeout` or `REPAIR_TIMEOUT_SECONDS` without re-checking this
    arithmetic would show up in production as an EA that receives nothing at all,
    which is worse than receiving a fallback decision.

    Note when changing values: the EA is single-threaded, so `ai_timeout` is also
    how long the EA can sit idle on one check. There is spare room here, but the
    per-call timeout is a responsiveness choice as well as a safety one.
    """
    from app.config import Settings

    per_call = Settings().ai_timeout
    worst_case = 2 * per_call + 2 * REPAIR_TIMEOUT_SECONDS

    assert worst_case < EA_REQUEST_TIMEOUT_SECONDS, (
        f"worst-case call chain is {worst_case}s, which does not fit the EA's "
        f"{EA_REQUEST_TIMEOUT_SECONDS}s request timeout"
    )
    # Leave room for network round trips and for our own bookkeeping.
    assert EA_REQUEST_TIMEOUT_SECONDS - worst_case >= 10


def test_elapsed_note_is_empty_unless_there_is_a_measurement() -> None:
    assert _elapsed_note(0) == ""
    assert _elapsed_note(-1) == ""
    note = _elapsed_note(2345)
    assert "耗时" in note
    assert "2.3s" in note


# ── the EA panel line ─────────────────────────────────────────────────────────


def _decision(*, elapsed_ms: int, reason: str = "暂不开仓：当前处于窄通道") -> TradeDecision:
    from datetime import datetime, timedelta, timezone

    return TradeDecision(
        decision_id="dec_panel",
        request_id="req_panel",
        status="HOLD",
        action="HOLD",
        symbol="XAUUSD",
        confidence=0.4,
        reason=reason,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        usage=UsageSummary(ai_called=elapsed_ms > 0, elapsed_ms=elapsed_ms),
    )


def test_panel_line_shows_the_ai_analysis_time() -> None:
    from app.api.router import _panel_description

    text = _panel_description(_decision(elapsed_ms=2345))

    assert text.endswith("（本次AI分析耗时：2.3秒）")
    assert text.startswith("暂不开仓：当前处于窄通道")


def test_panel_line_omits_the_time_when_no_ai_ran() -> None:
    """A local-only decision must not read as a zero-second failure."""
    from app.api.router import _panel_description

    decision = _decision(elapsed_ms=0)

    assert _panel_description(decision) == decision.reason
    assert "耗时" not in _panel_description(decision)


def test_panel_line_is_built_for_both_ea_responses() -> None:
    from app.api.router import _mt5_open_response, _mt5_position_response

    open_decision = _decision(elapsed_ms=1500)
    open_decision.action = "BUY"
    open_decision.lot = 0.01
    open_decision.entry = 2500.0
    assert "耗时" in _mt5_open_response(open_decision, spread=0.2).description

    position_decision = _decision(elapsed_ms=900)
    assert "耗时" in _mt5_position_response(position_decision, spread=0.2, positions=[]).description


def test_stored_reason_keeps_the_strategy_wording() -> None:
    """The timing is presentation only; the decision record is not rewritten."""
    from app.api.router import _panel_description

    decision = _decision(elapsed_ms=2345)
    _panel_description(decision)

    assert decision.reason == "暂不开仓：当前处于窄通道"


def test_call_duration_is_recorded_and_shown(tmp_path: Path, monkeypatch) -> None:
    """The EA is single-threaded, so its idle time is exactly this number."""
    client = _client(tmp_path)
    store = client.store
    deployment = _deployment(store)
    body = json.dumps({"should_open": False, "direction": None, "confidence": 0.4,
                       "reason": "条件不足", "analysis": "结构不清晰。"}, ensure_ascii=False)
    monkeypatch.setattr(
        client,
        "_post_chat_completion",
        lambda **_kwargs: json.dumps({
            "choices": [{"message": {"content": body}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }, ensure_ascii=False),
    )

    result = client._chat_json(
        deployment=deployment,
        endpoint="open",
        system_prompt="test",
        user_payload={"account": {"login": "123456"}, "symbol": "XAUUSD", "timeframe": "M5"},
    )

    assert result is not None
    assert result.usage.elapsed_ms >= 1
    usage = store.list_ai_usage_logs(page=1, size=10, user_id=deployment["user_id"])["list"][0]
    assert usage["elapsed_ms"] >= 1
    assert "耗时" in str(usage["response_preview"])


def test_slow_and_truncated_are_both_reported_on_the_same_call(tmp_path: Path, monkeypatch) -> None:
    """The two markers are independent and must not overwrite each other."""
    client = _client(tmp_path)
    store = client.store
    deployment = _deployment(store)
    monkeypatch.setattr(
        client,
        "_post_chat_completion",
        lambda **_kwargs: _response(
            '{"reason": "市场结构呈震荡下行，最近5根K线高点逐步下移',
            finish_reason="length",
            completion_tokens=3000,
        ),
    )

    result = client._chat_json(
        deployment=deployment,
        endpoint="open",
        system_prompt="test",
        user_payload={"account": {"login": "123456"}, "symbol": "XAUUSD", "timeframe": "M5"},
    )

    assert result is not None
    preview = str(
        store.list_ai_usage_logs(page=1, size=10, user_id=deployment["user_id"])["list"][0][
            "response_preview"
        ]
    )
    assert "耗时" in preview
    assert "输出被截断" in preview
    assert preview.index("耗时") < preview.index("输出被截断")


def test_repair_call_gets_its_own_short_timeout(tmp_path: Path, monkeypatch) -> None:
    """The repair is a small reformatting task; it must not inherit the full timeout."""
    client = _client(tmp_path)
    captured: dict = {}

    def fake_post(**kwargs):
        captured.update(kwargs)
        fixed = json.dumps({"should_open": False, "direction": None, "confidence": 0.3,
                            "reason": "修复结果", "analysis": "已修复。"}, ensure_ascii=False)
        return json.dumps({"choices": [{"message": {"content": fixed}}]}, ensure_ascii=False)

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)

    repaired = client._repair_json_response(
        base_url="https://example.com/v1",
        api_key="sk-repair",
        model="example-model",
        endpoint="open",
        response_content='{"should_open": false, "reason": "被截断',
    )

    assert captured["timeout"] == REPAIR_TIMEOUT_SECONDS
    assert REPAIR_TIMEOUT_SECONDS < 45
    assert json.loads(repaired)["reason"] == "修复结果"


def test_normal_call_does_not_override_the_configured_timeout(tmp_path: Path, monkeypatch) -> None:
    """Only the repair path narrows the timeout; decision calls keep the setting.

    The main path passes no override at all, which is what makes it fall back to
    the client's configured `timeout`.
    """
    client = _client(tmp_path)
    captured: dict = {}

    def fake_post(**kwargs):
        captured.update(kwargs)
        body = json.dumps({"should_open": False, "direction": None, "confidence": 0.4,
                           "reason": "条件不足", "analysis": "结构不清晰。"}, ensure_ascii=False)
        return json.dumps({
            "choices": [{"message": {"content": body}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }, ensure_ascii=False)

    monkeypatch.setattr(client, "_post_chat_completion", fake_post)

    client._chat_json(
        deployment=_deployment(client.store),
        endpoint="open",
        system_prompt="test",
        user_payload={"account": {"login": "123456"}, "symbol": "XAUUSD", "timeframe": "M5"},
    )

    assert "timeout" not in captured
    # And the repair path is the only one that narrows it.
    assert REPAIR_TIMEOUT_SECONDS < client.timeout

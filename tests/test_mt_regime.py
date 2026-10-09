"""The arbitrage EA's trend read: rules always answer, AI improves the answer.

GL_ARBITRAGE_V1 trades on its own, so /mt5/strategy/regime is a reference, not a
gate. These tests pin the two properties that make it safe to poll: the answer
never depends on the AI being up, and an unusable AI answer silently degrades to
the server's own bar read instead of emptying the trend.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.regime_service import (
    REGIME_KLINE_COUNT,
    REGIME_PERIODS,
    normalize_trend_text,
    timeframe_features,
)

UP = "up"
DOWN = "down"
FLAT = "flat"


def _app(tmp_path: Path, name: str):
    return create_app(Settings(
        environment="production",
        database_path=tmp_path / name,
        auth_secret="test-auth-secret",
    ))


def _vip_user(store) -> str:
    from datetime import datetime, timedelta, timezone

    user = store.save_user({
        "email": "regime@example.com",
        "status": "active",
        "vip_level": 1,
        "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    return str(user["id"])


def _deploy(store, key: str, user_id: str, code: str = "GL_ARBITRAGE_V1") -> None:
    store.upsert_web_deployment(
        key,
        user_id=user_id,
        strategy_code=code,
        strategy_name=code,
        status="active",
        symbol="XAUUSD",
        timeframe="M15",
        config={"fixed_lot": 0.01},
    )


def _bars(direction: str, count: int = 120) -> list[dict]:
    bars = []
    for index in range(count):
        if direction == UP:
            close = 2000.0 + index * 2.0
        elif direction == DOWN:
            close = 2000.0 + (count - index) * 2.0
        else:
            close = 2000.0 + (1.0 if index % 2 else -1.0)
        bars.append({
            "time": 1_700_000_000 + index * 900,
            "open": close - 1.0,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 10.0,
        })
    return bars


def _payload(key: str, periods: dict[str, list[dict]], *, timeframe: str = "M15") -> dict:
    return {
        "deployment_key": key,
        "account": {"platform": "MT5", "login": "60064845"},
        "symbol": "XAUUSD",
        "timeframe": timeframe,
        "market": {
            "bid": 2100.0,
            "ask": 2100.2,
            "spread": 0.2,
            "bars": periods.get(timeframe, []),
            "secondary_bars": {name: bars for name, bars in periods.items() if name != timeframe},
        },
    }


def _ai_response(content: dict) -> str:
    return json.dumps({
        "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 120, "total_tokens": 520},
    }, ensure_ascii=False)


def _default_endpoint(store) -> None:
    store.save_ai_endpoint({
        "id": "aie_regime",
        "owner_type": "gl",
        "name": "Regime model",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "sk-regime",
        "is_default": True,
        "input_price_per_million": "2",
        "output_price_per_million": "8",
    })


# ── the deterministic layer ───────────────────────────────────────────────────


def test_rule_read_calls_a_rising_market_bullish() -> None:
    assert timeframe_features([_candle(bar) for bar in _bars(UP)])["rule_trend"] == "bull"


def test_rule_read_calls_a_falling_market_bearish() -> None:
    assert timeframe_features([_candle(bar) for bar in _bars(DOWN)])["rule_trend"] == "bear"


def test_rule_read_calls_a_sawtooth_range() -> None:
    assert timeframe_features([_candle(bar) for bar in _bars(FLAT)])["rule_trend"] == "range"


def test_rule_read_refuses_to_guess_on_a_short_history() -> None:
    # Twenty bars cannot support a 30 period average: saying "range" here would be
    # a guess dressed as a reading.
    features = timeframe_features([_candle(bar) for bar in _bars(UP, count=20)])
    assert features["rule_trend"] == "unknown"
    assert features["timeframe_ready"] is False


def test_trend_words_are_folded_from_both_languages() -> None:
    assert normalize_trend_text("bull") == "bull"
    assert normalize_trend_text("多头") == "bull"
    assert normalize_trend_text("震荡") == "range"
    assert normalize_trend_text("bearish") == "bear"
    assert normalize_trend_text("") == "unknown"
    assert normalize_trend_text("???") == "unknown"


def test_the_data_contract_is_fixed_in_code_not_configured() -> None:
    # The endpoint serves one strategy, so its shape is a constant: three
    # periods, one answer slot each, a fixed bar count. Nothing an admin edits
    # can make the EA upload a list the server cannot answer.
    assert REGIME_PERIODS == ("M15", "H4", "D1")
    assert len(REGIME_PERIODS) == 3
    assert REGIME_KLINE_COUNT == 100


def _candle(bar: dict):
    from app.models import Candle

    return Candle(
        timestamp=bar["time"], open=bar["open"], high=bar["high"],
        low=bar["low"], close=bar["close"], volume=bar["volume"],
    )


# ── the endpoint ──────────────────────────────────────────────────────────────


def test_regime_answers_from_rules_when_no_ai_is_configured(tmp_path: Path) -> None:
    app = _app(tmp_path, "regime-rules.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_regime_rules", _vip_user(store))

        response = client.post("/mt5/strategy/regime", json=_payload(
            "gl_regime_rules", {"M15": _bars(UP), "H4": _bars(UP), "D1": _bars(DOWN)},
        ))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ok"
        assert body["short"]["trend"] == "bull"
        assert body["short"]["timeframe"] == "M15"
        assert body["long"]["trend"] == "bear"
        assert body["cached"] is False
        assert body["valid_until"]
        assert "未取得AI分析" in body["description"]
        # The fallback still explains itself instead of being an error string.
        assert body["short"]["detail"]


def test_the_request_is_answered_without_waiting_for_the_ai(tmp_path: Path) -> None:
    # This is the whole point of the design. An AI read takes tens of seconds and
    # the MT5 WebRequest carrying the request gives up long before that, so the
    # first call is answered from the server's own bar read while the AI read is
    # computed behind it, and the next call picks that up.
    app = _app(tmp_path, "regime-ai.db")
    calls: list[dict] = []

    def fake_provider(**kwargs):
        calls.append(kwargs)
        return _ai_response({
            "short": {"trend": "多", "detail": "短周期均线向上。"},
            "mid": {"trend": "range", "detail": "中周期区间震荡。"},
            "long": {"trend": "bull", "detail": "长周期多头延续。"},
        })

    with TestClient(app) as client:
        store = app.state.store
        _default_endpoint(store)
        _deploy(store, "gl_regime_ai", _vip_user(store))
        import app.services.ai_service as ai_service

        original = ai_service.AiDecisionClient._post_chat_completion
        ai_service.AiDecisionClient._post_chat_completion = lambda self, **kwargs: fake_provider(**kwargs)
        try:
            payload = _payload("gl_regime_ai", {"M15": _bars(UP), "H4": _bars(FLAT), "D1": _bars(UP)})
            first = client.post("/mt5/strategy/regime", json=payload).json()
            second = client.post("/mt5/strategy/regime", json=payload).json()
        finally:
            ai_service.AiDecisionClient._post_chat_completion = original

        # First answer: the server's own read, produced while the AI was still out.
        assert first["cached"] is False
        assert "未取得AI分析" in first["description"]
        assert first["short"]["trend"] == "bull"
        assert len(calls) == 1

        # Second answer: the AI read, served instantly from the stored snapshot.
        assert second["cached"] is True
        assert second["evaluated_at"]
        # The Chinese word is folded onto the value the EA compares.
        assert second["short"]["trend"] == "bull"
        assert second["short"]["detail"] == "短周期均线向上。"
        assert second["mid"]["trend"] == "range"
        assert second["long"]["trend"] == "bull"


def test_regime_keeps_the_rule_read_when_the_ai_answer_is_unusable(tmp_path: Path) -> None:
    app = _app(tmp_path, "regime-bad-ai.db")

    with TestClient(app) as client:
        store = app.state.store
        _default_endpoint(store)
        _deploy(store, "gl_regime_bad_ai", _vip_user(store))
        import app.services.ai_service as ai_service

        original = ai_service.AiDecisionClient._post_chat_completion
        # An answer that satisfies the contract with a word nobody can read.
        ai_service.AiDecisionClient._post_chat_completion = lambda self, **kwargs: _ai_response({
            "short": {"trend": "???", "detail": ""},
        })
        try:
            payload = _payload("gl_regime_bad_ai", {"M15": _bars(UP), "H4": _bars(UP), "D1": _bars(UP)})
            client.post("/mt5/strategy/regime", json=payload)
            body = client.post("/mt5/strategy/regime", json=payload).json()
        finally:
            ai_service.AiDecisionClient._post_chat_completion = original

        assert body["short"]["trend"] == "bull"
        assert body["mid"]["trend"] == "bull"
        assert body["long"]["trend"] == "bull"
        assert body["short"]["detail"] != ""
        assert "未取得AI分析" in body["description"]


def test_regime_survives_a_provider_failure(tmp_path: Path) -> None:
    app = _app(tmp_path, "regime-ai-down.db")

    with TestClient(app) as client:
        store = app.state.store
        _default_endpoint(store)
        _deploy(store, "gl_regime_ai_down", _vip_user(store))
        import app.services.ai_service as ai_service

        def explode(self, **kwargs):
            raise RuntimeError("provider down")

        original = ai_service.AiDecisionClient._post_chat_completion
        ai_service.AiDecisionClient._post_chat_completion = explode
        try:
            payload = _payload("gl_regime_ai_down", {"M15": _bars(UP), "H4": _bars(FLAT), "D1": _bars(DOWN)})
            response = client.post("/mt5/strategy/regime", json=payload)
            body = client.post("/mt5/strategy/regime", json=payload).json()
        finally:
            ai_service.AiDecisionClient._post_chat_completion = original

        # The client still gets a usable answer both times.
        assert response.status_code == 200, response.text
        assert body["short"]["trend"] == "bull"
        assert body["long"]["trend"] == "bear"
        assert "未取得AI分析" in body["description"]


def test_regime_reuses_one_answer_inside_the_cache_window(tmp_path: Path) -> None:
    app = _app(tmp_path, "regime-cache.db")
    calls: list[int] = []

    def fake_provider(**kwargs):
        calls.append(1)
        return _ai_response({
            "short": {"trend": "bull", "detail": "短周期多头。"},
            "mid": {"trend": "bull", "detail": "中周期多头。"},
            "long": {"trend": "range", "detail": "长周期震荡。"},
        })

    with TestClient(app) as client:
        store = app.state.store
        _default_endpoint(store)
        _deploy(store, "gl_regime_cache", _vip_user(store))
        import app.services.ai_service as ai_service

        original = ai_service.AiDecisionClient._post_chat_completion
        ai_service.AiDecisionClient._post_chat_completion = lambda self, **kwargs: fake_provider(**kwargs)
        try:
            payload = _payload("gl_regime_cache", {"M15": _bars(UP), "H4": _bars(UP), "D1": _bars(UP)})
            first = client.post("/mt5/strategy/regime", json=payload).json()
            second = client.post("/mt5/strategy/regime", json=payload).json()
            third = client.post("/mt5/strategy/regime", json=payload).json()
        finally:
            ai_service.AiDecisionClient._post_chat_completion = original

        assert first["cached"] is False
        assert second["cached"] is True
        assert third["cached"] is True
        # One paid call answers every poll inside the window.
        assert len(calls) == 1
        assert second["short"]["trend"] == "bull"
        assert third["evaluated_at"] == second["evaluated_at"]


def test_regime_reports_the_periods_the_ea_did_not_upload(tmp_path: Path) -> None:
    app = _app(tmp_path, "regime-missing.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_regime_missing", _vip_user(store))

        response = client.post("/mt5/strategy/regime", json=_payload(
            "gl_regime_missing", {"M15": _bars(UP)},
        ))

        body = response.json()
        assert response.status_code == 200, response.text
        assert "H4" in body["description"] and "D1" in body["description"]
        assert body["mid"]["trend"] in {"range", "unknown"}
        assert body["mid"]["detail"]


def test_regime_accepts_the_payload_the_ea_already_sends_open(tmp_path: Path) -> None:
    # The EA builds one request body and posts it to both endpoints. Fields the
    # trend read does not use (data_type, balance, equity, the screenshot slots)
    # must be ignored, not rejected: a 422 here would make the EA keep two shapes
    # in step for no reason.
    app = _app(tmp_path, "regime-open-shape.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_regime_open_shape", _vip_user(store))

        payload = _payload(
            "gl_regime_open_shape", {"M15": _bars(UP), "H4": _bars(UP), "D1": _bars(DOWN)},
        )
        payload.update({"data_type": "kline", "balance": 1000.0, "equity": 1000.0})
        payload["market"].update({
            "screenshot": None,
            "screenshot_id": None,
            "metadata": {"contract_size": 100, "point": 0.01},
        })

        response = client.post("/mt5/strategy/regime", json=payload)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["short"]["trend"] == "bull"
        assert body["long"]["trend"] == "bear"


def test_regime_refuses_an_unknown_key_without_an_http_error(tmp_path: Path) -> None:
    app = _app(tmp_path, "regime-bad-key.db")
    with TestClient(app) as client:
        response = client.post("/mt5/strategy/regime", json=_payload(
            "gl_regime_absent_key", {"M15": _bars(UP)},
        ))

        body = response.json()
        assert response.status_code == 200, response.text
        assert body["short"]["trend"] == "unknown"
        assert "无效" in body["description"]


def test_init_tells_the_arbitrage_ea_which_periods_to_upload(tmp_path: Path) -> None:
    app = _app(tmp_path, "regime-init.db")
    with TestClient(app) as client:
        store = app.state.store
        user_id = _vip_user(store)
        _deploy(store, "gl_regime_init", user_id)
        _deploy(store, "gl_trend_init", user_id, code="GL_TREND_V1")

        arbitrage = client.post(
            "/mt5/strategy/init",
            json={"deployment_key": "gl_regime_init", "account": {"platform": "MT5", "login": "60064845"}},
        ).json()
        trend = client.post(
            "/mt5/strategy/init",
            json={"deployment_key": "gl_trend_init", "account": {"platform": "MT5", "login": "60064845"}},
        ).json()

        periods = [item["timeframe"] for item in arbitrage["strategy"]["secondary_timeframes"]]
        assert periods == ["M15", "H4", "D1"]
        assert arbitrage["strategy"]["secondary_timeframes"][0]["kline_count"] == 100
        # Every other strategy keeps the empty list older EAs already expect.
        assert trend["strategy"]["secondary_timeframes"] == []

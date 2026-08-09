from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import AccountIdentity, OpenEvaluateRequest, PositionEvaluateRequest, UsageSummary
from app.strategies.pa_agent_lite import PaAgentLiteStrategy


DEMO_KEY = "gl_test_demo_key"
ACCOUNT = {
    "platform": "MT5",
    "login": "123456",
    "server": "Demo-Server",
}


def _client(tmp_path):
    settings = Settings(
        environment="test",
        database_path=tmp_path / "test.db",
        demo_deployment_key=DEMO_KEY,
    )
    return TestClient(create_app(settings))


def _activate(client: TestClient):
    response = client.post(
        "/api/v1/ea/activate",
        json={
            "deployment_key": DEMO_KEY,
            "account": ACCOUNT,
            "ea_version": "0.1.0",
        },
    )
    assert response.status_code == 200
    return response.json()


def _candles(closes):
    return [
        {
            "timestamp": 1_750_000_000 + index * 900,
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 100,
        }
        for index, close in enumerate(closes)
    ]


def test_health_and_activation(tmp_path):
    with _client(tmp_path) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        activation = _activate(client)
        assert activation["strategy_code"] == "PA_MOCK_V1"
        assert activation["symbol"] == "XAUUSD"


def test_mt5_strategy_init_returns_runtime_config(tmp_path):
    with _client(tmp_path) as client:
        response = client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["protocol_version"] == 1.0
        assert payload["min_ea_version"] == 1.0
        assert payload["strategy"]["status"] == "active"
        assert payload["strategy"]["open_data_type"] == "kline"
        assert payload["strategy"]["open_kline_count"] == 100
        assert payload["strategy"]["position_data_type"] == "kline"
        assert payload["strategy"]["position_kline_count"] == 100
        assert payload["strategy"]["call_mode"] == "bar"
        assert payload["strategy"]["call_val"] == 1


def test_open_decision_is_idempotent(tmp_path):
    with _client(tmp_path) as client:
        _activate(client)
        payload = {
            "deployment_key": DEMO_KEY,
            "request_id": "req_open_000001",
            "account": ACCOUNT,
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "bar_time": 1_750_001_800,
            "bid": 3300.0,
            "ask": 3300.3,
            "spread_points": 30,
            "balance": 10_000,
            "equity": 10_000,
            "candles": _candles([3298.0, 3299.0, 3300.0]),
        }

        first = client.post("/api/v1/trading/open/evaluate", json=payload)
        second = client.post("/api/v1/trading/open/evaluate", json=payload)

        assert first.status_code == 200
        assert first.json()["action"] == "BUY"
        assert first.json()["idempotent"] is False
        assert second.json()["decision_id"] == first.json()["decision_id"]
        assert second.json()["idempotent"] is True


def test_mt5_open_decision_format(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        response = client.post(
            "/mt5/strategy/open-decision",
            json={
                "deployment_key": DEMO_KEY,
                "account": ACCOUNT,
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "data_type": "kline",
                "market": {
                    "bid": 3300.0,
                    "ask": 3300.3,
                    "spread": 30,
                    "bars": [
                        {
                            "time": "2026-07-16 15:00:00",
                            "open": 3297.5,
                            "high": 3299.0,
                            "low": 3297.0,
                            "close": 3298.0,
                            "volume": 100,
                        },
                        {
                            "time": "2026-07-16 15:15:00",
                            "open": 3298.5,
                            "high": 3300.0,
                            "low": 3298.0,
                            "close": 3299.0,
                            "volume": 100,
                        },
                        {
                            "time": "2026-07-16 15:30:00",
                            "open": 3299.5,
                            "high": 3301.0,
                            "low": 3299.0,
                            "close": 3300.0,
                            "volume": 100,
                        },
                    ],
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["should_open"] is True
        assert payload["description"] == "PA Base deterministic trend candidate"
        assert payload["spread"] == 30
        assert payload["orders_count"] == 1
        assert payload["orders"][0]["direction"] == "buy"
        assert payload["orders"][0]["volume"] == 0.01
        assert payload["orders"][0]["order_type"] == "market"


def test_mt5_open_decision_request_id_is_scoped_to_market_payload(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        base = {
            "deployment_key": DEMO_KEY,
            "request_id": "dep_fixed_for_mt5",
            "account": ACCOUNT,
            "timeframe": "M1",
            "data_type": "kline",
            "market": {
                "spread": 30,
                "bars": [
                    {
                        "time": 1784937120,
                        "open": 4053.0,
                        "high": 4053.2,
                        "low": 4051.8,
                        "close": 4052.0,
                        "volume": 100,
                    },
                    {
                        "time": 1784937180,
                        "open": 4052.0,
                        "high": 4052.2,
                        "low": 4050.8,
                        "close": 4051.0,
                        "volume": 100,
                    },
                    {
                        "time": 1784937240,
                        "open": 4051.0,
                        "high": 4051.2,
                        "low": 4049.8,
                        "close": 4050.0,
                        "volume": 100,
                    },
                ],
            },
        }
        xau_response = client.post(
            "/mt5/strategy/open-decision",
            json={
                **base,
                "symbol": "XAUUSD",
                "market": {
                    **base["market"],
                    "bid": 4053.19,
                    "ask": 4053.53,
                },
            },
        )
        btc_response = client.post(
            "/mt5/strategy/open-decision",
            json={
                **base,
                "symbol": "BTCUSD",
                "market": {
                    **base["market"],
                    "bid": 64417.71,
                    "ask": 64431.71,
                    "spread": 1400,
                },
            },
        )

        assert xau_response.status_code == 200
        assert btc_response.status_code == 200
        xau = xau_response.json()
        btc = btc_response.json()
        assert xau["request_id"] != btc["request_id"]
        assert xau["orders"][0]["price"] == 4053.19
        assert btc["orders"][0]["price"] == 64417.71
        assert btc["orders"][0]["sl"] == 64459.71
        assert btc["orders"][0]["tp"] == 64347.71


def test_mt5_open_decision_test_pending_order(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        response = client.post(
            "/mt5/strategy/open-decision",
            json={
                "deployment_key": DEMO_KEY,
                "request_id": "test_pending_buy_limit",
                "account": {"login": "60064845"},
                "symbol": "BTCUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 64400.0,
                    "ask": 64414.0,
                    "spread": 1400,
                    "bars": [],
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["should_open"] is True
        assert payload["orders_count"] == 1
        assert payload["orders"][0]["direction"] == "buy"
        assert payload["orders"][0]["order_type"] == "limit"
        assert payload["orders"][0]["price"] == 64358.0
        assert payload["orders"][0]["comment"] == "Test pending buy limit"


def test_mt5_open_decision_random_test_response(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        response = client.post(
            "/mt5/strategy/open-decision",
            json={
                "deployment_key": DEMO_KEY,
                "request_id": "test_random_open",
                "account": {"login": "60064845"},
                "symbol": "BTCUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 64400.0,
                    "ask": 64414.0,
                    "spread": 1400,
                    "bars": [],
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["orders_count"] == len(payload["orders"])
        assert payload["should_open"] == (payload["orders_count"] > 0)


def test_pa_agent_strategy_library_deployment_routes_to_pa_engine(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_pa_agent_test_key"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "PA Agent 两阶段K线策略",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "web_demo",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "summary": "基于价格行为特征预计算和两阶段 AI 分析流程设计。",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        bars = []
        for index in range(35):
            close = 100.0 + index * 0.5
            bars.append(
                {
                    "time": 1_784_900_000 + index * 60,
                    "open": close - 0.2,
                    "high": close + 0.1,
                    "low": close - 0.4,
                    "close": close,
                    "volume": 100,
                }
            )

        response = client.post(
            "/mt5/strategy/open-decision",
            json={
                "deployment_key": deployment_key,
                "request_id": "pa_agent_open_0001",
                "account": {"login": "60064845"},
                "symbol": "BTCUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 117.0,
                    "ask": 117.5,
                    "spread": 50,
                    "bars": bars,
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["should_open"] is True
        assert payload["orders_count"] == 1
        assert payload["orders"][0]["direction"] == "buy"
        assert payload["orders"][0]["price"] == 117.5
        assert payload["description"].startswith("PA Agent bullish setup")


def test_pa_agent_risk_position_sizing_uses_symbol_metadata(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_pa_agent_risk_lot"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "PA Agent Risk Lot",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "web_demo",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "position_size_mode": "risk",
                "fixed_volume": 0.01,
                "risk_base_mode": "balance_percent",
                "risk_percent": 1,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        bars = []
        for index in range(35):
            close = 100.0 + index * 0.5
            bars.append(
                {
                    "time": 1_784_900_000 + index * 60,
                    "open": close - 0.2,
                    "high": close + 0.1,
                    "low": close - 0.4,
                    "close": close,
                    "volume": 100,
                }
            )

        response = client.post(
            "/mt5/strategy/open-decision",
            json={
                "deployment_key": deployment_key,
                "request_id": "pa_agent_risk_lot_0001",
                "account": {"login": "60064845"},
                "symbol": "XAUUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "balance": 150000,
                "equity": 150000,
                "market": {
                    "bid": 117.0,
                    "ask": 117.5,
                    "spread": 50,
                    "metadata": {
                        "tick_size": 0.01,
                        "tick_value": 1,
                        "volume_min": 0.01,
                        "volume_step": 0.01,
                        "volume_max": 100,
                    },
                    "bars": bars,
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["orders_count"] == 1
        assert payload["orders"][0]["volume"] > 0.01


def test_pa_agent_risk_position_sizing_accepts_value_per_price(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_pa_agent_value_price"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "PA Agent Value Price",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "web_demo",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "position_size_mode": "risk",
                "fixed_volume": 0.01,
                "risk_base_mode": "fixed_loss",
                "risk_amount": 150,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        bars = []
        for index in range(35):
            close = 100.0 + index * 0.5
            bars.append(
                {
                    "time": 1_784_900_000 + index * 60,
                    "open": close - 0.2,
                    "high": close + 0.1,
                    "low": close - 0.4,
                    "close": close,
                    "volume": 100,
                }
            )

        response = client.post(
            "/mt5/strategy/open-decision",
            json={
                "deployment_key": deployment_key,
                "request_id": "pa_agent_value_price_0001",
                "account": {"login": "60064845"},
                "symbol": "XAUUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "balance": 150000,
                "equity": 150000,
                "market": {
                    "bid": 117.0,
                    "ask": 117.5,
                    "spread": 50,
                    "metadata": {
                        "value_per_price": 100,
                        "volume_min": 0.01,
                        "volume_step": 0.01,
                        "volume_max": 100,
                    },
                    "bars": bars,
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["orders_count"] == 1
        assert payload["orders"][0]["volume"] > 0.01


def test_pa_agent_risk_position_sizing_accepts_mt5_account_and_symbol_names(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_pa_agent_mt5_names"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "PA Agent MT5 Names",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "web_demo",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "position_size_mode": "risk",
                "fixed_volume": 0.01,
                "risk_base_mode": "balance_percent",
                "risk_percent": 1,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        bars = []
        for index in range(35):
            close = 100.0 + index * 0.5
            bars.append(
                {
                    "time": 1_784_900_000 + index * 60,
                    "open": close - 0.2,
                    "high": close + 0.1,
                    "low": close - 0.4,
                    "close": close,
                    "volume": 100,
                }
            )

        response = client.post(
            "/mt5/strategy/open-decision",
            json={
                "deployment_key": deployment_key,
                "request_id": "pa_agent_mt5_names_0001",
                "account": {
                    "login": "60064845",
                    "balance": 150000,
                    "equity": 150000,
                },
                "symbol": "XAUUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 117.0,
                    "ask": 117.5,
                    "spread": 50,
                    "metadata": {
                        "tick": 0.01,
                        "tickVal": 1,
                        "minLot": 0.01,
                        "lotStep": 0.01,
                        "maxLot": 100,
                    },
                    "bars": bars,
                },
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["orders_count"] == 1
        assert payload["orders"][0]["volume"] > 0.01


def test_pa_agent_strategy_can_use_ai_open_decision():
    class FakeAiClient:
        def pa_open_decision(self, **_kwargs):
            return SimpleNamespace(
                content={
                    "should_open": True,
                    "direction": "sell",
                    "confidence": 0.81,
                    "lot": 0.01,
                    "sl_distance_price": 4.0,
                    "tp_distance_price": 8.0,
                    "reason": "AI看空并给出明确风控距离",
                },
                usage=UsageSummary(ai_called=True, input_tokens=100, output_tokens=30, charged_points=130),
            )

    bars = []
    for index in range(35):
        close = 120.0 - index * 0.4
        bars.append(
            {
                "timestamp": 1_784_900_000 + index * 60,
                "open": close + 0.2,
                "high": close + 0.4,
                "low": close - 0.1,
                "close": close,
                "volume": 100,
            }
        )
    request = OpenEvaluateRequest(
        deployment_key="gl_pa_agent_ai_test",
        request_id="pa_agent_ai_open_0001",
        account=AccountIdentity(login="60064845"),
        symbol="XAUUSD",
        timeframe="M1",
        bar_time=1_784_902_040,
        bid=106.0,
        ask=106.3,
        spread_points=30,
        balance=10000,
        equity=10000,
        candles=bars,
    )
    deployment = {
        "id": "dep_test",
        "user_id": "17",
        "strategy_code": "PA_AGENT_V1",
        "strategy_name": "PA Agent 两阶段K线策略",
        "config": {
            "lot": 0.2,
            "fixed_volume": 0.2,
            "position_size_mode": "fixed",
            "summary": "",
            "open_logic": "",
        },
    }

    decision = PaAgentLiteStrategy(FakeAiClient()).evaluate_open(request, deployment)

    assert decision.action == "SELL"
    assert decision.lot == 0.2
    assert decision.entry == 106.0
    assert decision.sl and decision.sl > decision.entry
    assert decision.tp and decision.tp < decision.entry
    assert decision.usage.ai_called is True


def test_pa_agent_blocks_open_when_resistance_space_is_too_small():
    bars = []
    for index in range(40):
        close = 100.0 + index * 0.15
        bars.append(
            {
                "timestamp": 1_784_900_000 + index * 300,
                "open": close - 0.08,
                "high": close + 0.18,
                "low": close - 0.12,
                "close": close,
                "volume": 100,
            }
        )
    bars[-3]["high"] = bars[-1]["close"] + 0.05
    request = OpenEvaluateRequest(
        deployment_key="gl_pa_agent_space_test",
        request_id="pa_agent_space_0001",
        account=AccountIdentity(login="60064845"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=1_784_911_700,
        bid=105.7,
        ask=105.75,
        spread_points=5,
        balance=10000,
        equity=10000,
        candles=bars,
    )
    deployment = {
        "id": "dep_test",
        "user_id": "17",
        "strategy_code": "PA_AGENT_V1",
        "strategy_name": "PA Agent",
        "config": {"lot": 0.01},
    }

    decision = PaAgentLiteStrategy(None).evaluate_open(request, deployment)

    assert decision.action == "HOLD"
    assert "空间不足" in decision.reason


def test_pa_agent_cooldown_blocks_early_close():
    bars = []
    for index in range(40):
        close = 120.0 - index * 0.3
        bars.append(
            {
                "timestamp": 1_784_900_000 + index * 300,
                "open": close + 0.12,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 100,
            }
        )
    response_client = PaAgentLiteStrategy(None)
    request = PositionEvaluateRequest(
        deployment_key="gl_pa_agent_cooldown_test",
        request_id="pa_agent_cooldown_0001",
        account=AccountIdentity(login="60064845"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=bars[-1]["timestamp"],
        bid=108.0,
        ask=108.05,
        spread_points=5,
        candles=bars,
        positions=[
            {
                "ticket": "1001",
                "symbol": "XAUUSD",
                "side": "BUY",
                "volume": 0.01,
                "open_price": 108.2,
                "current_price": 108.0,
                "profit": -2.0,
                "open_time": bars[-1]["timestamp"],
            }
        ],
    )
    deployment = {
        "id": "dep_test",
        "user_id": "17",
        "strategy_code": "PA_AGENT_V1",
        "strategy_name": "PA Agent",
        "config": {"lot": 0.01, "max_loss_per_position": 100, "take_profit_per_position": 150},
    }

    decision = response_client.evaluate_position(request, deployment)

    assert decision.action == "HOLD"
    assert "新单冷却" in decision.reason


def test_admin_ai_provider_model_and_quota_endpoints(tmp_path):
    with _client(tmp_path) as client:
        provider = client.post(
            "/api/admin/ai/provider/save",
            json={
                "name": "DeepSeek",
                "provider_type": "openai_compatible",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-test-secret",
                "enabled": True,
                "sort": 10,
            },
        )
        assert provider.status_code == 200
        provider_payload = provider.json()
        assert provider_payload["code"] == 0
        provider_id = provider_payload["data"]["id"]

        provider_list = client.post("/api/admin/ai/provider/list", json={"page": 1, "size": 20})
        assert provider_list.status_code == 200
        assert provider_list.json()["data"]["total"] == 1
        assert provider_list.json()["data"]["list"][0]["api_key_masked"] == "sk-t...cret"
        assert "api_key" not in provider_list.json()["data"]["list"][0]

        model = client.post(
            "/api/admin/ai/model/save",
            json={
                "provider_id": provider_id,
                "name": "deepseek-chat",
                "display_name": "快速模型",
                "base_url": "https://api.deepseek.com/v1",
                "context_window": 64000,
                "input_token_rate": 1,
                "output_token_rate": 1,
                "billing_multiplier": 1,
                "is_default": True,
                "enabled": True,
            },
        )
        assert model.status_code == 200
        assert model.json()["data"]["provider_name"] == "DeepSeek"

        quota = client.post(
            "/api/admin/ai/quota/save",
            json={
                "user_id": "17",
                "monthly_quota": 100000,
                "extra_quota": 50000,
                "used_tokens": 1200,
                "reset_at": "2026-08-01",
            },
        )
        assert quota.status_code == 200
        assert quota.json()["data"]["available_tokens"] == 148800

        usage = client.post("/api/admin/ai/usage/list", json={"page": 1, "size": 20})
        assert usage.status_code == 200
        assert usage.json()["data"]["total"] == 0

        stats = client.post("/api/admin/ai/stats/overview", json={})
        assert stats.status_code == 200
        stats_payload = stats.json()["data"]
        assert stats_payload["summary"]["quota_user_count"] == 1
        assert stats_payload["summary"]["monthly_quota"] == 100000
        assert "strategies" in stats_payload


def test_mt5_history_sync_is_idempotent_and_updates_pnl_stats(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_history_sync_key"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "History Sync Strategy",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "17",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        payload = {
            "deployment_key": deployment_key,
            "login": "60064845",
            "orders": [
                {
                    "order_id": 438123456,
                    "symbol": "XAUUSD",
                    "mt_type": 0,
                    "volume": 0.2,
                    "open_price": 4047.27,
                    "close_price": 4058.96,
                    "profit": -120.5,
                    "commission": -2.0,
                    "swap": 0,
                    "open_time": 1785480000,
                    "close_time": 1785480900,
                    "comment": "GainLabAI",
                }
            ],
        }

        first = client.post("/mt5/executions/history-sync", json=payload)
        assert first.status_code == 200
        assert first.json()["inserted_count"] == 1
        assert first.json()["updated_count"] == 0
        assert first.json()["profit_orders_count"] == 1
        assert first.json()["profit_deals_count"] == 1
        assert first.json()["net_profit"] == -122.5

        second_payload = {
            **payload,
            "orders": [
                {
                    **payload["orders"][0],
                    "profit": -100.0,
                    "commission": -1.5,
                }
            ],
        }
        second = client.post("/mt5/executions/history-sync", json=second_payload)
        assert second.status_code == 200
        assert second.json()["inserted_count"] == 0
        assert second.json()["updated_count"] == 1

        stats = client.post("/api/admin/ai/stats/overview", json={})
        assert stats.status_code == 200
        assert stats.json()["data"]["summary"]["pnl"] == -101.5


def test_mt5_history_sync_accepts_deal_id_without_order_id(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_history_deal_id_key"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "History Deal Id Strategy",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "17",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        response = client.post(
            "/mt5/executions/history-sync",
            json={
                "deployment_key": deployment_key,
                "login": "60064845",
                "orders": [
                    {
                        "deal_id": 330938065,
                        "symbol": "XAUUSD",
                        "mt_type": 1,
                        "volume": 0.01,
                        "open_price": 4093.25,
                        "close_price": 4093.25,
                        "profit": -2.2,
                        "commission": 0,
                        "swap": 0,
                        "open_time": 1785132000,
                        "close_time": 1785132000,
                        "comment": "",
                    }
                ],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["inserted_count"] == 1
        assert payload["net_profit"] == -2.2


def test_web_deployment_stats_returns_runtime_curve(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_history_curve_key"
        client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "History Curve Strategy",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "17",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        client.post(
            "/mt5/executions/history-sync",
            json={
                "deployment_key": deployment_key,
                "login": "60064845",
                "orders": [
                    {
                        "deal_id": 1,
                        "symbol": "XAUUSD",
                        "mt_type": 0,
                        "volume": 0.01,
                        "open_price": 4090,
                        "close_price": 4092,
                        "profit": 20,
                        "commission": -1,
                        "swap": 0,
                        "open_time": 1785130000,
                        "close_time": 1785130060,
                    },
                    {
                        "deal_id": 2,
                        "symbol": "BTCUSD",
                        "mt_type": 1,
                        "volume": 0.01,
                        "open_price": 64000,
                        "close_price": 64100,
                        "profit": -5,
                        "commission": 0,
                        "swap": 0,
                        "open_time": 1785130100,
                        "close_time": 1785130160,
                    },
                ],
            },
        )

        response = client.get(
            "/api/v1/web/deployments/stats",
            params={"deployment_key": deployment_key, "user_id": "17"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"]["pnl"] == 14
        assert payload["summary"]["win_count"] == 1
        assert payload["summary"]["loss_count"] == 1
        assert payload["summary"]["win_rate"] == 50
        assert payload["summary"]["traded_symbol_count"] == 2
        assert [point["cumulative_pnl"] for point in payload["curve"]] == [19, 14]

        orders_response = client.get(
            "/api/v1/web/deployments/orders",
            params={"deployment_key": deployment_key, "user_id": "17", "page": 1, "size": 10},
        )
        assert orders_response.status_code == 200
        orders_payload = orders_response.json()
        assert orders_payload["total"] == 2
        assert orders_payload["orders"][0]["order_id"] == "2"
        assert orders_payload["orders"][0]["open_price"] == 64000
        assert orders_payload["orders"][0]["close_price"] == 64100
        assert orders_payload["orders"][0]["net_profit"] == -5


def test_deployment_key_is_not_bound_to_mt_account(tmp_path):
    with _client(tmp_path) as client:
        _activate(client)
        response = client.post(
            "/api/v1/ea/heartbeat",
            json={
                "deployment_key": DEMO_KEY,
                "account": {
                    **ACCOUNT,
                    "login": "999999",
                },
                "terminal_time": datetime.now(timezone.utc).isoformat(),
                "auto_trading_enabled": True,
            },
        )
        assert response.status_code == 200
        assert response.json()["deployment_status"] == "active"


def test_position_reversal_returns_close(tmp_path):
    with _client(tmp_path) as client:
        _activate(client)
        response = client.post(
            "/api/v1/trading/position/evaluate",
            json={
                "deployment_key": DEMO_KEY,
                "request_id": "req_position_0001",
                "account": ACCOUNT,
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "bar_time": 1_750_001_800,
                "bid": 3297.0,
                "ask": 3297.3,
                "spread_points": 30,
                "candles": _candles([3300.0, 3299.0, 3298.0]),
                "positions": [
                    {
                        "ticket": "ticket-1",
                        "symbol": "XAUUSD",
                        "side": "BUY",
                        "volume": 0.01,
                        "open_price": 3300.0,
                        "current_price": 3297.0,
                        "profit": -3.0,
                    }
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["action"] == "CLOSE"
        assert response.json()["position_ticket"] == "ticket-1"


def test_mt5_position_decision_format(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        response = client.post(
            "/mt5/strategy/position-decision",
            json={
                "deployment_key": DEMO_KEY,
                "request_id": "risk_XAUUSD_M1_1784937240",
                "account": {
                    "login": "60064845",
                    "balance": 1430882.85,
                    "equity": 1430882.85,
                    "margin_free": 0,
                },
                "symbol": "XAUUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 3297.0,
                    "ask": 3297.3,
                    "spread": 30,
                    "bars": [
                        {
                            "time": 1784937120,
                            "open": 3301.0,
                            "high": 3301.2,
                            "low": 3299.8,
                            "close": 3300.0,
                            "volume": 100,
                        },
                        {
                            "time": 1784937180,
                            "open": 3300.0,
                            "high": 3300.2,
                            "low": 3298.8,
                            "close": 3299.0,
                            "volume": 100,
                        },
                        {
                            "time": 1784937240,
                            "open": 3299.0,
                            "high": 3299.2,
                            "low": 3297.8,
                            "close": 3298.0,
                            "volume": 100,
                        },
                    ],
                },
                "positions": [
                    {
                        "ticket": "ticket-1",
                        "symbol": "XAUUSD",
                        "mt_type": 0,
                        "volume": 0.01,
                        "open_price": 3300.0,
                        "current_price": 3297.0,
                        "sl": 3290.0,
                        "tp": 3310.0,
                        "profit": -3.0,
                        "open_time": 1784937000,
                        "comment": "GainLabAI",
                    }
                ],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["has_action"] is True
        assert payload["spread"] == 30
        assert payload["actions_count"] == 1
        assert payload["actions"][0]["action"] == "close"
        assert payload["actions"][0]["ticket"] == "ticket-1"
        assert payload["actions"][0]["mt_type"] == 0


def test_pa_agent_position_cooldown_accepts_reversed_bars_and_ms_open_time(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_pa_agent_cooldown_time"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "PA Agent Cooldown Time",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "web_demo",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        latest = 1_784_901_800
        bars = []
        for index in range(40):
            close = 100.0 + index * 0.1
            bars.append(
                {
                    "time": latest - index * 60,
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 100,
                }
            )

        response = client.post(
            "/mt5/strategy/position-decision",
            json={
                "deployment_key": deployment_key,
                "request_id": "pa_agent_cooldown_ms_0001",
                "account": {"login": "60064845"},
                "symbol": "XAUUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 103.9,
                    "ask": 104.2,
                    "spread": 30,
                    "bars": bars,
                },
                "positions": [
                    {
                        "ticket": "ticket-ms",
                        "symbol": "XAUUSD",
                        "mt_type": 0,
                        "volume": 0.01,
                        "open_price": 105.0,
                        "current_price": 103.9,
                        "sl": 95.0,
                        "tp": 125.0,
                        "profit": -1.0,
                        "open_time": (latest - 13 * 60) * 1000,
                        "comment": "GainLabAI",
                    }
                ],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert "opened 0 bars ago" not in payload["description"]


def test_pa_agent_risk_position_management_uses_risk_money_limit(tmp_path):
    with _client(tmp_path) as client:
        deployment_key = "gl_pa_agent_risk_position_limit"
        upsert = client.post(
            "/api/v1/web/deployments/upsert",
            json={
                "deployment_key": deployment_key,
                "name": "PA Agent Risk Position Limit",
                "status": "active",
                "strategy_code": "PA_AGENT_V1",
                "user_id": "web_demo",
                "open_data_type": "kline",
                "open_kline_count": 100,
                "position_data_type": "kline",
                "position_kline_count": 100,
                "position_size_mode": "risk",
                "fixed_volume": 0.01,
                "risk_base_mode": "balance_percent",
                "risk_percent": 1,
                "summary": "",
                "open_logic": "",
                "position_logic": "",
            },
        )
        assert upsert.status_code == 200

        bars = []
        for index in range(40):
            close = 100.0 + index * 0.05
            bars.append(
                {
                    "time": 1_784_900_000 + index * 60,
                    "open": close - 0.1,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 100,
                }
            )

        response = client.post(
            "/mt5/strategy/position-decision",
            json={
                "deployment_key": deployment_key,
                "request_id": "pa_agent_risk_position_limit_0001",
                "account": {
                    "login": "60064845",
                    "balance": 150000,
                    "equity": 149800,
                },
                "symbol": "XAUUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 101.8,
                    "ask": 102.1,
                    "spread": 30,
                    "bars": bars,
                },
                "positions": [
                    {
                        "ticket": "ticket-risk-limit",
                        "symbol": "XAUUSD",
                        "mt_type": 0,
                        "volume": 4.65,
                        "open_price": 103.0,
                        "current_price": 101.8,
                        "sl": 95.0,
                        "tp": 125.0,
                        "profit": -139.0,
                        "open_time": 1_784_899_000,
                        "comment": "GainLabAI",
                    }
                ],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["description"] != "Position reached configured maximum loss"


def test_mt5_position_decision_test_actions(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        base_payload = {
            "deployment_key": DEMO_KEY,
            "account": {"login": "60064845"},
            "symbol": "BTCUSD",
            "timeframe": "M1",
            "data_type": "kline",
            "market": {
                "bid": 64400.0,
                "ask": 64414.0,
                "spread": 1400,
                "bars": [
                    {
                        "time": 1784937240,
                        "open": 64400.0,
                        "high": 64420.0,
                        "low": 64380.0,
                        "close": 64410.0,
                        "volume": 100,
                    }
                ],
            },
            "positions": [
                {
                    "ticket": "ticket-1",
                    "symbol": "BTCUSD",
                    "mt_type": 1,
                    "volume": 0.01,
                    "open_price": 64482.5,
                    "current_price": 64400.0,
                    "profit": 0.5,
                }
            ],
        }

        cases = [
            ("test_add_buy", "add", 0, "buy"),
            ("test_add_sell", "add", 1, "sell"),
            ("test_modify", "modify", 1, None),
            ("test_cancel", "cancel", 1, None),
        ]
        for request_id, action_name, mt_type, direction in cases:
            response = client.post(
                "/mt5/strategy/position-decision",
                json={**base_payload, "request_id": request_id},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["has_action"] is True
            assert payload["actions_count"] == 1
            action = payload["actions"][0]
            assert action["action"] == action_name
            assert action["mt_type"] == mt_type
            assert action["direction"] == direction


def test_mt5_position_decision_random_test_response(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        response = client.post(
            "/mt5/strategy/position-decision",
            json={
                "deployment_key": DEMO_KEY,
                "request_id": "test_random_position",
                "account": {"login": "60064845"},
                "symbol": "BTCUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 64400.0,
                    "ask": 64414.0,
                    "spread": 1400,
                    "bars": [],
                },
                "positions": [
                    {
                        "ticket": "ticket-1",
                        "symbol": "BTCUSD",
                        "mt_type": 1,
                        "volume": 0.01,
                        "open_price": 64482.5,
                        "current_price": 64400.0,
                        "profit": 0.5,
                    }
                ],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["actions_count"] == len(payload["actions"])
        assert payload["has_action"] == (payload["actions_count"] > 0)


def test_mt5_position_decision_random_all_returns_one_action_per_position(tmp_path):
    with _client(tmp_path) as client:
        client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": DEMO_KEY,
                "ea_version": 1.0,
            },
        )
        positions = [
            {
                "ticket": f"ticket-{index}",
                "symbol": "BTCUSD",
                "mt_type": index % 2,
                "volume": 0.01,
                "open_price": 64482.5,
                "current_price": 64400.0,
                "profit": 0.5,
            }
            for index in range(4)
        ]
        response = client.post(
            "/mt5/strategy/position-decision",
            json={
                "deployment_key": DEMO_KEY,
                "request_id": "test_random_all",
                "account": {"login": "60064845"},
                "symbol": "BTCUSD",
                "timeframe": "M1",
                "data_type": "kline",
                "market": {
                    "bid": 64400.0,
                    "ask": 64414.0,
                    "spread": 1400,
                    "bars": [],
                },
                "positions": positions,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["has_action"] is True
        assert payload["actions_count"] == 4
        assert [item["ticket"] for item in payload["actions"]] == [
            "ticket-0",
            "ticket-1",
            "ticket-2",
            "ticket-3",
        ]

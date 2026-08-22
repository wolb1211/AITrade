from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.services.ai_service import AiDecisionClient
from app.store import SqliteStore


def test_ai_endpoint_prices_are_saved_per_model(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "pricing.db")
    store.initialize()

    saved = store.save_ai_endpoint(
        {
            "id": "aie_pricing_test",
            "owner_type": "gl",
            "name": "Pricing test model",
            "base_url": "https://example.com/v1",
            "model": "example-model",
            "api_key": "sk-test-price",
            "input_price_per_million": "1.250000",
            "output_price_per_million": "8.500000",
        }
    )

    assert saved["input_price_per_million"] == "1.25"
    assert saved["output_price_per_million"] == "8.5"

    updated = store.save_ai_endpoint(
        {
            "id": "aie_pricing_test",
            "name": "Pricing test model",
            "base_url": "https://example.com/v1",
            "model": "example-model",
        }
    )

    assert updated["input_price_per_million"] == "1.25"
    assert updated["output_price_per_million"] == "8.5"
    assert updated["api_key_masked"] == "sk-t...rice"


def test_ai_client_uses_model_prices_for_realtime_charge(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "pricing-call.db")
    store.initialize()
    user = store.save_user({"email": "pricing-call@example.com", "status": "active"})
    store.save_ai_endpoint({
        "id": "aie_realtime_price",
        "owner_type": "gl",
        "name": "Realtime pricing model",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "sk-realtime-price",
        "is_default": True,
        "input_price_per_million": "2",
        "output_price_per_million": "8",
    })
    client = AiDecisionClient(store)
    provider_response = json.dumps({
        "choices": [{"message": {"content": json.dumps({
            "should_open": False,
            "direction": None,
            "confidence": 0.5,
            "reason": "测试观望",
            "analysis": "测试实时计费链路",
        }, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
    }, ensure_ascii=False)
    monkeypatch.setattr(client, "_post_chat_completion", lambda **_kwargs: provider_response)

    result = client._chat_json(
        deployment={"id": "dep_price", "user_id": str(user["id"]), "strategy_code": "PA_AGENT_V1", "config": {}},
        endpoint="open",
        system_prompt="test",
        user_payload={"account": {"login": "123456", "server": "Test"}, "symbol": "XAUUSD", "timeframe": "M5"},
    )

    assert result is not None
    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 500
    assert Decimal(store.get_user(user["id"])["ai_balance"]) == Decimal("-0.006")
    usage = store.list_ai_usage_logs(page=1, size=10, user_id=str(user["id"]))["list"][0]
    assert usage["billing_source"] == "official"
    assert Decimal(usage["input_price_snapshot"]) == Decimal("2")
    assert Decimal(usage["output_price_snapshot"]) == Decimal("8")
    assert Decimal(usage["charged_amount"]) == Decimal("0.006")

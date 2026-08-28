from __future__ import annotations

import json
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from urllib import error, request

from app.services.ai_service import AiDecisionClient
from app.store import SqliteStore


def test_full_chat_completions_url_and_bearer_header_are_supported(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "full-chat-url.db")
    store.initialize()
    client = AiDecisionClient(store)
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"OK"}}]}'

    def fake_urlopen(req: request.Request, timeout: float):
        captured["url"] = req.full_url
        captured["authorization"] = req.get_header("Authorization")
        captured["body"] = json.loads((req.data or b"{}").decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(request, "urlopen", fake_urlopen)
    client._post_chat_completion(
        base_url="https://api.example.com/v1/chat/completions/",
        api_key="sk-header-only",
        model="example-model",
        system_prompt="test",
        user_prompt="test",
        max_tokens=16,
        strict_json=False,
    )

    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["authorization"] == "Bearer sk-header-only"
    assert "api_key" not in captured["body"]


def test_response_format_rejection_retries_openai_compatible_body(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "response-format-fallback.db")
    store.initialize()
    client = AiDecisionClient(store)
    request_bodies = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"status\\":\\"ok\\"}"}}]}'

    def fake_urlopen(req: request.Request, timeout: float):
        request_bodies.append(json.loads((req.data or b"{}").decode("utf-8")))
        if len(request_bodies) == 1:
            raise error.HTTPError(
                req.full_url,
                404,
                "unsupported request format",
                hdrs=None,
                fp=BytesIO(b'{"error":{"message":"request path format is incorrect"}}'),
            )
        return FakeResponse()

    monkeypatch.setattr(request, "urlopen", fake_urlopen)
    result = client.test_configuration(
        base_url="https://api.example.com/v1",
        api_key="sk-fallback-test",
        model="example-model",
        strict_json=True,
    )

    assert result["success"] is True
    assert len(request_bodies) == 2
    assert request_bodies[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in request_bodies[1]


def test_vision_parameter_rejection_retries_new_and_minimal_parameters(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "vision-parameter-fallback.db")
    store.initialize()
    client = AiDecisionClient(store)
    request_bodies = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"RED CIRCLE, BLUE SQUARE"}}]}'

    def fake_urlopen(req: request.Request, timeout: float):
        request_bodies.append(json.loads((req.data or b"{}").decode("utf-8")))
        if len(request_bodies) < 3:
            raise error.HTTPError(
                req.full_url,
                404,
                "parameter error",
                hdrs=None,
                fp=BytesIO(b'{"error":{"message":"Parameter error"}}'),
            )
        return FakeResponse()

    monkeypatch.setattr(request, "urlopen", fake_urlopen)
    result = client.test_vision_configuration(
        base_url="https://api.example.com/v1",
        api_key="sk-vision-fallback",
        model="vision-model",
    )

    assert result["success"] is True
    assert len(request_bodies) == 3
    assert request_bodies[0]["max_tokens"] == 512
    assert request_bodies[0]["temperature"] == 0
    assert request_bodies[1]["max_completion_tokens"] == 512
    assert "temperature" not in request_bodies[1]
    assert "max_tokens" not in request_bodies[1]
    assert "max_tokens" not in request_bodies[2]
    assert "max_completion_tokens" not in request_bodies[2]


def test_ai_endpoint_connection_test_does_not_create_billing_log(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "endpoint-connection-test.db")
    store.initialize()
    store.save_ai_endpoint({
        "id": "aie_connection_test",
        "owner_type": "gl",
        "name": "Connection test",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "sk-connection-test",
        "strict_json": True,
    })
    client = AiDecisionClient(store)
    provider_args = {}

    def fake_provider(**kwargs):
        provider_args.update(kwargs)
        return json.dumps({
            "choices": [{"message": {"content": '{"status":"ok"}'}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        })

    monkeypatch.setattr(client, "_post_chat_completion", fake_provider)
    result = client.test_endpoint("aie_connection_test")

    assert result["success"] is True
    assert result["model"] == "example-model"
    assert result["total_tokens"] == 12
    assert provider_args["strict_json"] is True
    assert store.list_ai_usage_logs(page=1, size=10)["total"] == 0


def test_unsaved_custom_ai_connection_test_has_no_side_effects(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "custom-connection-test.db")
    store.initialize()
    client = AiDecisionClient(store)
    provider_args = {}

    def fake_provider(**kwargs):
        provider_args.update(kwargs)
        return json.dumps({
            "choices": [{"message": {"content": '{"status":"ok"}'}}],
            "usage": {"prompt_tokens": 6, "completion_tokens": 3, "total_tokens": 9},
        })

    monkeypatch.setattr(client, "_post_chat_completion", fake_provider)
    result = client.test_configuration(
        base_url="https://custom.example.com/v1",
        api_key="sk-custom-test",
        model="custom-model",
    )

    assert result["success"] is True
    assert result["endpoint_id"] == ""
    assert result["total_tokens"] == 9
    assert provider_args["base_url"] == "https://custom.example.com/v1"
    assert provider_args["api_key"] == "sk-custom-test"
    assert provider_args["strict_json"] is True
    assert store.list_ai_usage_logs(page=1, size=10)["total"] == 0


def test_vision_test_sends_image_and_persists_capability(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "vision-test.db")
    store.initialize()
    store.save_ai_endpoint({
        "id": "aie_vision_test",
        "owner_type": "gl",
        "name": "Vision test",
        "base_url": "https://example.com/v1",
        "model": "vision-model",
        "api_key": "sk-vision-test",
    })
    client = AiDecisionClient(store)
    provider_args = {}

    def fake_provider(**kwargs):
        provider_args.update(kwargs)
        return json.dumps({
            "choices": [{"message": {"content": "RED CIRCLE, BLUE SQUARE"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    monkeypatch.setattr(client, "_post_chat_completion", fake_provider)
    result = client.test_vision_endpoint("aie_vision_test")
    saved = store.get_ai_endpoint("aie_vision_test")

    assert result["supports_vision"] is True
    assert provider_args["user_image_url"].startswith("data:image/jpeg;base64,")
    assert "RED CIRCLE" not in provider_args["user_prompt"]
    assert "BLUE SQUARE" not in provider_args["user_prompt"]
    assert saved is not None
    assert saved["supports_vision"] is True
    assert saved["vision_test_status"] == "passed"
    assert saved["vision_tested_at"]
    assert store.list_ai_usage_logs(page=1, size=10)["total"] == 0


def test_editing_model_resets_vision_verification(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "vision-reset.db")
    store.initialize()
    payload = {
        "id": "aie_vision_reset",
        "owner_type": "gl",
        "name": "Vision reset",
        "base_url": "https://example.com/v1",
        "model": "vision-model",
        "api_key": "sk-vision-test",
    }
    store.save_ai_endpoint(payload)
    store.save_ai_endpoint_vision_test("aie_vision_reset", passed=True)
    store.save_ai_endpoint({**payload, "model": "different-model", "api_key": ""})
    saved = store.get_ai_endpoint("aie_vision_reset")

    assert saved is not None
    assert saved["supports_vision"] is False
    assert saved["vision_test_status"] == "untested"
    assert saved["vision_tested_at"] == ""


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
    request_snapshot = json.loads(usage["request_snapshot"])
    assert request_snapshot["model"] == "example-model"
    assert request_snapshot["messages"][0]["role"] == "system"
    assert json.loads(request_snapshot["messages"][1]["content"])["symbol"] == "XAUUSD"
    assert "sk-realtime-price" not in usage["request_snapshot"]


def test_ai_client_reuses_cached_result_but_records_and_charges_each_request(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "cached-pricing-call.db")
    store.initialize()
    user = store.save_user({"email": "cached-pricing@example.com", "status": "active"})
    store.save_ai_endpoint({
        "id": "aie_cached_price",
        "owner_type": "gl",
        "name": "Cached pricing model",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "sk-cached-price",
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
            "reason": "缓存测试观望",
            "analysis": "相同分析内容应复用结果，但每个用户请求仍独立记录并计费。",
        }, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
    }, ensure_ascii=False)
    provider_calls = 0

    def fake_provider(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return provider_response

    monkeypatch.setattr(client, "_post_chat_completion", fake_provider)
    deployment = {"id": "dep_cache", "user_id": str(user["id"]), "strategy_code": "PA_AGENT_V1", "config": {}}
    payload = {"account": {"login": "123456", "server": "Test"}, "symbol": "XAUUSD", "timeframe": "M5"}

    first = client._chat_json(deployment=deployment, endpoint="open", system_prompt="test", user_payload=payload)
    second = client._chat_json(deployment=deployment, endpoint="open", system_prompt="test", user_payload=payload)

    assert first is not None and second is not None
    assert first.content == second.content
    assert provider_calls == 1
    assert Decimal(store.get_user(user["id"])["ai_balance"]) == Decimal("-0.012")
    usage = store.list_ai_usage_logs(page=1, size=10, user_id=str(user["id"]))
    assert usage["total"] == 2
    assert usage["list"][0]["response_source"] == "cache"
    assert usage["list"][0]["provider_called"] is False
    assert usage["list"][0]["cache_id"]
    assert usage["list"][1]["response_source"] == "provider"
    assert usage["list"][1]["provider_called"] is True
    assert usage["summary"]["provider_calls"] == 1
    assert usage["summary"]["cache_hits"] == 1
    assert usage["summary"]["cache_hit_rate"] == 50.0
    assert usage["summary"]["provider_input_tokens"] == 1000
    assert usage["summary"]["provider_output_tokens"] == 500

    changed_payload = {**payload, "timeframe": "M15"}
    client._chat_json(deployment=deployment, endpoint="open", system_prompt="test", user_payload=changed_payload)
    assert provider_calls == 2


def test_ai_client_does_not_cache_executable_trade_action(tmp_path: Path, monkeypatch) -> None:
    store = SqliteStore(tmp_path / "action-cache-safety.db")
    store.initialize()
    user = store.save_user({"email": "action-cache@example.com", "status": "active"})
    store.save_ai_endpoint({
        "id": "aie_action_cache",
        "owner_type": "gl",
        "name": "Action cache safety",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "sk-action-cache",
        "is_default": True,
    })
    client = AiDecisionClient(store)
    provider_calls = 0

    def fake_provider(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "should_open": True,
                "direction": "buy",
                "confidence": 0.8,
                "lot": 0.01,
                "sl_distance_price": 1,
                "tp_distance_price": 2,
                "reason": "测试开仓",
                "analysis": "带交易动作的结果不得被缓存重复下发。",
            }, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }, ensure_ascii=False)

    monkeypatch.setattr(client, "_post_chat_completion", fake_provider)
    deployment = {"id": "dep_action_cache", "user_id": str(user["id"]), "strategy_code": "PA_AGENT_V1", "config": {}}
    payload = {"account": {"login": "123456"}, "symbol": "XAUUSD", "timeframe": "M5"}
    client._chat_json(deployment=deployment, endpoint="open", system_prompt="test", user_payload=payload)
    client._chat_json(deployment=deployment, endpoint="open", system_prompt="test", user_payload=payload)

    assert provider_calls == 2
    usage = store.list_ai_usage_logs(page=1, size=10, user_id=str(user["id"]))
    assert usage["summary"]["cache_hits"] == 0
    assert usage["summary"]["provider_calls"] == 2

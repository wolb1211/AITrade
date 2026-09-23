"""/v1/models: the public interface answers only to a user API key."""

from __future__ import annotations

import json
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path) -> tuple[TestClient, list[str]]:
    app = create_app(Settings(database_path=tmp_path / "public-api.db"))
    store = app.state.store
    store.initialize()
    # A real user row, because billing reads and writes users.ai_balance.
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO users (id, email, password_hash, nickname, status, ai_balance,
                               created_at, updated_at)
            VALUES (7, 'api@example.com', 'x', 'api', 'active', 100,
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """
        )
    created = store.create_user_api_key(7, name="测试")
    return TestClient(app), [created["key"], created["id"]]


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_models_refuse_an_unknown_or_missing_key(client) -> None:
    test_client, _ = client

    assert test_client.get("/v1/models").status_code == 401
    assert test_client.get("/v1/models", headers=_headers("ak_deadbeef")).status_code == 401


def test_models_answer_with_a_valid_key(client) -> None:
    test_client, (key, _) = client

    response = test_client.get("/v1/models", headers=_headers(key))

    assert response.status_code == 200
    body = response.json()
    # The OpenAI shape, so an off-the-shelf client can read it.
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    for item in body["data"]:
        assert item["object"] == "model"
        assert item["pricing"]["currency"] == "CNY"
        assert item["pricing"]["unit"] == "per_million_tokens"
        # The provider and its base url never appear on this surface.
        assert "base_url" not in item


def test_a_revoked_key_is_refused(client) -> None:
    test_client, (key, key_id) = client
    assert test_client.get("/v1/models", headers=_headers(key)).status_code == 200

    test_client.app.state.store.revoke_user_api_key(7, key_id)

    assert test_client.get("/v1/models", headers=_headers(key)).status_code == 401


def test_the_key_record_never_exposes_the_secret(client) -> None:
    test_client, (key, _) = client
    listed = test_client.app.state.store.list_user_api_keys(7)

    assert listed and "key" not in listed[0]
    assert key not in str(listed)


def _seed_endpoint(store, *, model: str = "qwen-plus") -> None:
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO ai_endpoints
                (id, owner_type, template_code, name, base_url, model, api_key,
                 input_price_per_million, output_price_per_million,
                 cache_input_price_per_million, enabled, selectable_by_user,
                 is_default, sort, created_at, updated_at)
            VALUES ('ep_1', 'gl', 'openai_compatible', '通义千问',
                    'https://provider.test/v1', ?, 'sk-provider',
                    1.0, 2.4, 0, 1, 1, 1, 1, '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00')
            """,
            (model,),
        )


def _seed_balance(store, amount: str) -> None:
    store.adjust_ai_balance(
        user_id=7, amount=Decimal(amount), entry_type="admin_recharge", operator_id="test"
    )


def test_the_model_list_shows_a_selectable_model(client) -> None:
    test_client, (key, _) = client
    store = test_client.app.state.store
    _seed_endpoint(store)

    body = test_client.get("/v1/models", headers=_headers(key)).json()

    assert [item["id"] for item in body["data"]] == ["qwen-plus"]
    assert body["data"][0]["pricing"]["input"] == "1"


def test_a_completion_is_relayed_and_charged(client, monkeypatch) -> None:
    from app.api import public_api

    test_client, (key, _) = client
    store = test_client.app.state.store
    _seed_endpoint(store)
    relayed: dict[str, Any] = {}

    class _Answer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "你好"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 2000, "total_tokens": 3000},
            }).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        relayed["url"] = request.full_url
        relayed["auth"] = request.headers.get("Authorization")
        relayed["body"] = json.loads(request.data.decode("utf-8"))
        return _Answer()

    monkeypatch.setattr(public_api, "urlopen", fake_urlopen)

    response = test_client.post(
        "/v1/chat/completions",
        headers=_headers(key),
        json={"model": "qwen-plus", "messages": [{"role": "user", "content": "在吗"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "你好"
    # Our key and the provider url never leave the server, but the call went out.
    assert relayed["url"] == "https://provider.test/v1/chat/completions"
    assert relayed["auth"] == "Bearer sk-provider"
    assert relayed["body"]["model"] == "qwen-plus"
    # 1000 input at 1.00 and 2000 output at 2.40 per million = 0.001 + 0.0048.
    assert Decimal(str(store.get_user(7)["ai_balance"])) == Decimal("100") - Decimal("0.0058")


def test_an_empty_balance_stops_the_call_before_the_provider(client, monkeypatch) -> None:
    from app.api import public_api

    test_client, (key, _) = client
    store = test_client.app.state.store
    _seed_endpoint(store)
    # The fixture starts with a balance; drain it so this is the empty case.
    with store._connect() as connection:
        connection.execute("UPDATE users SET ai_balance = 0 WHERE id = 7")
    called = []
    monkeypatch.setattr(public_api, "urlopen", lambda *a, **k: called.append(1))

    response = test_client.post(
        "/v1/chat/completions",
        headers=_headers(key),
        json={"model": "qwen-plus", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 402
    assert called == []


def test_an_unknown_model_is_refused(client) -> None:
    test_client, (key, _) = client

    response = test_client.post(
        "/v1/chat/completions",
        headers=_headers(key),
        json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 404


def test_streaming_is_refused_rather_than_silently_buffered(client) -> None:
    test_client, (key, _) = client

    response = test_client.post(
        "/v1/chat/completions",
        headers=_headers(key),
        json={"model": "qwen-plus", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400

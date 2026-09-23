"""/v1/models: the public interface answers only to a user API key."""

from __future__ import annotations

import warnings
from pathlib import Path

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

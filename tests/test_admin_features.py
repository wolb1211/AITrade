from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import SqliteStore


def _admin_token(secret: str, *, roles: list[str] | None = None, expires_in: int = 300) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode({"id": 1, "roles": roles or ["admin"], "exp": int(time.time()) + expires_in})
    signed = f"{header}.{payload}"
    signature = hmac.new(secret.encode("utf-8"), signed.encode("ascii"), hashlib.sha256).digest()
    return f"{signed}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def test_ea_download_crud_and_public_visibility(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "ea-download.db")
    store.initialize()

    visible = store.save_ea_download({
        "name": "GainLab MT5 EA",
        "description": "MT5 installation package",
        "oss_url": "https://example.com/gainlab-mt5.zip",
        "file_name": "gainlab-mt5.zip",
        "file_size": 1024,
        "enabled": True,
        "sort": 0,
    })
    hidden = store.save_ea_download({
        "name": "Internal build",
        "oss_url": "https://example.com/internal.zip",
        "enabled": False,
        "sort": 1,
    })

    assert [item["id"] for item in store.list_ea_downloads()["list"]] == [visible["id"]]
    all_items = store.list_ea_downloads(include_disabled=True)["list"]
    assert [item["id"] for item in all_items] == [visible["id"], hidden["id"]]
    assert all_items[0]["sort"] == 0

    updated = store.save_ea_download({**visible, "name": "GainLab MT5 EA v2"})
    assert updated["name"] == "GainLab MT5 EA v2"
    store.delete_ea_download(hidden["id"])
    assert len(store.list_ea_downloads(include_disabled=True)["list"]) == 1


def test_referenced_ai_endpoint_cannot_be_deleted(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "endpoint-reference.db")
    store.initialize()
    endpoint = store.save_ai_endpoint({
        "id": "aie_referenced",
        "owner_type": "gl",
        "name": "Referenced model",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key": "sk-test",
    })
    store.save_official_ai_strategy({
        "id": "ofs_reference",
        "code": "REFERENCE_V1",
        "name": "Reference strategy",
        "open_ai_endpoint_id": endpoint["id"],
    })

    with pytest.raises(RuntimeError, match="ai_endpoint_in_use"):
        store.delete_ai_endpoint(endpoint["id"])


def test_saving_official_strategy_keeps_user_strategy_name(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "strategy-name.db")
    store.initialize()
    store.save_official_ai_strategy({"id": "ofs_name", "code": "NAME_V1", "name": "Official name"})
    store.upsert_web_deployment(
        "gl_custom_name_key",
        user_id="1",
        strategy_code="NAME_V1",
        strategy_name="My custom name",
        status="active",
        symbol="XAUUSD",
        timeframe="M5",
        config={"deployment_key": "gl_custom_name_key"},
    )

    store.save_official_ai_strategy({"id": "ofs_name", "code": "NAME_V1", "name": "New official name"})
    assert store.find_deployment_by_key("gl_custom_name_key")["strategy_name"] == "My custom name"


def test_admin_ai_routes_require_valid_admin_jwt(tmp_path: Path) -> None:
    secret = "test-admin-jwt-secret"
    app = create_app(Settings(database_path=tmp_path / "admin-auth.db", admin_jwt_secret=secret))
    with TestClient(app) as client:
        assert client.post("/api/admin/ai/ea-download/list", json={}).status_code == 401
        non_admin = _admin_token(secret, roles=["user"])
        assert client.post(
            "/api/admin/ai/ea-download/list",
            json={},
            headers={"Authorization": f"Bearer {non_admin}"},
        ).status_code == 403
        admin = _admin_token(secret)
        response = client.post(
            "/api/admin/ai/ea-download/list",
            json={},
            headers={"Authorization": f"Bearer {admin}"},
        )
        assert response.status_code == 200
        assert response.json()["code"] == 0


def test_guide_articles_only_publish_enabled_content(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "guides.db")
    store.initialize()
    visible = store.save_guide_article({
        "title": "Connect MT5",
        "summary": "Quick start",
        "sort": 0,
        "content": [
            {"type": "heading", "text": "Install EA"},
            {"type": "paragraph", "text": "Copy the EA into the Experts directory."},
            {"type": "image", "url": "https://example.com/install.png", "caption": "Install directory"},
            {"type": "script", "text": "ignored"},
        ],
    })
    hidden = store.save_guide_article({
        "title": "Draft",
        "enabled": False,
        "content": [{"type": "paragraph", "text": "Not public"}],
    })

    public_list = store.list_guide_articles()["list"]
    assert [item["id"] for item in public_list] == [visible["id"]]
    assert "content" not in public_list[0]
    assert store.get_guide_article(hidden["id"]) is None
    detail = store.get_guide_article(visible["id"])
    assert detail is not None
    assert [block["type"] for block in detail["content"]] == ["heading", "paragraph", "image"]
    assert detail["sort"] == 0


def test_public_guide_http_routes(tmp_path: Path) -> None:
    app = create_app(Settings(database_path=tmp_path / "guide-http.db"))
    with TestClient(app) as client:
        article = app.state.store.save_guide_article({
            "title": "MT4 setup",
            "content": [{"type": "paragraph", "text": "Allow WebRequest first."}],
        })
        listing = client.get("/api/v1/guides")
        assert listing.status_code == 200
        assert listing.json()["data"]["list"][0]["id"] == article["id"]
        detail = client.get(f"/api/v1/guides/{article['id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["content"][0]["type"] == "paragraph"
        assert client.get("/api/v1/guides/missing").status_code == 404

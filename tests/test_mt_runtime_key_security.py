from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _open_request(key: str) -> dict[str, object]:
    return {
        "deployment_key": key,
        "request_id": "runtime-key-security-open-001",
        "account": {"platform": "MT5", "login": "60064845"},
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "market": {
            "bid": 4561.8,
            "ask": 4562.0,
            "spread": 0.2,
            "bars": [],
        },
    }


def test_unknown_gl_key_is_not_auto_created(tmp_path: Path) -> None:
    app = create_app(Settings(
        environment="production",
        database_path=tmp_path / "unknown-key.db",
        auth_secret="test-auth-secret",
    ))
    key = "gl_local_key_that_does_not_exist_online"

    with TestClient(app) as client:
        response = client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": key,
                "account": {"platform": "MT5", "login": "60064845"},
            },
        )

        assert response.status_code == 401
        assert response.json()["detail"] == "invalid_deployment_key"
        assert app.state.store.find_deployment_by_key(key) is None


def test_legacy_runtime_mock_key_cannot_open_orders(tmp_path: Path) -> None:
    app = create_app(Settings(
        environment="production",
        database_path=tmp_path / "legacy-runtime-key.db",
        auth_secret="test-auth-secret",
    ))
    key = "gl_legacy_runtime_mock_key"

    with TestClient(app) as client:
        app.state.store.upsert_web_deployment(
            key,
            user_id="mt5_runtime",
            strategy_code="PA_MOCK_V1",
            strategy_name="MT5 Runtime PA Mock",
            status="active",
            symbol="XAUUSD",
            timeframe="M5",
            config={"lot": 0.01},
        )

        init_response = client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": key,
                "account": {"platform": "MT5", "login": "60064845"},
            },
        )
        assert init_response.status_code == 401
        assert init_response.json()["detail"] == "invalid_deployment_key"

        open_response = client.post(
            "/mt5/strategy/open-decision",
            json=_open_request(key),
        )
        assert open_response.status_code == 200
        assert open_response.json()["orders_count"] == 0
        assert open_response.json()["orders"] == []
        assert "invalid_deployment_key" in open_response.json()["description"]

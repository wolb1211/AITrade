from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_strategy_creation_requires_active_vip(tmp_path: Path) -> None:
    settings = Settings(
        environment="production",
        database_path=tmp_path / "vip-create.db",
        auth_secret="test-auth-secret",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        store = app.state.store
        user = store.save_user({"email": "vip-create@example.com", "status": "active"})
        payload = {
            "deployment_key": "gl_vip_permission_test",
            "name": "VIP permission test",
            "user_id": str(user["id"]),
        }
        denied = client.post("/api/v1/web/deployments/upsert", json=payload)
        assert denied.status_code == 403
        assert denied.json()["detail"] == "vip_required"

        expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        store.save_user({
            "id": user["id"],
            "email": user["email"],
            "status": "active",
            "vip_level": 1,
            "vip_expires_at": expires,
            "max_strategy_keys": 10,
        })
        allowed = client.post("/api/v1/web/deployments/upsert", json=payload)
        assert allowed.status_code == 200


def test_init_binds_only_mt_login(tmp_path: Path) -> None:
    settings = Settings(
        environment="production",
        database_path=tmp_path / "mt-login.db",
        auth_secret="test-auth-secret",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        store = app.state.store
        key = "gl_test_login_binding"
        store.save_user({
            "id": 17,
            "email": "mt-vip@example.com",
            "status": "active",
            "vip_level": 1,
            "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        })
        store.upsert_web_deployment(
            key,
            user_id="17",
            strategy_code="PA_MOCK_V1",
            strategy_name="MT login binding test",
            status="active",
            symbol="*",
            timeframe="*",
            config={"deployment_key": key, "ea_description": "EA端显示的用户填写说明"},
        )

        missing_account = client.post(
            "/mt5/strategy/init",
            json={"deployment_key": key},
        )
        assert missing_account.status_code == 422

        first = client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": key,
                "account": {
                    "platform": "MT5",
                    "login": "12345678",
                    "server": "Broker-Demo",
                },
            },
        )
        assert first.status_code == 200
        assert first.json()["strategy"]["name"] == "MT login binding test"
        assert first.json()["strategy"]["summary"] == "EA端显示的用户填写说明"
        assert store.find_deployment_by_key(key)["mt_login"] == "12345678"

        # Platform and server are deliberately different: only login is binding.
        same_login = client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": key,
                "account": {
                    "platform": "MT4",
                    "login": "12345678",
                    "server": "Another-Broker-Live",
                },
            },
        )
        assert same_login.status_code == 200

        different_login = client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": key,
                "account": {
                    "platform": "MT5",
                    "login": "87654321",
                    "server": "Broker-Demo",
                },
            },
        )
        assert different_login.status_code == 200
        assert different_login.json()["strategy"]["status"] == (
            "当前 MT 账号与策略绑定账号不一致，请在用户中心修改绑定账号"
        )
        assert store.find_deployment_by_key(key)["mt_login"] == "12345678"

        mismatched_request = {
            "deployment_key": key,
            "request_id": "mismatched-account-open-001",
            "account": {"platform": "MT5", "login": "87654321"},
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "market": {"bid": 2500.0, "ask": 2500.2, "spread": 0.2, "bars": []},
        }
        mismatched_open = client.post("/mt5/strategy/open-decision", json=mismatched_request)
        assert mismatched_open.status_code == 200
        assert mismatched_open.json()["orders_count"] == 0
        assert "deployment_account_mismatch" in mismatched_open.json()["description"]

        mismatched_position = client.post(
            "/mt5/strategy/position-decision",
            json={
                **mismatched_request,
                "request_id": "mismatched-account-position-001",
                "positions": [{
                    "ticket": "10001",
                    "symbol": "XAUUSD",
                    "direction": "buy",
                    "volume": 0.1,
                    "open_price": 2499.0,
                    "current_price": 2500.0,
                }],
            },
        )
        assert mismatched_position.status_code == 200
        assert mismatched_position.json()["actions_count"] == 0
        assert "deployment_account_mismatch" in mismatched_position.json()["description"]

        store.set_deployment_login(key, "")
        rebound = client.post(
            "/mt5/strategy/init",
            json={
                "deployment_key": key,
                "account": {"platform": "MT4", "login": "87654321"},
            },
        )
        assert rebound.status_code == 200
        assert store.find_deployment_by_key(key)["mt_login"] == "87654321"


def test_expired_vip_is_rejected_before_mt_analysis(tmp_path: Path) -> None:
    settings = Settings(
        environment="production",
        database_path=tmp_path / "expired-vip.db",
        auth_secret="test-auth-secret",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        store = app.state.store
        user = store.save_user({
            "email": "expired@example.com",
            "status": "active",
            "vip_level": 1,
            "vip_expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        })
        key = "gl_expired_vip_test"
        store.upsert_web_deployment(
            key,
            user_id=str(user["id"]),
            strategy_code="PA_MOCK_V1",
            strategy_name="Expired VIP test",
            status="active",
            symbol="*",
            timeframe="*",
            config={"deployment_key": key},
        )
        response = client.post(
            "/mt5/strategy/init",
            json={"deployment_key": key, "account": {"login": "123456"}},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["strategy"]["status"] == "VIP 已到期，策略分析已停止"
        assert store.find_deployment_by_key(key)["mt_login"] is None

        common_request = {
            "deployment_key": key,
            "request_id": "expired-vip-request-001",
            "account": {"login": "123456"},
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "market": {"bid": 2500.0, "ask": 2500.2, "spread": 0.2, "bars": []},
        }
        open_response = client.post("/mt5/strategy/open-decision", json=common_request)
        assert open_response.status_code == 200
        assert open_response.json()["orders_count"] == 0
        assert open_response.json()["orders"] == []
        assert "vip_expired" in open_response.json()["description"]

        position_response = client.post(
            "/mt5/strategy/position-decision",
            json={
                **common_request,
                "request_id": "expired-vip-request-002",
                "positions": [{
                    "ticket": "10001",
                    "symbol": "XAUUSD",
                    "direction": "buy",
                    "volume": 0.1,
                    "open_price": 2499.0,
                    "current_price": 2500.0,
                }],
            },
        )
        assert position_response.status_code == 200
        assert position_response.json()["actions_count"] == 0
        assert position_response.json()["actions"] == []
        assert "vip_expired" in position_response.json()["description"]


def test_exhausted_ai_credit_is_rejected_by_all_mt_strategy_endpoints(tmp_path: Path) -> None:
    settings = Settings(
        environment="production",
        database_path=tmp_path / "exhausted-credit.db",
        auth_secret="test-auth-secret",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        store = app.state.store
        user = store.save_user({
            "email": "exhausted@example.com",
            "status": "active",
            "vip_level": 1,
            "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        })
        store.adjust_ai_balance(
            user_id=user["id"],
            amount=Decimal("-10"),
            entry_type="admin_deduction",
        )
        key = "gl_exhausted_credit_test"
        store.upsert_web_deployment(
            key,
            user_id=str(user["id"]),
            strategy_code="PA_AGENT_V1",
            strategy_name="Exhausted credit test",
            status="active",
            symbol="*",
            timeframe="*",
            config={"deployment_key": key, "open_ai_mode": "official", "position_ai_mode": "official"},
        )

        init_response = client.post(
            "/mt5/strategy/init",
            json={"deployment_key": key, "account": {"login": "123456"}},
        )
        assert init_response.status_code == 200
        assert init_response.json()["strategy"]["status"] == "AI 余额不足，策略分析已停止，请充值后继续使用"

        common_request = {
            "deployment_key": key,
            "request_id": "exhausted-credit-001",
            "account": {"login": "123456"},
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "market": {"bid": 2500.0, "ask": 2500.2, "spread": 0.2, "bars": []},
        }
        open_response = client.post("/mt5/strategy/open-decision", json=common_request)
        assert open_response.status_code == 200
        assert open_response.json()["orders_count"] == 0
        assert "insufficient_balance" in open_response.json()["description"]

        position_response = client.post(
            "/mt5/strategy/position-decision",
            json={
                **common_request,
                "request_id": "exhausted-credit-002",
                "positions": [{
                    "ticket": "10001",
                    "symbol": "XAUUSD",
                    "direction": "buy",
                    "volume": 0.1,
                    "open_price": 2499.0,
                    "current_price": 2500.0,
                }],
            },
        )
        assert position_response.status_code == 200
        assert position_response.json()["actions_count"] == 0
        assert "insufficient_balance" in position_response.json()["description"]

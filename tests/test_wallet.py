from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.decision_service import DecisionService
from app.store import SqliteStore


def test_wallet_settings_and_balance_ledger(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "wallet.db")
    store.initialize()
    user = store.save_user({"email": "wallet@example.com", "status": "active"})

    assert store.get_ai_billing_settings() == {
        "credit_limit": "10.000000",
        "low_balance_threshold": "10.000000",
    }

    settings = store.save_ai_billing_settings(
        credit_limit=Decimal("12.5"),
        low_balance_threshold=Decimal("8"),
    )
    assert settings == {
        "credit_limit": "12.5",
        "low_balance_threshold": "8",
    }

    recharged = store.adjust_ai_balance(
        user_id=user["id"],
        amount=Decimal("20.25"),
        entry_type="admin_recharge",
        remark="测试充值",
    )
    assert recharged["user"]["ai_balance"] == "20.25"
    assert recharged["user"]["available_balance"] == "32.75"

    deducted = store.adjust_ai_balance(
        user_id=user["id"],
        amount=Decimal("-25"),
        entry_type="admin_deduction",
        remark="测试扣减",
    )
    assert deducted["user"]["ai_balance"] == "-4.75"
    assert deducted["user"]["available_balance"] == "7.75"
    assert deducted["user"]["balance_warning"] is True
    assert deducted["user"]["credit_exhausted"] is False

    ledger = store.list_ai_balance_ledger(page=1, size=20, user_id=user["id"])
    assert ledger["total"] == 2
    assert ledger["list"][0]["entry_type"] == "admin_deduction"
    assert ledger["list"][0]["amount"] == "-25"
    assert ledger["list"][1]["amount"] == "20.25"

    portal = store.get_user_portal_data(user["id"])
    assert portal["wallet"]["balance"] == "-4.75"
    assert portal["wallet"]["available_balance"] == "7.75"
    assert portal["wallet"]["total_credit"] == "20.25"
    assert portal["wallet"]["total_debit"] == "25"
    assert portal["summary"]["strategy_count"] == 0
    assert portal["orders"]["total"] == 0
    assert portal["usage"]["calls"] == 0


def test_official_ai_usage_charges_balance_and_writes_ledger(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "ai-charge.db")
    store.initialize()
    user = store.save_user({"email": "charge@example.com", "status": "active"})

    usage = store.save_ai_usage_log({
        "user_id": str(user["id"]),
        "deployment_id": "dep_charge",
        "strategy_code": "PA_AGENT_V1",
        "endpoint": "open",
        "provider_id": "aie_charge",
        "model_id": "aie_charge",
        "input_tokens": 1000,
        "output_tokens": 500,
        "total_tokens": 1500,
        "official_tokens": 1500,
        "custom_tokens": 0,
        "billing_source": "official",
        "input_price_snapshot": "2",
        "output_price_snapshot": "8",
    })

    assert Decimal(usage["charged_amount"]) == Decimal("0.006")
    assert Decimal(usage["balance_after"]) == Decimal("-0.006")
    assert Decimal(store.get_user(user["id"])["ai_balance"]) == Decimal("-0.006")
    ledger = store.list_ai_balance_ledger(page=1, size=10, user_id=user["id"])
    assert ledger["total"] == 1
    assert ledger["list"][0]["entry_type"] == "ai_charge"
    assert Decimal(ledger["list"][0]["amount"]) == Decimal("-0.006")
    assert ledger["list"][0]["reference_id"] == usage["id"]

    custom_usage = store.save_ai_usage_log({
        "user_id": str(user["id"]),
        "deployment_id": "dep_charge",
        "strategy_code": "PA_AGENT_V1",
        "endpoint": "position",
        "provider_id": "custom_position",
        "model_id": "user-model",
        "input_tokens": 2000,
        "output_tokens": 1000,
        "total_tokens": 3000,
        "official_tokens": 0,
        "custom_tokens": 3000,
        "billing_source": "custom",
        "input_price_snapshot": "100",
        "output_price_snapshot": "100",
    })
    assert Decimal(custom_usage["charged_amount"]) == 0
    assert custom_usage["balance_after"] is None
    assert custom_usage["official_tokens"] == 0
    assert custom_usage["custom_tokens"] == 3000
    assert Decimal(store.get_user(user["id"])["ai_balance"]) == Decimal("-0.006")
    assert store.list_ai_balance_ledger(page=1, size=10, user_id=user["id"])["total"] == 1


def test_credit_exhaustion_blocks_official_but_not_custom_ai(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "credit-access.db")
    store.initialize()
    user = store.save_user({
        "email": "credit@example.com",
        "status": "active",
        "vip_level": 1,
        "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    store.adjust_ai_balance(
        user_id=user["id"],
        amount=Decimal("-10"),
        entry_type="admin_deduction",
    )
    key = "gl_credit_access_test"
    deployment = store.upsert_web_deployment(
        key,
        user_id=str(user["id"]),
        strategy_code="PA_AGENT_V1",
        strategy_name="Credit access test",
        status="active",
        symbol="*",
        timeframe="*",
        config={"deployment_key": key, "open_ai_mode": "official", "position_ai_mode": "official"},
    )
    service = DecisionService(store, {})
    assert service.deployment_access_error(deployment) == "insufficient_balance"

    custom_deployment = store.upsert_web_deployment(
        key,
        user_id=str(user["id"]),
        strategy_code="PA_AGENT_V1",
        strategy_name="Credit access test",
        status="active",
        symbol="*",
        timeframe="*",
        config={"deployment_key": key, "open_ai_mode": "custom", "position_ai_mode": "custom"},
    )
    assert service.deployment_access_error(custom_deployment) is None


def test_user_usage_filters_summary_pagination_and_deployment_key(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "usage-filters.db")
    store.initialize()
    user = store.save_user({"email": "usage-filter@example.com", "status": "active"})
    first = store.upsert_web_deployment(
        "gl_filter_first",
        user_id=str(user["id"]), strategy_code="PA_AGENT_V1", strategy_name="First",
        status="active", symbol="*", timeframe="*", config={"deployment_key": "gl_filter_first"},
    )
    second = store.upsert_web_deployment(
        "gl_filter_second",
        user_id=str(user["id"]), strategy_code="PA_AGENT_V1", strategy_name="Second",
        status="active", symbol="*", timeframe="*", config={"deployment_key": "gl_filter_second"},
    )
    for deployment, model, tokens in ((first, "model-a", 100), (second, "model-b", 200)):
        store.save_ai_usage_log({
            "user_id": str(user["id"]), "deployment_id": deployment["id"],
            "strategy_code": "PA_AGENT_V1", "endpoint": "open",
            "provider_id": model, "model_id": model,
            "input_tokens": tokens, "output_tokens": 10, "billing_source": "custom",
            "official_tokens": 0, "custom_tokens": tokens + 10,
        })

    all_rows = store.list_user_ai_usage(user_id=user["id"], page=1, size=1)
    assert all_rows["total"] == 2
    assert all_rows["pages"] == 2
    assert all_rows["summary"]["calls"] == 2
    assert all_rows["summary"]["input_tokens"] == 300
    assert all_rows["lifetime_summary"]["calls"] == 2
    assert all_rows["lifetime_summary"]["input_tokens"] == 300
    assert all_rows["retention_days"] == 60
    assert all_rows["monthly_bills"][0]["calls"] == 2
    assert {item["key"] for item in all_rows["filters"]["deployments"]} == {"gl_filter_first", "gl_filter_second"}

    filtered = store.list_user_ai_usage(
        user_id=user["id"], page=1, size=10,
        model_id="model-a", deployment_id=first["id"],
    )
    assert filtered["total"] == 1
    assert filtered["summary"]["input_tokens"] == 100
    assert filtered["list"][0]["deployment_key"] == "gl_filter_first"

    no_rows = store.list_user_ai_usage(user_id=user["id"], page=1, size=10, start_at="2999-01-01T00:00:00+00:00")
    assert no_rows["total"] == 0
    assert no_rows["summary"]["calls"] == 0

    with store._connect() as connection:
        connection.execute("UPDATE ai_usage_logs SET created_at = '2000-01-01T00:00:00+00:00'")
    assert store.cleanup_expired_ai_usage_details() == 2
    after_cleanup = store.list_user_ai_usage(user_id=user["id"], page=1, size=10)
    assert after_cleanup["total"] == 0
    assert after_cleanup["lifetime_summary"]["calls"] == 2
    assert after_cleanup["monthly_bills"][0]["input_tokens"] == 300


def test_user_order_filters_stats_account_and_curve(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "order-filters.db")
    store.initialize()
    user = store.save_user({"email": "order-filter@example.com", "status": "active"})
    first = store.upsert_web_deployment(
        "gl_order_first", user_id=str(user["id"]), strategy_code="PA_AGENT_V1",
        strategy_name="First", status="active", symbol="*", timeframe="*",
        config={"deployment_key": "gl_order_first"},
    )
    second = store.upsert_web_deployment(
        "gl_order_second", user_id=str(user["id"]), strategy_code="PA_AGENT_V1",
        strategy_name="Second", status="active", symbol="*", timeframe="*",
        config={"deployment_key": "gl_order_second"},
    )
    store.sync_mt5_history_deals(
        first["id"], account_login="10001", account_server="Demo",
        orders=[
            {"order_id": "1", "symbol": "XAUUSD", "mt_type": "sell", "volume": 0.1,
             "open_price": 2000, "close_price": 1990, "net_profit": 100, "close_time": 1700000000},
            {"order_id": "3", "symbol": "XAUUSD", "mt_type": "sell", "volume": 0.1,
             "open_price": 2000, "close_price": 1995, "net_profit": 25, "close_time": 1700003600},
        ],
    )
    store.sync_mt5_history_deals(
        second["id"], account_login="20002", account_server="Demo",
        orders=[{"order_id": "2", "symbol": "EURUSD", "mt_type": "buy", "volume": 0.2,
                 "open_price": 1.1, "close_price": 1.09, "net_profit": -50, "close_time": 1700086400}],
    )

    data = store.list_user_orders(user_id=user["id"], page=1, size=10)
    assert data["summary"] == {"total": 3, "wins": 2, "losses": 1, "win_rate": 66.67, "pnl": 75.0, "symbol_count": 2}
    assert data["list"][0]["account_login"] == "20002"
    assert len(data["curve"]) == 3
    assert data["curve"][0]["time"] == "2023-11-14 22:13:20"
    assert data["curve"][1]["time"] == "2023-11-14 23:13:20"
    assert data["curve"][-1]["pnl"] == 75.0

    filtered = store.list_user_orders(
        user_id=user["id"], page=1, size=10,
        deployment_id=first["id"], symbol="XAUUSD",
    )
    assert filtered["total"] == 2
    assert filtered["summary"]["pnl"] == 125.0
    assert filtered["list"][0]["deployment_key"] == "gl_order_first"


def test_user_can_pause_resume_and_soft_delete_own_strategy(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "strategy-actions.db")
    store.initialize()
    owner = store.save_user({"email": "strategy-owner@example.com", "status": "active"})
    other = store.save_user({"email": "strategy-other@example.com", "status": "active"})
    deployment = store.upsert_web_deployment(
        "gl_strategy_action", user_id=str(owner["id"]), strategy_code="PA_AGENT_V1",
        strategy_name="Action", status="active", symbol="*", timeframe="*",
        config={"deployment_key": "gl_strategy_action"},
    )

    paused = store.update_user_deployment_status(
        user_id=owner["id"], deployment_id=deployment["id"], status="paused",
    )
    assert paused["status"] == "paused"
    resumed = store.update_user_deployment_status(
        user_id=owner["id"], deployment_id=deployment["id"], status="active",
    )
    assert resumed["status"] == "active"
    with pytest.raises(RuntimeError, match="deployment_not_found"):
        store.delete_user_deployment(user_id=other["id"], deployment_id=deployment["id"])

    store.delete_user_deployment(user_id=owner["id"], deployment_id=deployment["id"])
    assert store.list_web_deployments(str(owner["id"])) == []
    assert store.get_user(owner["id"])["strategy_count"] == 0
    assert store.find_deployment_by_key("gl_strategy_action")["status"] == "deleted"


def test_user_strategy_ai_config_uses_selectable_models_and_preserves_custom_key(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "strategy-ai-config.db")
    store.initialize()
    official = next(item for item in store.list_public_official_ai_strategies()["list"] if item["code"] == "PA_AGENT_V1")
    assert official["default_config"]["fixed_lot"] == 0.01
    assert official["default_config"]["max_stop_amount"] == 100
    assert official["default_config"]["max_positions"] == 1
    user = store.save_user({"email": "strategy-ai@example.com", "status": "active"})
    endpoint = store.save_ai_endpoint({
        "id": "aie_user_selectable",
        "owner_type": "gl",
        "name": "通义千问 / qwen-plus",
        "base_url": "https://example.com/v1",
        "model": "qwen-plus",
        "api_key": "sk-platform",
        "enabled": True,
        "selectable_by_user": True,
        "input_price_per_million": "2.4",
        "output_price_per_million": "9.6",
    })
    model_option = store.list_public_ai_model_options()["list"][0]
    assert model_option["input_price_per_million"] == "2.4"
    assert model_option["output_price_per_million"] == "9.6"
    deployment = store.upsert_web_deployment(
        "gl_strategy_ai", user_id=str(user["id"]), strategy_code="PA_AGENT_V1",
        strategy_name="AI config", status="active", symbol="*", timeframe="*",
        config={"deployment_key": "gl_strategy_ai"},
    )

    updated = store.update_user_deployment_ai_config(
        user_id=user["id"], deployment_id=deployment["id"],
        payload={
            "open_ai_mode": "official", "open_ai_endpoint_id": endpoint["id"],
            "position_ai_mode": "custom", "position_ai_base_url": "https://custom.example/v1",
            "position_ai_model": "custom-model", "position_ai_key": "sk-custom",
        },
    )
    assert updated["config"]["open_ai_model"] == "qwen-plus"
    assert updated["config"]["position_ai_key"] == "sk-custom"

    preserved = store.update_user_deployment_ai_config(
        user_id=user["id"], deployment_id=deployment["id"],
        payload={
            "open_ai_mode": "official", "open_ai_endpoint_id": endpoint["id"],
            "position_ai_mode": "custom", "position_ai_base_url": "https://custom.example/v1",
            "position_ai_model": "custom-model-v2", "position_ai_key": "",
        },
    )
    assert preserved["config"]["position_ai_key"] == "sk-custom"
    portal = store.get_user_portal_data(user["id"])["strategies"][0]
    assert portal["open_ai_endpoint_name"] == "通义千问 / qwen-plus"
    assert portal["position_ai_key_configured"] is True
    assert "position_ai_key" not in portal

    settings = store.update_user_deployment_settings(
        user_id=user["id"], deployment_id=deployment["id"],
        payload={
            "name": "Updated strategy", "status": "paused", "mt_login": "60064845",
            "open_ai_mode": "official", "open_ai_endpoint_id": endpoint["id"],
            "position_ai_mode": "custom", "position_ai_base_url": "https://custom.example/v1",
            "position_ai_model": "custom-model-v2", "position_ai_key": "",
            "position_size_mode": "risk", "risk_base_mode": "balance_percent",
            "risk_percent": 1.5, "risk_amount": 100, "fixed_volume": 0.1,
            "max_positions": 5, "allow_add": True,
        },
    )
    assert settings["status"] == "paused"
    assert settings["mt_login"] == "60064845"
    assert settings["config"]["risk_base_mode"] == "balance_percent"
    assert settings["config"]["risk_percent"] == 1.5
    assert settings["config"]["max_positions"] == 5
    assert settings["config"]["allow_add"] is True
    assert store.get_user_portal_data(user["id"])["strategies"][0]["name"] == "Updated strategy"

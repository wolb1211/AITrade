from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.services.auth_service import AuthError, UserAuthService
from app.store import SqliteStore


class FakeEmailService:
    def __init__(self) -> None:
        self.codes: dict[tuple[str, str], str] = {}

    def send_verification_code(self, *, to: str, code: str, purpose: str, minutes: int) -> None:
        assert minutes == 10
        self.codes[(to, purpose)] = code


def create_service(tmp_path: Path) -> tuple[UserAuthService, FakeEmailService]:
    store = SqliteStore(tmp_path / "auth.db")
    store.initialize()
    email = FakeEmailService()
    settings = Settings(auth_secret="test-auth-secret", verification_minutes=10, session_days=30)
    return UserAuthService(store, settings, email), email


def test_register_password_and_code_login(tmp_path: Path) -> None:
    service, email = create_service(tmp_path)
    address = "user@example.com"

    prepared = service.register(email=address, password="Password123")
    assert prepared["user_id"] == 1
    registered = service.verify_registration(
        email=address,
        code=email.codes[(address, "register")],
    )
    assert registered["user"]["id"] == 1
    assert registered["user"]["status"] == "active"
    assert registered["user"]["vip_level"] == 0
    assert registered["user"]["max_strategy_keys"] == 1

    password_login = service.password_login(email=address, password="Password123")
    assert password_login["token"]
    assert service.me(password_login["token"])["email"] == address
    updated = service.update_profile(password_login["token"], nickname="老刘")
    assert updated["nickname"] == "老刘"
    assert service.me(password_login["token"])["nickname"] == "老刘"

    service.send_login_code(email=address)
    code_login = service.code_login(email=address, code=email.codes[(address, "login")])
    assert code_login["user"]["id"] == 1

    changed_password = service.change_password(
        password_login["token"],
        current_password="Password123",
        new_password="NewPassword456",
    )
    assert changed_password["password_configured"] is True
    assert len(service.list_sessions(password_login["token"])) == 1
    assert service.password_login(email=address, password="NewPassword456")["token"]

    new_address = "new-user@example.com"
    service.send_current_email_code(password_login["token"])
    service.send_change_email_code(password_login["token"], email=new_address)
    changed_email = service.verify_change_email(
        password_login["token"],
        email=new_address,
        current_email_code=email.codes[(address, "change_email_old")],
        new_email_code=email.codes[(new_address, "change_email_new")],
    )
    assert changed_email["email"] == new_address
    assert service.me(password_login["token"])["email"] == new_address

    service.logout_all(password_login["token"])
    try:
        service.me(password_login["token"])
        assert False, "all sessions should have been revoked"
    except Exception as exc:
        assert str(exc) == "invalid_session"


def test_user_key_limit_default_does_not_overwrite_existing_limit(tmp_path: Path) -> None:
    service, _ = create_service(tmp_path)
    user = service.store.save_user({
        "email": "existing-limit@example.com",
        "status": "active",
        "max_strategy_keys": 8,
    })

    updated = service.store.save_user({
        "id": user["id"],
        "email": user["email"],
        "status": user["status"],
        "nickname": "Existing user",
    })

    assert updated["max_strategy_keys"] == 8


def test_user_remark_is_admin_only(tmp_path: Path) -> None:
    service, email = create_service(tmp_path)
    address = "remark@example.com"
    service.register(email=address, password="Password123")
    session = service.verify_registration(
        email=address,
        code=email.codes[(address, "register")],
    )

    saved = service.store.save_user({
        **session["user"],
        "remark": "仅供后台查看的客户备注",
    })
    assert "remark" not in saved
    assert "remark" not in service.me(session["token"])

    admin_users = service.store.list_users(
        page=1,
        size=20,
        keyword="客户备注",
    )
    assert admin_users["total"] == 1
    assert admin_users["list"][0]["remark"] == "仅供后台查看的客户备注"

    # 其他保存逻辑不携带备注时，后台备注仍应保留。
    service.store.save_user({**saved, "nickname": "新昵称"})
    assert service.store.list_users(page=1, size=20)["list"][0]["remark"] == "仅供后台查看的客户备注"


def test_reset_password(tmp_path: Path) -> None:
    service, email = create_service(tmp_path)
    address = "reset@example.com"
    service.register(email=address, password="Password123")
    service.verify_registration(email=address, code=email.codes[(address, "register")])

    service.forgot_password(email=address)
    reset = service.reset_password(
        email=address,
        code=email.codes[(address, "reset_password")],
        password="NewPassword456",
    )
    assert reset["token"]
    assert service.password_login(email=address, password="NewPassword456")["token"]


def test_vip_without_expiry_is_inactive(tmp_path: Path) -> None:
    service, _ = create_service(tmp_path)
    user = service.store.save_user({
        "email": "vip@example.com",
        "status": "active",
        "vip_level": 1,
        "vip_expires_at": "",
    })
    assert user["vip_expired"] is True
    assert user["vip_active"] is False


def test_authenticated_vip_user_can_create_strategy(tmp_path: Path) -> None:
    service, email = create_service(tmp_path)
    address = "creator@example.com"
    prepared = service.register(email=address, password="Password123")
    registered = service.verify_registration(email=address, code=email.codes[(address, "register")])
    token = registered["token"]
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE users SET vip_level = 1, vip_expires_at = ? WHERE id = ?",
            (expires_at, prepared["user_id"]),
        )
    endpoint = service.store.save_ai_endpoint({
        "id": "aie_create_strategy",
        "owner_type": "gl",
        "name": "通义千问 / qwen-turbo",
        "base_url": "https://example.com/v1",
        "model": "qwen-turbo",
        "api_key": "sk-platform",
        "enabled": True,
        "selectable_by_user": True,
    })
    created = service.create_strategy(token, payload={
        "strategy_code": "PA_AGENT_V1",
        "name": "新策略",
        "status": "active",
        "open_ai_mode": "official",
        "open_ai_endpoint_id": endpoint["id"],
        "position_ai_mode": "official",
        "position_ai_endpoint_id": endpoint["id"],
        "position_size_mode": "risk",
        "risk_base_mode": "balance_percent",
        "risk_percent": 1.5,
        "fixed_volume": 0.01,
        "risk_amount": 100,
        "max_positions": 2,
        "allow_add": True,
    })
    assert created["deployment_key"].startswith("gl_")
    deployments = service.store.list_web_deployments(str(prepared["user_id"]))
    assert len(deployments) == 1
    assert deployments[0]["id"] == created["id"]
    assert service.store.get_user_portal_data(prepared["user_id"])["strategies"][0]["name"] == "新策略"
    assert deployments[0]["config"]["risk_base_mode"] == "balance_percent"


class FakeCustomStrategyCompiler:
    def __init__(self) -> None:
        self.compile_calls = 0

    def compile_custom_strategy(self, deployment: dict[str, Any]) -> dict[str, Any]:
        self.compile_calls += 1
        return {
            "summary": "EMA 均线交叉策略",
            "open_prompt_template": "严格判断 EMA10 上穿 EMA20。",
            "position_prompt_template": "没有触发风控条件时继续持有。",
            "open_indicators": [{"name": "ema", "source": "close", "params": {"length": 20}, "alias": "ema20"}],
            "position_indicators": [],
            "open_kline_count": 80,
            "position_kline_count": 60,
            "open_data_type": "kline",
            "position_data_type": "kline",
            "unsupported_indicators": [],
            "warnings": [],
            "prompt_version": 1,
            "compile_status": "generated",
        }

    def normalize_custom_strategy_compilation(self, content: Any, **_: Any) -> dict[str, Any]:
        if not isinstance(content, dict) or not content.get("open_prompt_template") or not content.get("position_prompt_template"):
            raise RuntimeError("invalid_custom_strategy_compilation")
        return dict(content)


def test_custom_strategy_is_previewed_before_key_is_created(tmp_path: Path) -> None:
    service, email = create_service(tmp_path)
    compiler = FakeCustomStrategyCompiler()
    service.ai_client = compiler
    prepared = service.register(email="custom@example.com", password="Password123")
    session = service.verify_registration(email="custom@example.com", code=email.codes[("custom@example.com", "register")])
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    with service.store._connect() as connection:
        connection.execute("UPDATE users SET vip_level = 1, vip_expires_at = ? WHERE id = ?", (expires_at, prepared["user_id"]))
    endpoint = service.store.save_ai_endpoint({
        "id": "aie_custom_preview", "owner_type": "gl", "name": "测试模型", "base_url": "https://example.com/v1",
        "model": "test-model", "api_key": "sk-platform", "enabled": True, "selectable_by_user": True,
    })
    payload = {
        "strategy_code": "CUSTOM_AI_V1", "name": "均线策略", "ea_description": "EA显示的均线策略说明", "open_logic": "EMA10 上穿 EMA20 时开多",
        "position_logic": "出现反向交叉时平仓，否则继续持有", "open_data_type": "kline", "open_kline_count": 50,
        "position_data_type": "kline", "position_kline_count": 50, "open_ai_mode": "official",
        "open_ai_endpoint_id": endpoint["id"], "position_ai_mode": "official", "position_ai_endpoint_id": endpoint["id"],
        "position_size_mode": "fixed", "fixed_volume": 0.01, "risk_amount": 100, "risk_percent": 1,
        "max_positions": 1,
    }
    preview = service.preview_custom_strategy(session["token"], payload=payload)
    assert compiler.compile_calls == 1
    assert service.store.list_web_deployments(str(prepared["user_id"])) == []
    assert preview["open_kline_count"] == 80
    created = service.create_strategy(session["token"], payload={**payload, "compiled_config": preview})
    assert compiler.compile_calls == 1
    assert created["deployment_key"].startswith("gl_")
    saved = service.store.list_web_deployments(str(prepared["user_id"]))[0]
    assert saved["config"]["open_prompt_template"] == preview["open_prompt_template"]
    assert saved["config"]["ea_description"] == "EA显示的均线策略说明"
    assert service.store.get_user_portal_data(prepared["user_id"])["strategies"][0]["ea_description"] == "EA显示的均线策略说明"


def test_agent_invite_registration_and_dashboard(tmp_path: Path) -> None:
    service, email = create_service(tmp_path)
    agent_email = "agent@example.com"
    service.register(email=agent_email, password="Password123")
    agent_session = service.verify_registration(email=agent_email, code=email.codes[(agent_email, "register")])
    agent = service.store.save_user({**agent_session["user"], "agent_level": 2})
    assert agent["invite_code"].startswith("GL")

    invited_email = "invited@example.com"
    service.register(email=invited_email, password="Password123", invite_code=agent["invite_code"].lower())
    invited_session = service.verify_registration(email=invited_email, code=email.codes[(invited_email, "register")])
    assert invited_session["user"]["referrer_user_id"] == agent["id"]

    dashboard = service.agent_dashboard(agent_session["token"], page=1, size=20)
    assert dashboard["agent_level"] == 2
    assert dashboard["summary"]["total_users"] == 1
    assert dashboard["list"][0]["email"] == "in***@example.com"

    service.store.save_user({**agent, "agent_level": 0})
    assert service.store.get_user(invited_session["user"]["id"])["referrer_user_id"] == agent["id"]
    with pytest.raises(AuthError) as exc_info:
        service.register(email="rejected@example.com", password="Password123", invite_code=agent["invite_code"])
    assert exc_info.value.code == "invalid_invite_code"

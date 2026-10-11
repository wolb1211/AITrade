"""The agent centre: a referred user's keys, and one key's statistics.

An agent may watch the keys of the users they referred and nothing else, so the
read path checks the referral link rather than trusting the caller.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import _bucket_timestamp


def _app(tmp_path: Path, name: str):
    return create_app(Settings(
        environment="development",
        database_path=tmp_path / name,
        auth_secret="test-auth-secret",
    ))


def _agent(store, *, level: int = 1) -> int:
    user = store.save_user({
        "email": f"agent{level}@example.com",
        "status": "active",
        "agent_level": level,
        "vip_level": 1,
        "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    return int(user["id"])


def _referred(store, agent_id: int, email: str = "referred@example.com") -> int:
    user = store.save_user({
        "email": email,
        "status": "active",
        "vip_level": 1,
        "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    # save_user does not carry the referral link; the registration flow sets it.
    with store._connect() as connection:
        connection.execute(
            "UPDATE users SET referrer_user_id = ?, referred_at = ? WHERE id = ?",
            (agent_id, datetime.now(timezone.utc).isoformat(), user["id"]),
        )
    return int(user["id"])


def _deploy(store, key: str, user_id: int, code: str = "GL_ARBITRAGE_V1") -> dict:
    store.upsert_web_deployment(
        key,
        user_id=str(user_id),
        strategy_code=code,
        strategy_name=f"{code} 测试",
        status="active",
        symbol="XAUUSD",
        timeframe="M15",
        config={"deployment_key": key, "fixed_lot": 0.01},
    )
    deployment = store.find_deployment_by_key(key)
    store.bind_deployment_login(key, login="1015141")
    return deployment


def _order_bucket(store, deployment: dict, *, days_ago: int, pnl: float, orders: int = 3) -> None:
    stamp = int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())
    bucket = _bucket_timestamp(stamp, "day")
    with store._connect() as connection:
        connection.execute(
            """
            INSERT INTO mt_order_time_summaries (
                id, user_id, deployment_id, account_login, account_server, symbol,
                bucket_type, bucket_start, order_count, win_count, loss_count,
                total_volume, gross_profit, gross_loss, commission, swap, net_profit,
                created_at, updated_at
            ) VALUES (?, ?, ?, '1015141', '', 'XAUUSD', 'day', ?, ?, ?, ?, 0, ?, 0, 0, 0, ?, ?, ?)
            """,
            (
                f"osum_{bucket}_{deployment['id']}",
                str(deployment["user_id"]),
                deployment["id"],
                bucket,
                orders,
                orders if pnl > 0 else 0,
                0 if pnl > 0 else orders,
                pnl if pnl > 0 else 0,
                pnl,
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def test_agent_dashboard_lists_each_referred_users_keys(tmp_path: Path) -> None:
    app = _app(tmp_path, "agent-keys.db")
    with TestClient(app) as client:
        store = app.state.store
        agent_id = _agent(store)
        referred_id = _referred(store, agent_id)
        outsider_id = _referred(store, _agent(store, level=2), "outsider@example.com")

        own = _deploy(store, "gl_agent_own_key", referred_id)
        _deploy(store, "gl_agent_outsider_key", outsider_id)
        store.save_decision(own["id"], "open", "req-agent-0001", {"decision_id": "dec_agent", "action": "BUY"})
        _order_bucket(store, own, days_ago=1, pnl=120.5)

        dashboard = store.get_agent_dashboard(agent_id)
        assert dashboard["summary"]["total_users"] == 1
        listed = dashboard["list"][0]
        assert listed["id"] == referred_id
        keys = listed["deployments"]
        assert len(keys) == 1
        assert keys[0]["deployment_key"] == "gl_agent_own_key"
        assert keys[0]["mt_login"] == "1015141"
        assert keys[0]["analysis_count"] == 1
        assert keys[0]["order_count"] == 1
        assert keys[0]["pnl"] == 120.5


def test_agent_key_detail_returns_summary_and_curve(tmp_path: Path) -> None:
    app = _app(tmp_path, "agent-detail.db")
    with TestClient(app) as client:
        store = app.state.store
        agent_id = _agent(store)
        referred_id = _referred(store, agent_id)
        deployment = _deploy(store, "gl_agent_detail_key", referred_id)
        store.save_decision(deployment["id"], "open", "req-detail-0001", {"decision_id": "dec_d", "action": "BUY"})
        _order_bucket(store, deployment, days_ago=1, pnl=100.0)
        _order_bucket(store, deployment, days_ago=2, pnl=-30.0)

        detail = store.agent_deployment_detail(
            agent_user_id=agent_id, deployment_id=deployment["id"]
        )

        assert detail["deployment"]["id"] == deployment["id"]
        assert detail["deployment"]["mt_login"] == "1015141"
        assert detail["owner"]["id"] == referred_id
        summary = detail["summary"]
        assert summary["analysis_count"] == 1
        assert summary["closed_count"] == 6
        assert summary["win_count"] == 3
        assert summary["pnl"] == 70.0
        # The curve is cumulative and ordered oldest first.
        assert [point["pnl"] for point in detail["curve"]] == [-30.0, 70.0]
        assert [point["change"] for point in detail["curve"]] == [-30.0, 100.0]


def test_agent_cannot_read_a_key_outside_their_referrals(tmp_path: Path) -> None:
    app = _app(tmp_path, "agent-guard.db")
    with TestClient(app) as client:
        store = app.state.store
        agent_id = _agent(store)
        other_agent_id = _agent(store, level=2)
        outsider_id = _referred(store, other_agent_id, "outsider@example.com")
        deployment = _deploy(store, "gl_agent_guard_key", outsider_id)

        try:
            store.agent_deployment_detail(
                agent_user_id=agent_id, deployment_id=deployment["id"]
            )
        except RuntimeError as exc:
            assert str(exc) == "not_your_referral"
        else:
            raise AssertionError("an agent must not read another agent's referral")

        # The owner's own agent still can.
        assert store.agent_deployment_detail(
            agent_user_id=other_agent_id, deployment_id=deployment["id"]
        )["deployment"]["id"] == deployment["id"]


def test_non_agent_cannot_open_the_agent_centre(tmp_path: Path) -> None:
    app = _app(tmp_path, "agent-required.db")
    with TestClient(app) as client:
        store = app.state.store
        user = store.save_user({"email": "plain@example.com", "status": "active"})
        try:
            store.get_agent_dashboard(int(user["id"]))
        except RuntimeError as exc:
            assert str(exc) == "agent_required"
        else:
            raise AssertionError("a user without an agent level must be refused")

"""An unfilled order must not be managed as if it were an open position.

A client listed pending orders beside open positions. The strategy then counted
an unfilled order against max_positions, took its price as the furthest entry for
the basket stop, approved an add-on because of it, and modified the pending
order's stop as though it were a position. MT5 numbers order types 0-5, which is
enough to tell them apart without any change on the client side.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.router import _is_pending_mt_type
from app.config import Settings
from app.main import create_app


def test_mt5_order_types_separate_positions_from_pending_orders() -> None:
    for mt_type in (0, 1, "0", "1"):
        assert _is_pending_mt_type(mt_type) is False
    for mt_type in (2, 3, 4, 5, "2", "3", "4", "5"):
        assert _is_pending_mt_type(mt_type) is True
    # Anything unrecognised counts as a position, so nothing real is dropped.
    assert _is_pending_mt_type("buy") is False
    assert _is_pending_mt_type("") is False
    assert _is_pending_mt_type(None) is False


def test_a_pending_order_gets_no_position_action(tmp_path: Path) -> None:
    settings = Settings(
        environment="production",
        database_path=tmp_path / "pending-filter.db",
        auth_secret="test-auth-secret",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        store = app.state.store
        key = "gl_test_pending_filter"
        store.save_user({
            "id": 17,
            "email": "pending@example.com",
            "status": "active",
            "vip_level": 1,
            "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        })
        store.upsert_web_deployment(
            key,
            user_id="17",
            strategy_code="GL_TREND_V1",
            strategy_name="Pending filter test",
            status="active",
            symbol="*",
            timeframe="*",
            config={"deployment_key": key, "allow_add": True, "max_positions": 4},
        )

        response = client.post(
            "/mt5/strategy/position-decision",
            json={
                "deployment_key": key,
                "request_id": "pending-filter-position-001",
                "account": {"platform": "MT5", "login": "12345678", "server": "Broker-Demo"},
                "symbol": "XAUUSD",
                "timeframe": "M15",
                "market": {"bid": 2500.0, "ask": 2500.2, "spread": 2, "bars": []},
                "positions": [{
                    "ticket": "5001",
                    "symbol": "XAUUSD",
                    "mt_type": 2,  # a buy limit order, not a position
                    "volume": 0.1,
                    "open_price": 2499.0,
                    "current_price": 2500.0,
                    "sl": 2490.0,
                    "tp": 0.0,
                    "profit": 0.0,
                    "open_time": 0,
                    "comment": "",
                }],
            },
        )

        assert response.status_code == 200
        # Nothing is managed because nothing is open: no close, no modify and no
        # add-on against a slot the unfilled order does not hold.
        assert response.json()["actions_count"] == 0

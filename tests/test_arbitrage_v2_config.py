"""GL_ARBITRAGE_V2 ships the same EA contract with its own parameter set.

V2 drops maStart / maPeriod and adds the double moving average plus the reversal
and scaled add-on inputs. The init response must carry exactly the V2 keys: a V1
key leaking into a V2 config would mean the admin console and the EA disagree
about what the strategy reads.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import (
    ARBITRAGE_EA_CONFIG_DEFAULTS,
    ARBITRAGE_V2_EA_CONFIG_DEFAULTS,
    arbitrage_ea_config_defaults,
    backfill_arbitrage_config,
)

V2_KEYS = tuple(ARBITRAGE_V2_EA_CONFIG_DEFAULTS)


def _app(tmp_path: Path, name: str):
    return create_app(Settings(
        environment="production",
        database_path=tmp_path / name,
        auth_secret="test-auth-secret",
    ))


def _deploy(store, key: str, code: str, config: dict) -> None:
    store.upsert_web_deployment(
        key,
        user_id="1",
        strategy_code=code,
        strategy_name=code,
        status="active",
        symbol="XAUUSD",
        timeframe="M5",
        config=config,
    )


def _init(client: TestClient, key: str) -> dict:
    response = client.post(
        "/mt5/strategy/init",
        json={"deployment_key": key, "account": {"platform": "MT5", "login": "60064845"}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_v2_defaults_drop_the_v1_only_inputs() -> None:
    assert "maStart" not in ARBITRAGE_V2_EA_CONFIG_DEFAULTS
    assert "maPeriod" not in ARBITRAGE_V2_EA_CONFIG_DEFAULTS
    # Everything V1 has and V2 keeps is still there.
    for key in ("fixed_lot", "addMax", "addNum", "carryStart", "carryWin", "maxLoss", "step", "stopWin", "stopMove"):
        assert key in ARBITRAGE_V2_EA_CONFIG_DEFAULTS
    # The new inputs and their documented defaults.
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["orderLoss"] == 0.0328
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["addOrderTotal"] == 6
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["addOrderMultiple"] == 2.0
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["addOrderNum"] == 0.3
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["reversalDiff"] == 3.0
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["reversalAtr"] == 1.6
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["carryLose"] == 0.1
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["maPeriod1"] == 20
    assert ARBITRAGE_V2_EA_CONFIG_DEFAULTS["maPeriod2"] == 60


def test_v2_strategy_is_seeded(tmp_path: Path) -> None:
    app = _app(tmp_path, "arb-v2-seed.db")
    with TestClient(app) as client:
        store = app.state.store
        official = store.get_official_ai_strategy("GL_ARBITRAGE_V2")

        assert official is not None
        assert official["name"] == "GL对冲套利策略 V2"
        assert official["status"] == "active"
        stored = official["default_config"]
        assert set(V2_KEYS) <= set(stored)
        assert "maPeriod" not in stored and "maStart" not in stored
        assert stored["maPeriod1"] == 20


def test_v2_init_sends_the_v2_inputs_only(tmp_path: Path) -> None:
    app = _app(tmp_path, "arb-v2-init.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_arb_v2_key", "GL_ARBITRAGE_V2", {"fixed_lot": 0.02})

        payload = _init(client, "gl_arb_v2_key")
        config = payload["config"]

        assert set(V2_KEYS) <= set(config)
        assert "maPeriod" not in config
        assert "maStart" not in config
        # The user's own lot still wins over the shared default.
        assert config["fixed_lot"] == 0.02
        assert config["addOrderMultiple"] == 2.0
        # The regime contract applies to V2 exactly as it does to V1.
        assert [item["timeframe"] for item in payload["strategy"]["secondary_timeframes"]] == ["M15", "H4", "D1"]


def test_v1_config_does_not_gain_v2_keys() -> None:
    merged = backfill_arbitrage_config({"fixed_lot": 0.03}, "GL_ARBITRAGE_V1")

    assert "maPeriod1" not in merged
    assert "orderLoss" not in merged
    assert "maPeriod" in merged
    assert merged["fixed_lot"] == 0.03
    assert set(ARBITRAGE_EA_CONFIG_DEFAULTS) <= set(merged)


def test_v2_config_drops_stray_v1_keys() -> None:
    merged = backfill_arbitrage_config(
        {"fixed_lot": 0.03, "maPeriod": 10, "maStart": 5},
        "GL_ARBITRAGE_V2",
    )

    assert "maPeriod" not in merged
    assert "maStart" not in merged
    assert merged["maPeriod1"] == 20
    assert set(arbitrage_ea_config_defaults("GL_ARBITRAGE_V2")) <= set(merged)

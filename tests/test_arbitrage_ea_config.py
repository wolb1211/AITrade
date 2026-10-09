"""GL_ARBITRAGE_V1 keeps its trading parameters on the official strategy row.

Only the first lot is a user choice; addMax/carryWin/... are managed once in the
admin console and must reach every deployed EA on its next init. These tests pin
that contract, and pin that strategies outside the set are unaffected.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import ARBITRAGE_EA_CONFIG_DEFAULTS


EA_KEYS = tuple(ARBITRAGE_EA_CONFIG_DEFAULTS)


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


def test_init_returns_every_arbitrage_ea_input(tmp_path: Path) -> None:
    app = _app(tmp_path, "arb-init.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_arb_init_defaults", "GL_ARBITRAGE_V1", {"fixed_lot": 0.02})

        config = _init(client, "gl_arb_init_defaults")["config"]

        assert set(EA_KEYS) <= set(config)
        # The user's lot survives the merge with the admin defaults.
        assert config["fixed_lot"] == 0.02
        assert config["addMax"] == 30
        assert config["carryStart"] == 10
        assert config["carryWin"] == 500.0
        assert config["maxLoss"] == 0
        assert config["step"] == 0.0004
        assert config["stopWin"] == 0.0006
        assert config["stopMove"] == 0.0003
        assert config["maStart"] == 0
        assert config["maPeriod"] == 10


def test_legacy_lot_alias_still_wins_over_the_admin_default(tmp_path: Path) -> None:
    # Deployments created before fixed_lot existed store the lot as "lot"; the
    # admin default must fill gaps, not override that.
    app = _app(tmp_path, "arb-lot-alias.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_arb_legacy_lot", "GL_ARBITRAGE_V1", {"lot": 0.05})

        config = _init(client, "gl_arb_legacy_lot")["config"]

        assert config["fixed_lot"] == 0.05
        assert config["addMax"] == 30


def test_one_admin_edit_reaches_every_deployed_ea(tmp_path: Path) -> None:
    app = _app(tmp_path, "arb-shared.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_arb_shared_a", "GL_ARBITRAGE_V1", {"fixed_lot": 0.01})
        _deploy(store, "gl_arb_shared_b", "GL_ARBITRAGE_V1", {"fixed_lot": 0.03})

        assert _init(client, "gl_arb_shared_a")["config"]["addMax"] == 30
        assert _init(client, "gl_arb_shared_b")["config"]["addMax"] == 30

        official = store.get_official_ai_strategy("GL_ARBITRAGE_V1")
        store.save_official_ai_strategy({
            **official,
            "default_config": {**official["default_config"], "addMax": 12, "carryStart": 0},
        })

        first = _init(client, "gl_arb_shared_a")["config"]
        second = _init(client, "gl_arb_shared_b")["config"]

        assert first["addMax"] == 12
        assert second["addMax"] == 12
        assert first["carryStart"] == 0
        assert second["carryStart"] == 0
        # A shared setting never rewrites a user's own lot.
        assert first["fixed_lot"] == 0.01
        assert second["fixed_lot"] == 0.03


def test_init_sends_only_the_keys_the_arbitrage_ea_reads(tmp_path: Path) -> None:
    # The deployment row also stores the client form and the AI endpoint picks.
    # Shipping those to the EA would make a server-side rename look like an EA
    # setting, so the payload is a whitelist.
    app = _app(tmp_path, "arb-whitelist.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_arb_whitelist", "GL_ARBITRAGE_V1", {
            "fixed_lot": 0.01,
            "open_data_type": "kline",
            "open_kline_count": 200,
            "position_kline_count": 200,
            "call_mode": "bar",
            "call_val": 1,
            "open_ai_mode": "official",
            "open_ai_endpoint_id": "aie_somewhere",
            "open_ai_model": "qwen-turbo",
            "open_ai_base_url": "",
            "position_ai_model": "qwen-plus",
            "position_size_mode": "fixed",
            "fixed_volume": 0.01,
            "lot": 0.01,
            "risk_base_mode": "fixed_loss",
            "risk_amount": 100.0,
            "max_positions": 1,
            "allow_add": False,
            "ai_user_configured": True,
            "strategy_type": "official",
        })

        config = _init(client, "gl_arb_whitelist")["config"]

        assert set(config) == set(EA_KEYS)
        # The client's own lot key still wins over the admin default.
        assert config["fixed_lot"] == 0.01
        for unwanted in (
            "open_ai_model", "open_ai_endpoint_id", "open_data_type", "open_kline_count",
            "position_data_type", "position_kline_count", "call_mode", "call_val",
            "position_ai_model", "position_size_mode", "fixed_volume", "lot",
            "risk_base_mode", "risk_amount", "risk_percent", "max_positions",
            "allow_add", "ai_user_configured", "strategy_type", "position_sizing_mode",
        ):
            assert unwanted not in config, unwanted


def test_other_strategies_keep_their_full_config_payload(tmp_path: Path) -> None:
    # The whitelist is per strategy: an older EA must still receive every key it
    # reads today, or it would silently fall back to its hardcoded defaults.
    app = _app(tmp_path, "arb-other-full.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_trend_full", "GL_TREND_V1", {
            "fixed_lot": 0.01,
            "open_data_type": "kline",
            "open_kline_count": 200,
            "call_mode": "bar",
            "max_positions": 4,
            "allow_add": True,
        })

        config = _init(client, "gl_trend_full")["config"]

        assert config["open_data_type"] == "kline"
        assert config["open_kline_count"] == 200
        assert config["call_mode"] == "bar"
        assert config["max_positions"] == 4
        assert config["allow_add"] is True


def test_other_strategies_do_not_receive_arbitrage_settings(tmp_path: Path) -> None:
    app = _app(tmp_path, "arb-other.db")
    with TestClient(app) as client:
        store = app.state.store
        _deploy(store, "gl_trend_untouched", "GL_TREND_V1", {"fixed_lot": 0.01})
        _deploy(store, "gl_arb_only", "GL_ARBITRAGE_V1", {"fixed_lot": 0.01})

        trend_config = _init(client, "gl_trend_untouched")["config"]
        arb_config = _init(client, "gl_arb_only")["config"]

        # fixed_lot is not arbitrage specific (the trend form writes it too), so
        # the check is about the settings only the arbitrage EA understands.
        arbitrage_only = set(EA_KEYS) - {"fixed_lot"}
        assert not (arbitrage_only & set(trend_config))
        assert arbitrage_only <= set(arb_config)


def test_existing_strategy_row_is_backfilled_without_losing_admin_values(tmp_path: Path) -> None:
    app = _app(tmp_path, "arb-backfill.db")
    with TestClient(app) as client:
        store = app.state.store
        official = store.get_official_ai_strategy("GL_ARBITRAGE_V1")

        # Simulate a row written before the EA settings existed.
        store.save_official_ai_strategy({
            **official,
            "default_config": {"regime_periods": ["H4", "D1"], "addMax": 7},
        })
        store._ensure_official_strategy_config_defaults()

        migrated = store.get_official_ai_strategy("GL_ARBITRAGE_V1")["default_config"]
        assert set(EA_KEYS) <= set(migrated)
        # The tuned value is kept, the missing ones are filled from the defaults.
        assert migrated["addMax"] == 7
        assert migrated["carryWin"] == 500.0
        assert migrated["maPeriod"] == 10

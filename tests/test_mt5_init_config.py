"""The init response carries the deployment settings, minus anything secret."""

from __future__ import annotations

from app.api.router import _mt5_init_config


def test_the_lot_is_normalised_to_fixed_lot() -> None:
    """The form stores fixed_volume, the strategy default says fixed_lot."""
    assert _mt5_init_config({"fixed_volume": 0.01})["fixed_lot"] == 0.01
    assert _mt5_init_config({"lot": "0.05"})["fixed_lot"] == 0.05
    # An explicit fixed_lot wins over the aliases.
    assert _mt5_init_config({"fixed_lot": 0.2, "fixed_volume": 0.1})["fixed_lot"] == 0.2


def test_settings_the_strategy_reads_are_passed_through() -> None:
    settings = _mt5_init_config({
        "max_positions": 4,
        "allow_add": True,
        "regime_periods": ["H4", "D1"],
        "regime_cache_seconds": 900,
    })

    assert settings["max_positions"] == 4
    assert settings["allow_add"] is True
    assert settings["regime_periods"] == ["H4", "D1"]


def test_credentials_are_never_returned() -> None:
    """The EA needs the strategy settings, not the user's provider key."""
    settings = _mt5_init_config({
        "fixed_volume": 0.01,
        "open_ai_key": "sk-secret",
        "position_ai_key": "sk-secret",
        "provider_api_key": "sk-secret",
        "auth_token": "t",
    })

    assert settings["fixed_lot"] == 0.01
    assert not [name for name in settings if "key" in name or "token" in name]
    assert "sk-secret" not in str(settings)

"""The admin strategy statistics can be narrowed to one user, strategy or key."""

from __future__ import annotations

from pathlib import Path

from app.store import SqliteStore


def _store(tmp_path: Path) -> SqliteStore:
    store = SqliteStore(tmp_path / "admin-stats.db")
    store.initialize()
    store.upsert_web_deployment(
        "gl_alpha_key_1234",
        user_id="1",
        strategy_code="GL_TREND_V1",
        strategy_name="GL趋势跟踪策略",
        status="active",
        symbol="XAUUSD",
        timeframe="M15",
        config={"deployment_key": "gl_alpha_key_1234"},
    )
    store.upsert_web_deployment(
        "pa_beta_key_5678",
        user_id="2",
        strategy_code="PA_AGENT_V1",
        strategy_name="GL趋势自动分析策略",
        status="active",
        symbol="XAUUSD",
        timeframe="M15",
        config={"deployment_key": "pa_beta_key_5678"},
    )
    return store


def _codes(overview: dict) -> set[str]:
    return {item["strategy_code"] for item in overview["strategies"]}


def test_no_filter_returns_every_strategy(tmp_path: Path) -> None:
    overview = _store(tmp_path).admin_ai_strategy_overview()

    assert _codes(overview) == {"GL_TREND_V1", "PA_AGENT_V1"}


def test_the_rows_carry_the_displayable_key_and_user(tmp_path: Path) -> None:
    rows = _store(tmp_path).admin_ai_strategy_overview()["strategies"]

    assert {row["deployment_key"] for row in rows} == {
        "gl_alpha_key_1234", "pa_beta_key_5678",
    }
    # The username column is always present, even when the user is unknown.
    assert all("username" in row for row in rows)


def test_the_filter_by_user_narrows_the_table_and_the_totals(tmp_path: Path) -> None:
    store = _store(tmp_path)

    overview = store.admin_ai_strategy_overview({"user_id": "1"})

    assert _codes(overview) == {"GL_TREND_V1"}
    assert overview["summary"]["strategy_count"] == 1
    assert overview["summary"]["strategy_user_count"] == 1


def test_the_filter_by_strategy_code_and_key(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert _codes(store.admin_ai_strategy_overview({"strategy_code": "pa_agent"})) == {"PA_AGENT_V1"}
    assert _codes(store.admin_ai_strategy_overview({"deployment_key": "alpha"})) == {"GL_TREND_V1"}
    # An empty filter object still means "everything".
    assert _codes(store.admin_ai_strategy_overview({})) == {"GL_TREND_V1", "PA_AGENT_V1"}

"""Admin statistics window, and the admin owned run switch on a strategy key.

Two separate complaints are pinned here. The statistics page used to load every
decision row with its whole JSON and count one word in Python, so it grew with
history until the admin's ten second timeout hit; the counts now come from SQL
inside a time window. And an admin needs to be able to stop one key without
deleting it and without the user being able to switch it back on - while a new
bound account starts clean.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import _bucket_timestamp, admin_period_range


def _app(tmp_path: Path, name: str):
    # Development, so the admin routes need no JWT and the test stays about the
    # feature rather than about auth.
    return create_app(Settings(
        environment="development",
        database_path=tmp_path / name,
        auth_secret="test-auth-secret",
    ))


def _vip_user(store) -> str:
    user = store.save_user({
        "email": "stats@example.com",
        "status": "active",
        "vip_level": 1,
        "vip_expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    return str(user["id"])


def _deploy(store, key: str, user_id: str, code: str = "GL_ARBITRAGE_V1") -> dict:
    store.upsert_web_deployment(
        key,
        user_id=user_id,
        strategy_code=code,
        strategy_name=code,
        status="active",
        symbol="XAUUSD",
        timeframe="M15",
        config={"fixed_lot": 0.01},
    )
    return store.find_deployment_by_key(key)


def _bars(count: int = 100) -> list[dict]:
    return [
        {
            "time": 1_700_000_000 + index * 900,
            "open": 2000.0 + index,
            "high": 2001.0 + index,
            "low": 1999.0 + index,
            "close": 2000.5 + index,
            "volume": 10.0,
        }
        for index in range(count)
    ]


# ── the statistics window ─────────────────────────────────────────────────────


def test_period_range_is_built_on_the_local_calendar() -> None:
    start, end = admin_period_range("today")
    parsed = datetime.fromisoformat(start)
    # Midnight in UTC+8 is four in the afternoon UTC the day before.
    assert (parsed.hour, parsed.minute) == (16, 0)
    assert parsed.utcoffset() == timedelta(0)
    assert end == ""

    yesterday_start, yesterday_end = admin_period_range("yesterday")
    assert datetime.fromisoformat(yesterday_end) == parsed
    assert datetime.fromisoformat(yesterday_start) == parsed - timedelta(days=1)

    # An unknown value must not silently mean "all of history".
    assert admin_period_range("nonsense") == admin_period_range("today")
    assert admin_period_range("all") == ("", "")


def test_custom_range_covers_the_whole_end_date() -> None:
    start, end = admin_period_range("custom", "2026-10-01", "2026-10-05")
    assert datetime.fromisoformat(start) == datetime(2026, 10, 1, tzinfo=timezone(timedelta(hours=8)))
    # end is exclusive, so the fifth is included.
    assert datetime.fromisoformat(end) == datetime(2026, 10, 6, tzinfo=timezone(timedelta(hours=8)))


def test_overview_counts_signals_and_orders(tmp_path: Path) -> None:
    app = _app(tmp_path, "stats-count.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_stats_count", _vip_user(store))

        store.save_decision(deployment["id"], "open", "req-buy-0001", {"decision_id": "dec_buy_0001", "action": "BUY"})
        store.save_decision(deployment["id"], "open", "req-sell-0002", {"decision_id": "dec_sell_0002", "action": "SELL"})
        store.save_decision(deployment["id"], "position", "req-hold-0003", {"decision_id": "dec_hold_0003", "action": "HOLD"})
        store.save_decision(deployment["id"], "position", "req-add-0004", {"decision_id": "dec_add_0004", "action": "ADD"})

        overview = store.admin_ai_strategy_overview({})
        summary = overview["summary"]

        assert summary["analysis_count"] == 4
        # Signals are every action that is not hold: BUY, SELL and ADD.
        assert summary["signal_count"] == 3
        # Orders are openings only.
        assert summary["order_count"] == 2
        assert overview["strategies"][0]["analysis_count"] == 4
        assert overview["strategies"][0]["run_enabled"] is True


def test_the_action_backfill_lifts_history_out_of_the_payload(tmp_path: Path) -> None:
    # Decisions written before the column existed carry an empty action, and the
    # column is what a rebuilt day summary counts. The backfill reads the word out
    # of the stored payload.
    app = _app(tmp_path, "stats-backfill.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_stats_backfill", _vip_user(store))
        store.save_decision(
            deployment["id"], "open", "req-old-0001",
            {"decision_id": "dec_old_0001", "action": "BUY"},
        )
        with store._connect() as connection:
            connection.execute("UPDATE decisions SET action = '' WHERE request_id = ?", ("req-old-0001",))
            connection.execute(
                "DELETE FROM system_settings WHERE setting_key = ?",
                ("decisions_action_backfilled",),
            )

        store._backfill_decision_actions()

        with store._connect() as connection:
            row = connection.execute(
                "SELECT action FROM decisions WHERE request_id = ?", ("req-old-0001",)
            ).fetchone()
        assert row["action"] == "BUY"


def test_the_day_summary_backfill_rebuilds_the_counters(tmp_path: Path) -> None:
    # The statistics page reads the daily summary, so history that predates the
    # table has to be summarised once - and the attempt has to be remembered, or it
    # would be redone on every start.
    app = _app(tmp_path, "stats-summary-backfill.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_stats_summary", _vip_user(store))
        store.save_decision(deployment["id"], "open", "req-one-0001", {"decision_id": "dec_one", "action": "BUY"})
        store.save_decision(deployment["id"], "position", "req-two-0002", {"decision_id": "dec_two", "action": "HOLD"})

        with store._connect() as connection:
            connection.execute("DELETE FROM mt_decision_time_summaries")
            connection.execute(
                "DELETE FROM system_settings WHERE setting_key = ?",
                ("decision_time_summaries_backfilled",),
            )
        assert store.admin_ai_strategy_overview({})["summary"]["analysis_count"] == 0

        store._backfill_decision_summaries()

        summary = store.admin_ai_strategy_overview({})["summary"]
        assert summary["analysis_count"] == 2
        assert summary["signal_count"] == 1
        assert summary["order_count"] == 1

        # The marker makes it a one time cost.
        with store._connect() as connection:
            connection.execute("DELETE FROM mt_decision_time_summaries")
        store._backfill_decision_summaries()
        assert store.admin_ai_strategy_overview({})["summary"]["analysis_count"] == 0


def test_run_detail_counts_agree_with_the_summary_after_detail_is_trimmed(tmp_path: Path) -> None:
    # The per key run dialog and the statistics page must show the same numbers.
    # The dialog used to count decision rows directly, so once the detail was
    # trimmed it disagreed with the page that reads the day summary.
    app = _app(tmp_path, "run-detail-counts.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_run_detail_counts", _vip_user(store))
        store.save_decision(deployment["id"], "open", "req-open-0001", {"decision_id": "dec_open", "action": "BUY"})
        store.save_decision(deployment["id"], "position", "req-hold-0002", {"decision_id": "dec_hold", "action": "HOLD"})

        # The retention cleanup removes raw rows; the counters stay.
        with store._connect() as connection:
            connection.execute("DELETE FROM decisions WHERE deployment_id = ?", (deployment["id"],))

        detail = store.admin_deployment_order_overview(deployment["id"], period="all")
        page = store.admin_ai_strategy_overview({})

        assert detail["runtime_summary"]["analysis_count"] == 2
        assert detail["runtime_summary"]["signal_count"] == 1
        assert detail["runtime_summary"]["order_count"] == 1
        assert page["summary"]["analysis_count"] == 2
        assert page["summary"]["signal_count"] == 1
        assert page["summary"]["order_count"] == 1


def test_decision_detail_cleanup_keeps_days_that_are_not_summarised(tmp_path: Path) -> None:
    # Deleting a detail row before its day is summarised would lose statistics for
    # good, so an unsummarised day is kept however old it is.
    app = _app(tmp_path, "stats-cleanup.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_stats_cleanup", _vip_user(store))
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        store.save_decision(deployment["id"], "open", "req-summarised-0001", {"decision_id": "dec_sum", "action": "BUY"})
        store.save_decision(deployment["id"], "open", "req-unsummarised-0002", {"decision_id": "dec_unsum", "action": "BUY"})
        with store._connect() as connection:
            connection.execute(
                "UPDATE decisions SET created_at = ? WHERE request_id IN (?, ?)",
                (old_stamp, "req-summarised-0001", "req-unsummarised-0002"),
            )
            # Only one of the two days gets a summary row, so only it may be removed.
            connection.execute(
                "DELETE FROM mt_decision_time_summaries WHERE deployment_id = ?",
                (deployment["id"],),
            )
            connection.execute(
                """
                INSERT INTO mt_decision_time_summaries (
                    id, user_id, deployment_id, strategy_code, bucket_type, bucket_start,
                    analysis_count, signal_count, order_count, created_at, updated_at
                ) VALUES (?, '', ?, 'GL_ARBITRAGE_V1', 'day', ?, 1, 1, 1, ?, ?)
                """,
                (
                    "dsum_test_summarised",
                    deployment["id"],
                    _bucket_timestamp(int(datetime.fromisoformat(old_stamp).timestamp()), "day"),
                    old_stamp,
                    old_stamp,
                ),
            )
            # Both rows are in the same day, so give the second one its own day.
            connection.execute(
                "UPDATE decisions SET created_at = ? WHERE request_id = ?",
                ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), "req-unsummarised-0002"),
            )

        # Outside the block: the cleanup opens its own connection and would not see
        # uncommitted rows.
        deleted = store.cleanup_expired_decision_details()

        with store._connect() as connection:
            remaining = {
                str(row["request_id"])
                for row in connection.execute("SELECT request_id FROM decisions").fetchall()
            }
        assert deleted == 1
        assert remaining == {"req-unsummarised-0002"}


def test_overview_window_filters_on_the_day_bucket(tmp_path: Path) -> None:
    app = _app(tmp_path, "stats-window.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_stats_window", _vip_user(store))
        store.save_decision(deployment["id"], "open", "req-today-0001", {"decision_id": "dec_today_0001", "action": "BUY"})
        # A day from long ago, written straight into the summary as the retention
        # cleanup would leave it.
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        with store._connect() as connection:
            connection.execute(
                """
                INSERT INTO mt_decision_time_summaries (
                    id, user_id, deployment_id, strategy_code, bucket_type, bucket_start,
                    analysis_count, signal_count, order_count, created_at, updated_at
                ) VALUES (?, '', ?, 'GL_ARBITRAGE_V1', 'day', ?, 5, 2, 1, ?, ?)
                """,
                (
                    "dsum_test_old",
                    deployment["id"],
                    _bucket_timestamp(int(datetime.fromisoformat(old_stamp).timestamp()), "day"),
                    old_stamp,
                    old_stamp,
                ),
            )

        assert store.admin_ai_strategy_overview({})["summary"]["analysis_count"] == 6

        start_at, end_at = admin_period_range("today")
        windowed = store.admin_ai_strategy_overview({"start_at": start_at, "end_at": end_at})
        assert windowed["summary"]["analysis_count"] == 1
        assert windowed["summary"]["signal_count"] == 1


# ── the admin run switch ──────────────────────────────────────────────────────


def test_stopping_a_key_is_reported_in_the_init_status(tmp_path: Path) -> None:
    app = _app(tmp_path, "run-switch-init.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_run_switch_init", _vip_user(store))
        store.bind_deployment_login("gl_run_switch_init", login="60064845")

        def init_status() -> str:
            response = client.post(
                "/mt5/strategy/init",
                json={"deployment_key": "gl_run_switch_init", "account": {"platform": "MT5", "login": "60064845"}},
            )
            assert response.status_code == 200, response.text
            return response.json()["strategy"]["status"]

        assert init_status() == "active"

        stopped = store.set_deployment_run_enabled(deployment["id"], False)
        assert stopped["run_enabled"] in (0, False)
        assert stopped["blocked_login"] == "60064845"

        # The key still authenticates and still gets its config: the stop is told,
        # not enforced, and the EA acts on the text.
        assert "此账号已暂停运行" in init_status()

        store.set_deployment_run_enabled(deployment["id"], True)
        assert init_status() == "active"


def test_stopping_a_key_is_reported_in_the_regime_description(tmp_path: Path) -> None:
    app = _app(tmp_path, "run-switch-regime.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_run_switch_regime", _vip_user(store))
        store.set_deployment_run_enabled(deployment["id"], False)

        payload = {
            "deployment_key": "gl_run_switch_regime",
            "account": {"platform": "MT5", "login": "60064845"},
            "symbol": "XAUUSD",
            "timeframe": "M15",
            "market": {"secondary_bars": {"M15": _bars(), "H4": _bars(), "D1": _bars()}},
        }
        response = client.post("/mt5/strategy/regime", json=payload)

        assert response.status_code == 200, response.text
        body = response.json()
        assert "此账号已暂停运行" in body["description"]
        # The read itself is still produced.
        assert body["short"]["trend"] in {"bull", "bear", "range", "unknown"}


def test_a_new_bound_account_starts_the_key_again(tmp_path: Path) -> None:
    app = _app(tmp_path, "run-switch-relogin.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_run_switch_relogin", _vip_user(store))
        store.bind_deployment_login("gl_run_switch_relogin", login="60064845")
        store.set_deployment_run_enabled(deployment["id"], False)

        assert store.find_deployment_by_key("gl_run_switch_relogin")["run_enabled"] in (0, False)

        # The user points the key at another account: the stop was about the
        # account that was bound when it was stopped, so it clears on its own.
        updated = store.set_deployment_login("gl_run_switch_relogin", "70000001")
        assert updated["run_enabled"] in (1, True)
        assert updated["blocked_login"] == ""

        # Any account change re-opens the key, including one back to an account an
        # operator stopped earlier: the operator sees the new binding and can stop
        # it again, which is the whole point of tying the stop to the account.
        store.set_deployment_run_enabled(deployment["id"], False)
        store.set_deployment_login("gl_run_switch_relogin", "60064845")
        assert store.find_deployment_by_key("gl_run_switch_relogin")["run_enabled"] in (1, True)

        # Clearing the binding is not a change to a usable account, so the stop
        # that was in force for it stays.
        store.set_deployment_run_enabled(deployment["id"], False)
        store.set_deployment_login("gl_run_switch_relogin", "")
        assert store.find_deployment_by_key("gl_run_switch_relogin")["run_enabled"] in (0, False)


def test_admin_route_toggles_the_run_switch(tmp_path: Path) -> None:
    app = _app(tmp_path, "run-switch-route.db")
    with TestClient(app) as client:
        store = app.state.store
        deployment = _deploy(store, "gl_run_switch_route", _vip_user(store))

        stopped = client.post(
            "/api/admin/ai/deployment/run-state",
            json={"deployment_id": deployment["id"], "enabled": False},
        )
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"]["run_enabled"] is False

        overview = client.post("/api/admin/ai/stats/overview", json={"period": "today"}).json()
        row = next(item for item in overview["data"]["strategies"] if item["id"] == deployment["id"])
        assert row["run_enabled"] is False

        resumed = client.post(
            "/api/admin/ai/deployment/run-state",
            json={"deployment_id": deployment["id"], "enabled": True},
        )
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["data"]["run_enabled"] is True

        missing = client.post(
            "/api/admin/ai/deployment/run-state",
            json={"deployment_id": "dep_does_not_exist", "enabled": False},
        )
        assert missing.status_code == 404

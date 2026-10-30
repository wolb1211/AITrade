from __future__ import annotations

from pathlib import Path

from app.store import SqliteStore, _bucket_timestamp, utc_now_iso


def _deployment(store: SqliteStore):
    return store.upsert_web_deployment(
        "gl_history_rollup_key",
        user_id="1",
        strategy_code="PA_AGENT_V1",
        strategy_name="GL趋势自动分析策略",
        status="active",
        symbol="*",
        timeframe="*",
        config={"deployment_key": "gl_history_rollup_key"},
    )


def _order(index: int, close_time: int, profit: float) -> dict:
    return {
        "order_id": f"deal-{index}",
        "deal_id": f"deal-{index}",
        "symbol": "XAUUSD",
        "mt_type": 0,
        "volume": 0.1,
        "open_price": 4500,
        "close_price": 4510,
        "profit": profit,
        "commission": 0,
        "swap": 0,
        "open_time": close_time - 600,
        "close_time": close_time,
        "comment": "",
    }


def _day_summary(store: SqliteStore) -> tuple[int, float]:
    with store._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            """
            SELECT COALESCE(SUM(order_count), 0) orders,
                   COALESCE(SUM(net_profit), 0) pnl
            FROM mt_order_time_summaries
            WHERE bucket_type = 'day'
            """
        ).fetchone()
    return int(row["orders"]), round(float(row["pnl"]), 2)


def test_refresh_never_shrinks_a_rollup_whose_detail_was_trimmed(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "rollup.db")
    store.initialize()
    deployment = _deployment(store)
    close_time = 1_791_600_000  # a fixed instant

    store.sync_mt5_history_deals(
        deployment["id"],
        account_login="60064845",
        account_server="DLSMarkets-Live",
        orders=[_order(1, close_time, 120.0), _order(2, close_time + 60, -20.0)],
    )
    assert _day_summary(store) == (2, 100.0)

    # The retention cleanup removes one of the two details but keeps the rollup.
    with store._connect() as connection:  # noqa: SLF001
        connection.execute("DELETE FROM mt5_history_deals WHERE deal_id = ?", ("deal-2",))

    bucket_start = _bucket_timestamp(close_time, "day")
    with store._connect() as connection:  # noqa: SLF001
        store._refresh_order_summary_bucket(  # noqa: SLF001
            connection,
            deployment_id=deployment["id"],
            bucket_type="day",
            bucket_start=bucket_start,
            now=utc_now_iso(),
        )

    # Lifetime profit is the permanent record: the trimmed detail must not win.
    assert _day_summary(store) == (2, 100.0)

    with store._connect() as connection:  # noqa: SLF001
        store._refresh_order_summary_bucket(  # noqa: SLF001
            connection,
            deployment_id=deployment["id"],
            bucket_type="day",
            bucket_start=bucket_start,
            now=utc_now_iso(),
            allow_shrink=True,
        )

    assert _day_summary(store) == (1, 120.0)

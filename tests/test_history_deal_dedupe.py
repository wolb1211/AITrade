from __future__ import annotations

from pathlib import Path

from app.store import SqliteStore


def _deployment(store: SqliteStore):
    return store.upsert_web_deployment(
        "gl_history_dedupe_key",
        user_id="1",
        strategy_code="PA_AGENT_V1",
        strategy_name="GL趋势自动分析策略",
        status="active",
        symbol="*",
        timeframe="*",
        config={"deployment_key": "gl_history_dedupe_key"},
    )


def _order(index: int, profit: float) -> dict:
    return {
        "order_id": f"order-{index}",
        "deal_id": f"order-{index}",
        "symbol": "XAUUSD",
        "mt_type": 0,
        "volume": 0.1,
        "open_price": 4500,
        "close_price": 4510,
        "profit": profit,
        "commission": 0,
        "swap": 0,
        "open_time": 1_700_000_000 + index * 600,
        "close_time": 1_700_000_300 + index * 600,
        "comment": "",
    }


def _totals(store: SqliteStore) -> tuple[int, float]:
    with store._connect() as connection:  # noqa: SLF001 - assertions on raw rows
        row = connection.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(net_profit), 0) pnl FROM mt5_history_deals"
        ).fetchone()
    return int(row["c"]), float(row["pnl"])


def test_sync_without_server_reuses_stored_server(tmp_path: Path) -> None:
    """A sync that omits the account identity must not duplicate every deal."""
    store = SqliteStore(tmp_path / "dedupe.db")
    store.initialize()
    deployment = _deployment(store)
    orders = [_order(index, profit) for index, profit in enumerate([120.0, -40.0])]

    store.sync_mt5_history_deals(
        deployment["id"],
        account_login="60064845",
        account_server="DLSMarkets-Live",
        orders=orders,
    )
    assert _totals(store) == (2, 80.0)

    store.sync_mt5_history_deals(
        deployment["id"],
        account_login="60064845",
        account_server="",
        orders=orders,
    )

    assert _totals(store) == (2, 80.0)


def test_swap_and_commission_keep_their_mt5_sign(tmp_path: Path) -> None:
    """net = profit + commission + swap, with the signed values MT5 reports."""
    store = SqliteStore(tmp_path / "sign.db")
    store.initialize()
    deployment = _deployment(store)

    store.sync_mt5_history_deals(
        deployment["id"],
        account_login="60064845",
        account_server="DLSMarkets-Live",
        orders=[
            {
                **_order(1, 13.06),
                "commission": -0.5,
                "swap": -1.45,
            }
        ],
    )

    with store._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT profit, commission, swap, net_profit FROM mt5_history_deals"
        ).fetchone()

    assert float(row["net_profit"]) == 13.06 - 0.5 - 1.45


def test_duplicate_empty_server_rows_are_removed_once(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "cleanup.db")
    store.initialize()
    deployment = _deployment(store)
    order = _order(1, 100.0)

    store.sync_mt5_history_deals(
        deployment["id"],
        account_login="60064845",
        account_server="DLSMarkets-Live",
        orders=[order],
    )
    # Simulate rows written by an older build: same deal, no server name.
    with store._connect() as connection:  # noqa: SLF001
        connection.execute(
            "DELETE FROM system_settings WHERE setting_key = ?",
            ("history_deals_server_dedupe",),
        )
        connection.execute(
            """
            INSERT INTO mt5_history_deals (
                id, deployment_id, account_login, account_server, deal_id,
                order_id, position_id, symbol, mt_type, entry, volume, price,
                open_price, close_price, profit, commission, swap, net_profit,
                deal_time, open_time, close_time, comment, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "deal_legacy_dup", deployment["id"], "60064845", "", "order-1",
                "order-1", "order-1", "XAUUSD", "0", "out", 0.1, 4510,
                4500, 4510, 100, 0, 0, 100,
                1_700_000_300, 1_700_000_000, 1_700_000_300, "",
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
    assert _totals(store)[0] == 2

    store._dedupe_history_deals_by_server()  # noqa: SLF001

    assert _totals(store) == (1, 100.0)

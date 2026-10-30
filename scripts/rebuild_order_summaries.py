"""Rebuild the daily/hourly order rollups from the raw MT5 deals.

The client and admin dashboards read ``mt_order_time_summaries``, not the deal
rows. A bucket that was written while the raw deals were duplicated keeps its
inflated totals forever, so this script recomputes them.

    cd /path/to/gainlabaiapi
    .venv/bin/python scripts/rebuild_order_summaries.py            # everything
    .venv/bin/python scripts/rebuild_order_summaries.py dep_xxxx   # one deployment

Read-only against deals; it only rewrites the derived rollup table.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _connect():
    from app.config import Settings
    from app.store import MySQLStore, SqliteStore

    resolved = Settings.from_env()
    if resolved.database_type == 'mysql':
        return MySQLStore(
            host=resolved.mysql_host,
            port=resolved.mysql_port,
            database=resolved.mysql_database,
            user=resolved.mysql_user,
            password=resolved.mysql_password,
        )
    return SqliteStore(resolved.database_path)


def main() -> None:
    deployment_id = sys.argv[1].strip() if len(sys.argv) > 1 else None
    store = _connect()
    store.initialize()

    print('--- 汇总表 vs 原始成交（重建前）---')
    with store._connect() as conn:  # noqa: SLF001 - read only inspection
        raw = conn.execute(
            """
            SELECT COUNT(*) deals, ROUND(COALESCE(SUM(net_profit), 0), 2) pnl
            FROM mt5_history_deals
            WHERE LOWER(entry) IN ('out', 'out_by', 'inout')
            """
        ).fetchone()
        rollup = conn.execute(
            """
            SELECT COALESCE(SUM(order_count), 0) orders,
                   ROUND(COALESCE(SUM(net_profit), 0), 2) pnl
            FROM mt_order_time_summaries
            WHERE bucket_type = 'day'
            """
        ).fetchone()
    print('raw deals :', dict(raw))
    print('rollup day:', dict(rollup))
    if int(raw['deals'] or 0) != int(rollup['orders'] or 0):
        print('!! 数量不一致，汇总表就是页面数字偏大的原因')

    result = store.rebuild_order_time_summaries(deployment_id=deployment_id)
    print()
    print('--- 重建结果 ---')
    for key, value in result.items():
        print(f'{key}: {value}')


if __name__ == '__main__':
    main()

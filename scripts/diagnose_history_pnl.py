"""Read-only diagnostic for the EA / client / admin P&L mismatch.

Run it on the server that owns the database:

    cd /path/to/gainlabaiapi && .venv/bin/python scripts/diagnose_history_pnl.py

It answers four questions:

1. 成交是不是被重复写入（同一条 deal 同时存在「有服务器名」和「无服务器名」两行）。
2. 每个商品的单数与净盈亏，确认多出来的钱是不是来自另一个商品。
3. 成交数（deal）和开仓次数（position/单）差多少，确认「分批减仓」造成的放大倍数。
4. 手续费 / 库存费的符号，确认净盈亏是 profit + commission + swap。
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


def _section(title: str) -> None:
    print()
    print(f'=== {title} ===')


def main() -> None:
    store = _connect()
    with store._connect() as conn:  # noqa: SLF001 - read only inspection
        _section('1. 重复成交（同 deal_id 出现在多个 server 名下）')
        rows = conn.execute(
            """
            SELECT account_login, deal_id, COUNT(*) rows_count,
                   GROUP_CONCAT(account_server) servers
            FROM mt5_history_deals
            GROUP BY account_login, deal_id
            HAVING COUNT(*) > 1
            LIMIT 20
            """
        ).fetchall()
        for row in rows:
            print(dict(row))
        if not rows:
            print('(没有重复)')

        _section('2. 按商品汇总')
        rows = conn.execute(
            """
            SELECT deployment_id, account_login, account_server, symbol,
                   COUNT(*) orders,
                   SUM(CASE WHEN net_profit > 0 THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN net_profit < 0 THEN 1 ELSE 0 END) losses,
                   ROUND(SUM(volume), 2) volume,
                   ROUND(SUM(profit), 2) profit,
                   ROUND(SUM(commission), 2) commission,
                   ROUND(SUM(swap), 2) swap,
                   ROUND(SUM(net_profit), 2) net_profit
            FROM mt5_history_deals
            GROUP BY deployment_id, account_login, account_server, symbol
            ORDER BY orders DESC
            """
        ).fetchall()
        for row in rows:
            print(dict(row))

        _section('3. 成交数 vs 开仓次数（分批减仓的放大倍数）')
        rows = conn.execute(
            """
            SELECT deployment_id, account_login, symbol,
                   COUNT(*) deals,
                   COUNT(DISTINCT open_time) distinct_open_time,
                   ROUND(COUNT(*) / NULLIF(COUNT(DISTINCT open_time), 0), 2) deals_per_open
            FROM mt5_history_deals
            GROUP BY deployment_id, account_login, symbol
            """
        ).fetchall()
        for row in rows:
            print(dict(row))

        _section('4. 手续费 / 库存费符号（抽查绝对值最大的几条）')
        rows = conn.execute(
            """
            SELECT deal_id, symbol, profit, commission, swap, net_profit
            FROM mt5_history_deals
            WHERE commission != 0 OR swap != 0
            ORDER BY ABS(commission) + ABS(swap) DESC
            LIMIT 10
            """
        ).fetchall()
        for row in rows:
            print(dict(row))
        if not rows:
            print('(全部为 0，无从判断符号；说明这组数据不含费用)')

        _section('5. 开仓价 / 开仓时间是否与平仓相同（EA 上报质量）')
        row = conn.execute(
            """
            SELECT SUM(CASE WHEN ABS(open_price - close_price) < 0.000001 THEN 1 ELSE 0 END) same_price,
                   SUM(CASE WHEN open_time = close_time THEN 1 ELSE 0 END) same_time,
                   COUNT(*) total
            FROM mt5_history_deals
            """
        ).fetchone()
        print(dict(row))


if __name__ == '__main__':
    main()

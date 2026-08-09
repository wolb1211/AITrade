from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pymysql
from pymysql.err import IntegrityError as MySqlIntegrityError

from app.security import hash_deployment_key

LOCAL_TIMEZONE = timezone(timedelta(hours=8))

DatabaseIntegrityError = (sqlite3.IntegrityError, MySqlIntegrityError)


class DbRow(dict[str, Any]):
    def __init__(self, columns: list[str], values: tuple[Any, ...]) -> None:
        super().__init__(zip(columns, values, strict=False))
        self._values = values

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _mysql_sql(sql: str) -> str:
    transformed = sql.strip()
    if transformed.upper().startswith("PRAGMA "):
        return ""
    if transformed.upper() == "BEGIN IMMEDIATE":
        return "START TRANSACTION"
    transformed = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT IGNORE", transformed, flags=re.IGNORECASE)
    transformed = re.sub(
        r"ON\s+CONFLICT\s*\([^)]+\)\s+DO\s+UPDATE\s+SET",
        "ON DUPLICATE KEY UPDATE",
        transformed,
        flags=re.IGNORECASE,
    )
    transformed = re.sub(
        r"\bexcluded\.([A-Za-z_][A-Za-z0-9_]*)\b",
        r"VALUES(\1)",
        transformed,
    )
    return transformed.replace("?", "%s")


class MySqlCursor:
    def __init__(self, cursor: Any) -> None:
        self.cursor = cursor

    def execute(self, sql: str, params: Any = None) -> "MySqlCursor":
        transformed = _mysql_sql(sql)
        if not transformed:
            return self
        if params is None:
            self.cursor.execute(transformed)
        else:
            self.cursor.execute(transformed, params)
        return self

    def fetchone(self) -> DbRow | None:
        row = self.cursor.fetchone()
        if row is None:
            return None
        columns = [item[0] for item in self.cursor.description or []]
        return DbRow(columns, row)

    def fetchall(self) -> list[DbRow]:
        rows = self.cursor.fetchall()
        columns = [item[0] for item in self.cursor.description or []]
        return [DbRow(columns, row) for row in rows]


class MySqlConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def __enter__(self) -> "MySqlConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        self.connection.close()

    def execute(self, sql: str, params: Any = None) -> MySqlCursor:
        cursor = self.connection.cursor()
        return MySqlCursor(cursor).execute(sql, params)

    def executescript(self, _script: str) -> None:
        return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _period_bounds(period: str) -> tuple[str, str, int, int, str]:
    now_local = datetime.now(LOCAL_TIMEZONE)
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    normalized = (period or "all").strip().lower()
    if normalized == "today":
        start = today
        bucket = "hour"
    elif normalized == "week":
        start = today - timedelta(days=today.weekday())
        bucket = "day"
    elif normalized == "month":
        start = today.replace(day=1)
        bucket = "day"
    elif normalized == "7d":
        start = now_local - timedelta(days=7)
        bucket = "day"
    elif normalized == "30d":
        start = now_local - timedelta(days=30)
        bucket = "day"
    else:
        start = datetime(1970, 1, 1, tzinfo=LOCAL_TIMEZONE)
        bucket = "day"
    now_utc = now_local.astimezone(timezone.utc)
    start_utc = start.astimezone(timezone.utc)
    return start_utc.isoformat(), now_utc.isoformat(), int(start_utc.timestamp()), int(now_utc.timestamp()), bucket


def _time_bucket(timestamp: int, bucket: str) -> str:
    if not timestamp:
        return ""
    value = datetime.fromtimestamp(timestamp, LOCAL_TIMEZONE)
    if bucket == "hour":
        return value.strftime("%m-%d %H:00")
    return value.strftime("%m-%d")


def _pnl_curve_points(bucket_values: dict[str, float]) -> list[dict[str, Any]]:
    running = 0.0
    points: list[dict[str, Any]] = []
    for key in sorted(item for item in bucket_values if item):
        running = round(running + bucket_values[key], 2)
        points.append({"time": key, "pnl": running, "change": round(bucket_values[key], 2)})
    return points


def _is_demo_server(server: str) -> bool:
    normalized = (server or "").lower()
    return any(token in normalized for token in ("demo", "trial", "practice", "模拟", "測試", "测试"))


def _extract_profit(payload: dict[str, Any]) -> float:
    for key in ("profit", "pnl", "net_profit"):
        value = payload.get(key)
        if isinstance(value, int | float):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def _deal_net_profit(payload: dict[str, Any]) -> float:
    value = payload.get("net_profit")
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    return (
        _float_or_zero(payload.get("profit"))
        + _float_or_zero(payload.get("commission"))
        + _float_or_zero(payload.get("swap"))
    )


def _float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_profit_deal_entry(entry: str) -> bool:
    return entry.strip().lower() in {"out", "out_by", "inout"}


class SqliteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deployments (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    strategy_code TEXT NOT NULL,
                    strategy_name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    mt_platform TEXT,
                    mt_login TEXT,
                    mt_server TEXT,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(deployment_id, endpoint, request_id),
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );

                CREATE TABLE IF NOT EXISTS heartbeats (
                    deployment_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );

                CREATE TABLE IF NOT EXISTS execution_reports (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );

                CREATE TABLE IF NOT EXISTS mt5_history_deals (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    account_login TEXT NOT NULL,
                    account_server TEXT NOT NULL DEFAULT '',
                    deal_id TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    position_id TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    mt_type TEXT NOT NULL DEFAULT '',
                    entry TEXT NOT NULL DEFAULT '',
                    volume REAL NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    profit REAL NOT NULL DEFAULT 0,
                    commission REAL NOT NULL DEFAULT 0,
                    swap REAL NOT NULL DEFAULT 0,
                    net_profit REAL NOT NULL DEFAULT 0,
                    deal_time INTEGER NOT NULL DEFAULT 0,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_login, account_server, deal_id),
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );

                CREATE TABLE IF NOT EXISTS ai_providers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider_type TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort INTEGER NOT NULL DEFAULT 9999,
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_models (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    base_url TEXT NOT NULL DEFAULT '',
                    context_window INTEGER NOT NULL DEFAULT 0,
                    input_token_rate REAL NOT NULL DEFAULT 0,
                    output_token_rate REAL NOT NULL DEFAULT 0,
                    billing_multiplier REAL NOT NULL DEFAULT 1,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort INTEGER NOT NULL DEFAULT 9999,
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(provider_id) REFERENCES ai_providers(id)
                );

                CREATE TABLE IF NOT EXISTS ai_user_quotas (
                    user_id TEXT PRIMARY KEY,
                    monthly_quota INTEGER NOT NULL DEFAULT 0,
                    extra_quota INTEGER NOT NULL DEFAULT 0,
                    used_tokens INTEGER NOT NULL DEFAULT 0,
                    reset_at TEXT NOT NULL DEFAULT '',
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_usage_logs (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL DEFAULT '',
                    strategy_code TEXT NOT NULL DEFAULT '',
                    endpoint TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    official_tokens INTEGER NOT NULL DEFAULT 0,
                    custom_tokens INTEGER NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 1,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS official_ai_strategies (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    badge TEXT NOT NULL DEFAULT 'Gainlab',
                    version TEXT NOT NULL DEFAULT '1.0',
                    status TEXT NOT NULL DEFAULT 'active',
                    summary TEXT NOT NULL DEFAULT '',
                    open_logic TEXT NOT NULL DEFAULT '',
                    position_logic TEXT NOT NULL DEFAULT '',
                    open_data_type TEXT NOT NULL DEFAULT 'kline',
                    open_kline_count INTEGER NOT NULL DEFAULT 100,
                    position_data_type TEXT NOT NULL DEFAULT 'kline',
                    position_kline_count INTEGER NOT NULL DEFAULT 100,
                    call_mode TEXT NOT NULL DEFAULT 'bar',
                    call_value INTEGER NOT NULL DEFAULT 1,
                    open_model_id TEXT NOT NULL DEFAULT '',
                    position_model_id TEXT NOT NULL DEFAULT '',
                    default_config_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort INTEGER NOT NULL DEFAULT 9999,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deployment_activity_logs (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    strategy_code TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );

                CREATE TABLE IF NOT EXISTS deployment_accounts (
                    id TEXT PRIMARY KEY,
                    deployment_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    login TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'MT5',
                    provider TEXT NOT NULL DEFAULT '',
                    server TEXT NOT NULL DEFAULT '',
                    is_demo INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE(deployment_id, login, server),
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );
                """
            )
            self._ensure_mt5_history_columns(connection)
            self._ensure_ai_model_columns(connection)
            self._ensure_official_strategy_seed(connection)

    def _ensure_mt5_history_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(mt5_history_deals)").fetchall()
        }
        migrations = {
            "open_price": "ALTER TABLE mt5_history_deals ADD COLUMN open_price REAL NOT NULL DEFAULT 0",
            "close_price": "ALTER TABLE mt5_history_deals ADD COLUMN close_price REAL NOT NULL DEFAULT 0",
            "open_time": "ALTER TABLE mt5_history_deals ADD COLUMN open_time INTEGER NOT NULL DEFAULT 0",
            "close_time": "ALTER TABLE mt5_history_deals ADD COLUMN close_time INTEGER NOT NULL DEFAULT 0",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        connection.execute("UPDATE mt5_history_deals SET close_price = price WHERE close_price = 0 AND price != 0")
        connection.execute("UPDATE mt5_history_deals SET close_time = deal_time WHERE close_time = 0 AND deal_time != 0")

    def _ensure_ai_model_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ai_models)").fetchall()
        }
        if "base_url" not in columns:
            connection.execute("ALTER TABLE ai_models ADD COLUMN base_url TEXT NOT NULL DEFAULT ''")
        connection.execute(
            """
            UPDATE ai_models
            SET base_url = (
                SELECT p.base_url
                FROM ai_providers p
                WHERE p.id = ai_models.provider_id
            )
            WHERE base_url = ''
            """,
        )

    def _ensure_official_strategy_seed(self, connection: sqlite3.Connection) -> None:
        now = utc_now_iso()
        summary = (
            "本策略由 PA Agent（AI 分析代理）驱动，结合最新K线形态与关键指标，自动判断当前行情更可能属于以下三类状态之一：\n"
            "突破（Breakout）：识别有效突破信号，跟随趋势推进，并执行进场开仓。\n"
            "趋势（Trend）：当指标呈现顺势结构时，倾向趋势跟随，减少反复进出干扰。\n"
            "震荡（Range）：当价格波动缺乏趋势延续条件时，策略降低追单频率，避免震荡段的无效开仓。\n"
            "在完成行情判别后，策略会：\n"
            "自动下单：生成符合当前行情结构的开仓方向与执行节奏；\n"
            "设置止盈止损（TP/SL）：依据策略配置与风险框架动态生成目标与保护价；\n"
            "风控提前平仓：若价格运行未按预期发展（例如突破失败、趋势退化或条件反转），将触发提前风控平仓，降低回撤扩大的概率。\n"
            "整体目标是让交易决策具备“形态理解 + 指标过滤 + 风险约束”的闭环能力，在不同市场状态下实现更稳健的执行。"
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO official_ai_strategies (
                id, code, name, badge, version, status, summary,
                open_logic, position_logic, open_data_type, open_kline_count,
                position_data_type, position_kline_count, call_mode, call_value,
                default_config_json, enabled, sort, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ofs_pa_agent_v1",
                "PA_AGENT_V1",
                "Gainlab-PA BreakTrend Autopilot（PA突破趋势自动驾驶）",
                "Gainlab",
                "1.0",
                "active",
                summary,
                "先计算K线实体、影线、重叠、EMA、ATR、区间位置和突破状态，再判断是否出现趋势突破或趋势延续开仓机会。",
                "结合反向 PA 信号、浮动盈亏和结构止损位判断是否平仓或移动止损，后续接入 AI 后由两阶段分析输出统一风控动作。",
                "kline",
                100,
                "kline",
                100,
                "bar",
                1,
                json.dumps(
                    {
                        "position_sizing_mode": "fixed",
                        "fixed_lot": 0.01,
                        "risk_mode": "fixed_stop_amount",
                        "max_stop_amount": 100,
                        "max_positions": 1,
                        "allow_add_position": False,
                    },
                    ensure_ascii=False,
                ),
                1,
                10,
                now,
                now,
            ),
        )

    def ensure_demo_deployment(self, raw_key: str) -> None:
        key_hash = hash_deployment_key(raw_key)
        now = utc_now_iso()
        config = {
            "lot": 0.01,
            "sl_distance": 5.0,
            "tp_distance": 8.0,
            "max_loss_per_position": 100.0,
            "take_profit_per_position": 150.0,
            "open_data_type": "kline",
            "open_kline_count": 100,
            "position_data_type": "kline",
            "position_kline_count": 100,
            "call_mode": "bar",
            "call_val": 1,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO deployments (
                    id, user_id, strategy_code, strategy_name, key_hash,
                    status, symbol, timeframe, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "dep_demo_pa_xauusd_m15",
                    "user_demo",
                    "PA_MOCK_V1",
                    "GainLab PA Base Demo",
                    key_hash,
                    "active",
                    "XAUUSD",
                    "M15",
                    json.dumps(config),
                    now,
                    now,
                ),
            )

    def find_deployment_by_key(self, raw_key: str) -> dict[str, Any] | None:
        key_hash = hash_deployment_key(raw_key)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
        return self._deployment_row(row) if row else None

    def upsert_web_deployment(
        self,
        raw_key: str,
        *,
        user_id: str,
        strategy_code: str,
        strategy_name: str,
        status: str,
        symbol: str,
        timeframe: str,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        key_hash = hash_deployment_key(raw_key)
        now = utc_now_iso()
        existing = self.find_deployment_by_key(raw_key)
        deployment_id = existing["id"] if existing else f"dep_{uuid4().hex}"

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO deployments (
                    id, user_id, strategy_code, strategy_name, key_hash,
                    status, symbol, timeframe, config_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key_hash) DO UPDATE SET
                    user_id = excluded.user_id,
                    strategy_code = excluded.strategy_code,
                    strategy_name = excluded.strategy_name,
                    status = excluded.status,
                    symbol = excluded.symbol,
                    timeframe = excluded.timeframe,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (
                    deployment_id,
                    user_id,
                    strategy_code,
                    strategy_name,
                    key_hash,
                    status,
                    symbol,
                    timeframe,
                    json.dumps(config),
                    now,
                    now,
                ),
            )
        deployment = self.find_deployment_by_key(raw_key)
        if deployment is None:
            raise RuntimeError("deployment_upsert_failed")
        return deployment

    def list_web_deployments(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM deployments
                WHERE user_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id,),
            ).fetchall()
        deployments = [self._deployment_row(row) for row in rows]
        return [
            deployment
            for deployment in deployments
            if deployment["config"].get("deployment_key")
        ]

    def deployment_runtime_stats(self, deployment_id: str) -> dict[str, Any]:
        stats = {
            "analysis_count": 0,
            "signal_count": 0,
            "order_count": 0,
            "official_tokens_used": 0,
            "custom_tokens_used": 0,
            "pnl": 0.0,
        }
        with self._connect() as connection:
            decision_rows = connection.execute(
                """
                SELECT endpoint, response_json
                FROM decisions
                WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchall()
            usage_row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(official_tokens), 0) AS official_tokens_used,
                    COALESCE(SUM(custom_tokens), 0) AS custom_tokens_used
                FROM ai_usage_logs
                WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchone()
            history_rows = connection.execute(
                """
                SELECT entry, net_profit
                FROM mt5_history_deals
                WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchall()

        stats["analysis_count"] = len(decision_rows)
        for row in decision_rows:
            try:
                payload = json.loads(row["response_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            action = str(payload.get("action") or "").upper()
            if action and action != "HOLD":
                stats["signal_count"] += 1
            if row["endpoint"] == "open" and action in {"BUY", "SELL"}:
                stats["order_count"] += 1

        if usage_row is not None:
            stats["official_tokens_used"] = int(usage_row["official_tokens_used"] or 0)
            stats["custom_tokens_used"] = int(usage_row["custom_tokens_used"] or 0)

        pnl = sum(
            float(row["net_profit"] or 0)
            for row in history_rows
            if _is_profit_deal_entry(str(row["entry"] or ""))
        )
        stats["pnl"] = round(pnl, 2)
        return stats

    def deployment_detail_stats(self, deployment_id: str) -> dict[str, Any]:
        stats = self.deployment_runtime_stats(deployment_id)
        with self._connect() as connection:
            history_rows = connection.execute(
                """
                SELECT symbol, entry, net_profit, deal_time
                FROM mt5_history_deals
                WHERE deployment_id = ?
                ORDER BY deal_time ASC, updated_at ASC
                """,
                (deployment_id,),
            ).fetchall()

        win_count = 0
        loss_count = 0
        flat_count = 0
        symbols: set[str] = set()
        curve: list[dict[str, Any]] = []
        cumulative_pnl = 0.0

        for row in history_rows:
            if not _is_profit_deal_entry(str(row["entry"] or "")):
                continue
            symbol = str(row["symbol"] or "").strip()
            if symbol:
                symbols.add(symbol)
            pnl = round(float(row["net_profit"] or 0), 2)
            if pnl > 0:
                win_count += 1
            elif pnl < 0:
                loss_count += 1
            else:
                flat_count += 1
            cumulative_pnl = round(cumulative_pnl + pnl, 2)
            curve.append(
                {
                    "time": int(row["deal_time"] or 0),
                    "pnl": pnl,
                    "cumulative_pnl": cumulative_pnl,
                },
            )

        closed_count = win_count + loss_count + flat_count
        return {
            "summary": {
                **stats,
                "win_count": win_count,
                "loss_count": loss_count,
                "flat_count": flat_count,
                "win_rate": round((win_count / closed_count) * 100, 2) if closed_count else 0.0,
                "traded_symbol_count": len(symbols),
            },
            "curve": curve,
        }

    def list_deployment_history_orders(
        self,
        deployment_id: str,
        *,
        page: int = 1,
        size: int = 100,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        size = max(1, min(500, int(size or 100)))
        offset = (page - 1) * size
        with self._connect() as connection:
            total_row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM mt5_history_deals
                WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT
                    deal_id, order_id, symbol, mt_type, volume,
                    open_price, close_price, price, profit, commission, swap,
                    net_profit, open_time, close_time, deal_time, comment
                FROM mt5_history_deals
                WHERE deployment_id = ?
                ORDER BY COALESCE(NULLIF(close_time, 0), deal_time) DESC, updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (deployment_id, size, offset),
            ).fetchall()
        orders = []
        for row in rows:
            close_price = float(row["close_price"] or row["price"] or 0)
            close_time = int(row["close_time"] or row["deal_time"] or 0)
            orders.append(
                {
                    "order_id": str(row["order_id"] or row["deal_id"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "mt_type": str(row["mt_type"] or ""),
                    "volume": float(row["volume"] or 0),
                    "open_price": float(row["open_price"] or 0),
                    "close_price": close_price,
                    "profit": float(row["profit"] or 0),
                    "commission": float(row["commission"] or 0),
                    "swap": float(row["swap"] or 0),
                    "net_profit": float(row["net_profit"] or 0),
                    "open_time": int(row["open_time"] or 0),
                    "close_time": close_time,
                    "comment": str(row["comment"] or ""),
                },
            )
        return {
            "total": int(total_row["total"] or 0) if total_row else 0,
            "orders": orders,
        }

    def admin_deployment_account_symbol_stats(
        self,
        deployment_id: str,
        *,
        period: str = "all",
    ) -> dict[str, Any]:
        _, _, start_ts, end_ts, _ = _period_bounds(period)
        with self._connect() as connection:
            deployment = connection.execute(
                "SELECT * FROM deployments WHERE id = ?",
                (deployment_id,),
            ).fetchone()
            if deployment is None:
                raise RuntimeError("deployment_not_found")
            account_rows = connection.execute(
                """
                SELECT login, provider, server, last_seen_at
                FROM deployment_accounts
                WHERE deployment_id = ?
                """,
                (deployment_id,),
            ).fetchall()
            history_rows = connection.execute(
                """
                SELECT
                    account_login, account_server, symbol, mt_type, volume,
                    net_profit, close_time, deal_time, updated_at
                FROM mt5_history_deals
                WHERE deployment_id = ?
                  AND COALESCE(NULLIF(close_time, 0), deal_time) >= ?
                  AND COALESCE(NULLIF(close_time, 0), deal_time) <= ?
                """,
                (deployment_id, start_ts, end_ts),
            ).fetchall()

        account_map = {
            (str(row["login"] or ""), str(row["server"] or "")): {
                "provider": str(row["provider"] or ""),
                "server": str(row["server"] or ""),
                "last_seen_at": str(row["last_seen_at"] or ""),
            }
            for row in account_rows
        }
        latest_account_by_login: dict[str, dict[str, str]] = {}
        for row in account_rows:
            login = str(row["login"] or "")
            current = latest_account_by_login.get(login)
            if current is None or str(row["last_seen_at"] or "") > str(current.get("last_seen_at") or ""):
                latest_account_by_login[login] = {
                    "provider": str(row["provider"] or ""),
                    "server": str(row["server"] or ""),
                    "last_seen_at": str(row["last_seen_at"] or ""),
                }
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in history_rows:
            if not _is_profit_deal_entry("out"):
                continue
            login = str(row["account_login"] or "")
            server = str(row["account_server"] or "")
            symbol = str(row["symbol"] or "")
            key = (login, server, symbol)
            account_info = account_map.get((login, server), {}) or latest_account_by_login.get(login, {})
            item = groups.setdefault(
                key,
                {
                    "deployment_id": deployment_id,
                    "user_id": str(deployment["user_id"] or ""),
                    "account_login": login,
                    "order_account_server": server,
                    "account_provider": str(account_info.get("provider") or ""),
                    "account_server": server or str(account_info.get("server") or ""),
                    "symbol": symbol,
                    "close_order_count": 0,
                    "win_count": 0,
                    "loss_count": 0,
                    "flat_count": 0,
                    "volume": 0.0,
                    "pnl": 0.0,
                    "last_close_time": 0,
                    "last_active_at": str(account_info.get("last_seen_at") or ""),
                },
            )
            profit = float(row["net_profit"] or 0)
            item["close_order_count"] += 1
            item["volume"] = round(float(item["volume"]) + float(row["volume"] or 0), 4)
            item["pnl"] = round(float(item["pnl"]) + profit, 2)
            if profit > 0:
                item["win_count"] += 1
            elif profit < 0:
                item["loss_count"] += 1
            else:
                item["flat_count"] += 1
            close_time = int(row["close_time"] or row["deal_time"] or 0)
            item["last_close_time"] = max(int(item["last_close_time"] or 0), close_time)
            item["last_active_at"] = max(str(item["last_active_at"] or ""), str(row["updated_at"] or ""))

        items = []
        for item in groups.values():
            closed_count = int(item["close_order_count"] or 0)
            item["win_rate"] = round((int(item["win_count"] or 0) / closed_count) * 100, 2) if closed_count else 0.0
            items.append(item)
        return {
            "deployment": self._deployment_row(deployment),
            "list": sorted(
                items,
                key=lambda item: (
                    str(item.get("last_active_at") or ""),
                    int(item.get("last_close_time") or 0),
                    float(item.get("pnl") or 0),
                ),
                reverse=True,
            ),
        }

    def admin_deployment_history_orders(
        self,
        deployment_id: str,
        *,
        account_login: str = "",
        account_server: str = "",
        symbol: str = "",
        period: str = "all",
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        size = max(1, min(500, int(size or 50)))
        offset = (page - 1) * size
        _, _, start_ts, end_ts, _ = _period_bounds(period)
        where = [
            "deployment_id = ?",
            "COALESCE(NULLIF(close_time, 0), deal_time) >= ?",
            "COALESCE(NULLIF(close_time, 0), deal_time) <= ?",
        ]
        params: list[Any] = [deployment_id, start_ts, end_ts]
        if account_login:
            where.append("account_login = ?")
            params.append(account_login)
        if account_server:
            where.append("account_server = ?")
            params.append(account_server)
        if symbol:
            where.append("symbol = ?")
            params.append(symbol)
        where_sql = " AND ".join(where)
        with self._connect() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) AS total FROM mt5_history_deals WHERE {where_sql}",
                params,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT
                    account_login, account_server, deal_id, order_id, symbol,
                    mt_type, volume, open_price, close_price, price, profit,
                    commission, swap, net_profit, open_time, close_time,
                    deal_time, comment
                FROM mt5_history_deals
                WHERE {where_sql}
                ORDER BY COALESCE(NULLIF(close_time, 0), deal_time) DESC, updated_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, size, offset],
            ).fetchall()
        orders = []
        for row in rows:
            close_price = float(row["close_price"] or row["price"] or 0)
            close_time = int(row["close_time"] or row["deal_time"] or 0)
            orders.append(
                {
                    "account_login": str(row["account_login"] or ""),
                    "account_server": str(row["account_server"] or ""),
                    "order_id": str(row["order_id"] or row["deal_id"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "mt_type": str(row["mt_type"] or ""),
                    "volume": float(row["volume"] or 0),
                    "open_price": float(row["open_price"] or 0),
                    "close_price": close_price,
                    "profit": float(row["profit"] or 0),
                    "commission": float(row["commission"] or 0),
                    "swap": float(row["swap"] or 0),
                    "net_profit": float(row["net_profit"] or 0),
                    "open_time": int(row["open_time"] or 0),
                    "close_time": close_time,
                    "comment": str(row["comment"] or ""),
                },
            )
        return {
            "total": int(total_row["total"] or 0) if total_row else 0,
            "orders": orders,
        }

    def admin_ai_strategy_overview(self) -> dict[str, Any]:
        with self._connect() as connection:
            deployments = [
                self._deployment_row(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM deployments
                    WHERE config_json LIKE '%deployment_key%'
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            ]
            decision_rows = connection.execute(
                """
                SELECT d.deployment_id, d.endpoint, d.response_json
                FROM decisions d
                """
            ).fetchall()
            usage_rows = connection.execute(
                """
                SELECT
                    deployment_id,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(official_tokens), 0) AS official_tokens,
                    COALESCE(SUM(custom_tokens), 0) AS custom_tokens
                FROM ai_usage_logs
                GROUP BY deployment_id
                """
            ).fetchall()
            quota_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS quota_user_count,
                    COALESCE(SUM(monthly_quota), 0) AS monthly_quota,
                    COALESCE(SUM(extra_quota), 0) AS extra_quota,
                    COALESCE(SUM(used_tokens), 0) AS quota_used_tokens
                FROM ai_user_quotas
                """
            ).fetchone()
            history_rows = connection.execute(
                """
                SELECT deployment_id, entry, net_profit
                FROM mt5_history_deals
                """
            ).fetchall()

        by_deployment: dict[str, dict[str, Any]] = {}
        for deployment in deployments:
            by_deployment[deployment["id"]] = {
                "id": deployment["id"],
                "user_id": deployment["user_id"],
                "name": deployment["strategy_name"],
                "strategy_code": deployment["strategy_code"],
                "status": deployment["status"],
                "analysis_count": 0,
                "signal_count": 0,
                "order_count": 0,
                "official_tokens": 0,
                "custom_tokens": 0,
                "total_tokens": 0,
                "pnl": 0.0,
                "updated_at": deployment["updated_at"],
            }

        total_analysis = 0
        total_signals = 0
        total_orders = 0
        for row in decision_rows:
            deployment_id = row["deployment_id"]
            item = by_deployment.get(deployment_id)
            if item is None:
                continue
            item["analysis_count"] += 1
            total_analysis += 1
            try:
                payload = json.loads(row["response_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            action = str(payload.get("action") or "").upper()
            if action and action != "HOLD":
                item["signal_count"] += 1
                total_signals += 1
            if row["endpoint"] == "open" and action in {"BUY", "SELL"}:
                item["order_count"] += 1
                total_orders += 1

        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_official_tokens = 0
        total_custom_tokens = 0
        for row in usage_rows:
            item = by_deployment.get(row["deployment_id"])
            official_tokens = int(row["official_tokens"] or 0)
            custom_tokens = int(row["custom_tokens"] or 0)
            row_total_tokens = int(row["total_tokens"] or 0)
            total_input_tokens += int(row["input_tokens"] or 0)
            total_output_tokens += int(row["output_tokens"] or 0)
            total_tokens += row_total_tokens
            total_official_tokens += official_tokens
            total_custom_tokens += custom_tokens
            if item is not None:
                item["official_tokens"] = official_tokens
                item["custom_tokens"] = custom_tokens
                item["total_tokens"] = row_total_tokens

        total_pnl = 0.0
        for row in history_rows:
            if not _is_profit_deal_entry(str(row["entry"] or "")):
                continue
            profit = float(row["net_profit"] or 0)
            total_pnl += profit
            item = by_deployment.get(row["deployment_id"])
            if item is not None:
                item["pnl"] = round(float(item["pnl"]) + profit, 2)

        strategy_list = sorted(
            by_deployment.values(),
            key=lambda item: (int(item["analysis_count"]), str(item["updated_at"])),
            reverse=True,
        )
        return {
            "summary": {
                "user_count": len({item["user_id"] for item in by_deployment.values()}),
                "quota_user_count": int(quota_row["quota_user_count"] or 0) if quota_row else 0,
                "strategy_count": len(by_deployment),
                "running_strategy_count": len([item for item in by_deployment.values() if item["status"] == "active"]),
                "analysis_count": total_analysis,
                "signal_count": total_signals,
                "order_count": total_orders,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "official_tokens": total_official_tokens,
                "custom_tokens": total_custom_tokens,
                "monthly_quota": int(quota_row["monthly_quota"] or 0) if quota_row else 0,
                "extra_quota": int(quota_row["extra_quota"] or 0) if quota_row else 0,
                "quota_used_tokens": int(quota_row["quota_used_tokens"] or 0) if quota_row else 0,
                "pnl": round(total_pnl, 2),
            },
            "strategies": strategy_list[:50],
        }

    def list_official_ai_strategies(self, *, page: int, size: int, keyword: str = "") -> dict[str, Any]:
        params: list[Any] = []
        where = ""
        if keyword:
            like = f"%{keyword}%"
            where = "WHERE name LIKE ? OR code LIKE ? OR summary LIKE ?"
            params.extend([like, like, like])
        return self._paged_query_sql(
            count_sql=f"SELECT COUNT(*) FROM official_ai_strategies {where}",
            list_sql=f"""
                SELECT *
                FROM official_ai_strategies
                {where}
                ORDER BY sort ASC, updated_at DESC
                LIMIT ? OFFSET ?
            """,
            params=params,
            page=page,
            size=size,
            mapper=self._official_strategy_row,
        )

    def get_official_ai_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM official_ai_strategies WHERE id = ? OR code = ?",
                (strategy_id, strategy_id),
            ).fetchone()
        return self._official_strategy_row(row) if row else None

    def save_official_ai_strategy(self, payload: dict[str, Any]) -> dict[str, Any]:
        strategy_id = str(payload.get("id") or f"ofs_{uuid4().hex}")
        now = utc_now_iso()
        existing = self.get_official_ai_strategy(strategy_id)
        strategy_code = str(payload.get("code") or "").strip()
        strategy_name = str(payload.get("name") or "").strip()
        config = payload.get("default_config")
        if not isinstance(config, dict):
            config = {}
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO official_ai_strategies (
                    id, code, name, badge, version, status, summary,
                    open_logic, position_logic, open_data_type, open_kline_count,
                    position_data_type, position_kline_count, call_mode, call_value,
                    open_model_id, position_model_id, default_config_json,
                    enabled, sort, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    code = excluded.code,
                    name = excluded.name,
                    badge = excluded.badge,
                    version = excluded.version,
                    status = excluded.status,
                    summary = excluded.summary,
                    open_logic = excluded.open_logic,
                    position_logic = excluded.position_logic,
                    open_data_type = excluded.open_data_type,
                    open_kline_count = excluded.open_kline_count,
                    position_data_type = excluded.position_data_type,
                    position_kline_count = excluded.position_kline_count,
                    call_mode = excluded.call_mode,
                    call_value = excluded.call_value,
                    open_model_id = excluded.open_model_id,
                    position_model_id = excluded.position_model_id,
                    default_config_json = excluded.default_config_json,
                    enabled = excluded.enabled,
                    sort = excluded.sort,
                    updated_at = excluded.updated_at
                """,
                (
                    strategy_id,
                    strategy_code,
                    strategy_name,
                    str(payload.get("badge") or "Gainlab").strip(),
                    str(payload.get("version") or "1.0").strip(),
                    str(payload.get("status") or "active").strip(),
                    str(payload.get("summary") or ""),
                    str(payload.get("open_logic") or ""),
                    str(payload.get("position_logic") or ""),
                    str(payload.get("open_data_type") or "kline"),
                    int(payload.get("open_kline_count") or 100),
                    str(payload.get("position_data_type") or "kline"),
                    int(payload.get("position_kline_count") or 100),
                    str(payload.get("call_mode") or "bar"),
                    int(payload.get("call_value") or 1),
                    str(payload.get("open_model_id") or ""),
                    str(payload.get("position_model_id") or ""),
                    json.dumps(config, ensure_ascii=False),
                    1 if payload.get("enabled", True) else 0,
                    int(payload.get("sort") or 9999),
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
            if strategy_code and strategy_name:
                connection.execute(
                    """
                    UPDATE deployments
                    SET strategy_name = ?, updated_at = ?
                    WHERE strategy_code = ?
                    """,
                    (strategy_name, now, strategy_code),
                )
        saved = self.get_official_ai_strategy(strategy_id)
        if saved is None:
            raise RuntimeError("official_strategy_save_failed")
        return saved

    def admin_official_strategy_detail(self, strategy_id: str, *, period: str = "all") -> dict[str, Any]:
        strategy = self.get_official_ai_strategy(strategy_id)
        if strategy is None:
            raise RuntimeError("official_strategy_not_found")
        stats = self._admin_strategy_code_stats(str(strategy["code"]), period=period)
        return {
            "strategy": strategy,
            **stats,
        }

    def _admin_strategy_code_stats(self, strategy_code: str, *, period: str = "all") -> dict[str, Any]:
        start_iso, end_iso, start_ts, end_ts, bucket = _period_bounds(period)
        with self._connect() as connection:
            deployments = [
                self._deployment_row(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM deployments
                    WHERE strategy_code = ?
                    ORDER BY updated_at DESC
                    """,
                    (strategy_code,),
                ).fetchall()
            ]
            decision_rows = connection.execute(
                """
                SELECT decision.deployment_id, decision.endpoint, decision.response_json, decision.created_at
                FROM decisions decision
                JOIN deployments dep ON dep.id = decision.deployment_id
                WHERE dep.strategy_code = ?
                  AND decision.created_at >= ?
                  AND decision.created_at <= ?
                """,
                (strategy_code, start_iso, end_iso),
            ).fetchall()
            usage_rows = connection.execute(
                """
                SELECT
                    deployment_id,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(official_tokens), 0) AS official_tokens,
                    COALESCE(SUM(custom_tokens), 0) AS custom_tokens
                FROM ai_usage_logs
                WHERE strategy_code = ?
                  AND created_at >= ?
                  AND created_at <= ?
                GROUP BY deployment_id
                """,
                (strategy_code, start_iso, end_iso),
            ).fetchall()
            history_rows = connection.execute(
                """
                SELECT h.deployment_id, h.entry, h.net_profit, h.close_time, h.deal_time, h.updated_at
                FROM mt5_history_deals h
                JOIN deployments d ON d.id = h.deployment_id
                WHERE d.strategy_code = ?
                  AND COALESCE(NULLIF(h.close_time, 0), h.deal_time) >= ?
                  AND COALESCE(NULLIF(h.close_time, 0), h.deal_time) <= ?
                """,
                (strategy_code, start_ts, end_ts),
            ).fetchall()
            activity_rows = connection.execute(
                """
                SELECT deployment_id, event_type, created_at
                FROM deployment_activity_logs
                WHERE strategy_code = ?
                  AND created_at >= ?
                  AND created_at <= ?
                """,
                (strategy_code, start_iso, end_iso),
            ).fetchall()
            account_rows = connection.execute(
                """
                SELECT da.*
                FROM deployment_accounts da
                JOIN deployments dep ON dep.id = da.deployment_id
                WHERE dep.strategy_code = ?
                ORDER BY da.last_seen_at DESC
                """,
                (strategy_code,),
            ).fetchall()

        by_deployment: dict[str, dict[str, Any]] = {}
        for deployment in deployments:
            by_deployment[deployment["id"]] = {
                "id": deployment["id"],
                "user_id": deployment["user_id"],
                "name": deployment["strategy_name"],
                "status": deployment["status"],
                "account_login": "",
                "account_provider": "",
                "account_server": "",
                "account_type": "",
                "analysis_count": 0,
                "signal_count": 0,
                "order_count": 0,
                "close_order_count": 0,
                "activity_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "official_tokens": 0,
                "custom_tokens": 0,
                "total_tokens": 0,
                "pnl": 0.0,
                "updated_at": deployment["updated_at"],
                "last_active_at": "",
            }

        for row in account_rows:
            item = by_deployment.get(row["deployment_id"])
            if item is None or item["account_login"]:
                continue
            item["account_login"] = row["login"]
            item["account_provider"] = row["provider"]
            item["account_server"] = row["server"]
            item["account_type"] = "demo" if row["is_demo"] else "real"

        total_analysis = 0
        total_signals = 0
        total_orders = 0
        active_deployments: set[str] = set()
        for row in decision_rows:
            item = by_deployment.get(row["deployment_id"])
            if item is None:
                continue
            active_deployments.add(row["deployment_id"])
            item["analysis_count"] += 1
            item["activity_count"] += 1
            item["last_active_at"] = max(str(item["last_active_at"] or ""), str(row["created_at"] or ""))
            total_analysis += 1
            try:
                payload = json.loads(row["response_json"])
            except (TypeError, json.JSONDecodeError):
                payload = {}
            action = str(payload.get("action") or "").upper()
            if action and action != "HOLD":
                item["signal_count"] += 1
                total_signals += 1
            if row["endpoint"] == "open" and action in {"BUY", "SELL"}:
                item["order_count"] += 1
                total_orders += 1

        for row in activity_rows:
            item = by_deployment.get(row["deployment_id"])
            if item is None:
                continue
            active_deployments.add(row["deployment_id"])
            item["activity_count"] += 1
            item["last_active_at"] = max(str(item["last_active_at"] or ""), str(row["created_at"] or ""))

        total_input_tokens = 0
        total_output_tokens = 0
        total_tokens = 0
        total_official_tokens = 0
        total_custom_tokens = 0
        for row in usage_rows:
            item = by_deployment.get(row["deployment_id"])
            input_tokens = int(row["input_tokens"] or 0)
            output_tokens = int(row["output_tokens"] or 0)
            official_tokens = int(row["official_tokens"] or 0)
            custom_tokens = int(row["custom_tokens"] or 0)
            row_total_tokens = int(row["total_tokens"] or 0)
            total_input_tokens += input_tokens
            total_output_tokens += output_tokens
            total_tokens += row_total_tokens
            total_official_tokens += official_tokens
            total_custom_tokens += custom_tokens
            if item is not None:
                item["input_tokens"] = input_tokens
                item["output_tokens"] = output_tokens
                item["official_tokens"] = official_tokens
                item["custom_tokens"] = custom_tokens
                item["total_tokens"] = row_total_tokens

        total_pnl = 0.0
        total_close_orders = 0
        pnl_buckets: dict[str, float] = {}
        for row in history_rows:
            if not _is_profit_deal_entry(str(row["entry"] or "")):
                continue
            profit = float(row["net_profit"] or 0)
            total_pnl += profit
            total_close_orders += 1
            close_time = int(row["close_time"] or row["deal_time"] or 0)
            bucket_key = _time_bucket(close_time, bucket)
            pnl_buckets[bucket_key] = round(pnl_buckets.get(bucket_key, 0.0) + profit, 2)
            item = by_deployment.get(row["deployment_id"])
            if item is not None:
                active_deployments.add(row["deployment_id"])
                item["last_active_at"] = max(str(item["last_active_at"] or ""), str(row["updated_at"] or ""))
                item["pnl"] = round(float(item["pnl"]) + profit, 2)
                item["close_order_count"] += 1

        for item in by_deployment.values():
            if not item["last_active_at"]:
                item["last_active_at"] = item["updated_at"]

        deployments_list = sorted(
            by_deployment.values(),
            key=lambda item: (
                str(item.get("last_active_at") or item.get("updated_at") or ""),
                int(item["activity_count"]),
                int(item["analysis_count"]),
            ),
            reverse=True,
        )
        active_items = [item for key, item in by_deployment.items() if key in active_deployments]
        return {
            "summary": {
                "user_count": len({item["user_id"] for item in active_items}),
                "active_ea_count": len(active_deployments),
                "deployment_count": len(by_deployment),
                "running_deployment_count": len([item for item in by_deployment.values() if item["status"] == "active"]),
                "analysis_count": total_analysis,
                "signal_count": total_signals,
                "order_count": total_orders,
                "close_order_count": total_close_orders,
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "official_tokens": total_official_tokens,
                "custom_tokens": total_custom_tokens,
                "pnl": round(total_pnl, 2),
            },
            "chart": {
                "bucket": bucket,
                "pnl_curve": _pnl_curve_points(pnl_buckets),
            },
            "deployments": deployments_list[:100],
        }

    def activate_deployment(
        self,
        raw_key: str,
        *,
        platform: str,
        login: str,
        server: str,
    ) -> dict[str, Any] | None:
        key_hash = hash_deployment_key(raw_key)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deployments WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["mt_login"] is None:
                now = utc_now_iso()
                connection.execute(
                    """
                    UPDATE deployments
                    SET mt_platform = ?, mt_login = ?, mt_server = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (platform, login, server, now, row["id"]),
                )
                row = connection.execute(
                    "SELECT * FROM deployments WHERE id = ?",
                    (row["id"],),
                ).fetchone()
        return self._deployment_row(row)

    def account_matches(
        self,
        deployment: dict[str, Any],
        *,
        platform: str,
        login: str,
        server: str,
    ) -> bool:
        return (
            deployment.get("mt_platform") == platform
            and deployment.get("mt_login") == login
            and deployment.get("mt_server") == server
        )

    def get_decision(
        self,
        deployment_id: str,
        endpoint: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT response_json FROM decisions
                WHERE deployment_id = ? AND endpoint = ? AND request_id = ?
                """,
                (deployment_id, endpoint, request_id),
            ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def save_decision(
        self,
        deployment_id: str,
        endpoint: str,
        request_id: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO decisions (
                        id, deployment_id, endpoint, request_id,
                        response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        response["decision_id"],
                        deployment_id,
                        endpoint,
                        request_id,
                        json.dumps(response),
                        utc_now_iso(),
                    ),
                )
                return response
            except DatabaseIntegrityError:
                existing = self.get_decision(deployment_id, endpoint, request_id)
                if existing is None:
                    raise
                return existing

    def save_heartbeat(self, deployment_id: str, payload: dict[str, Any]) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO heartbeats (deployment_id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(deployment_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (deployment_id, json.dumps(payload), now),
            )

    def record_deployment_activity(
        self,
        deployment_id: str,
        *,
        strategy_code: str,
        event_type: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO deployment_activity_logs (
                    id, deployment_id, strategy_code, event_type, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    f"act_{uuid4().hex}",
                    deployment_id,
                    strategy_code,
                    event_type,
                    utc_now_iso(),
                ),
            )

    def save_deployment_account(
        self,
        deployment: dict[str, Any],
        *,
        login: str,
        platform: str = "MT5",
        provider: str = "",
        server: str = "",
    ) -> None:
        login = str(login or "").strip()
        if not login or login == "unknown":
            return
        now = utc_now_iso()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id, first_seen_at
                FROM deployment_accounts
                WHERE deployment_id = ? AND login = ? AND server = ?
                """,
                (deployment["id"], login, server),
            ).fetchone()
            account_id = existing["id"] if existing else f"dacc_{uuid4().hex}"
            first_seen_at = existing["first_seen_at"] if existing else now
            connection.execute(
                """
                INSERT INTO deployment_accounts (
                    id, deployment_id, user_id, login, platform, provider, server,
                    is_demo, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(deployment_id, login, server) DO UPDATE SET
                    user_id = excluded.user_id,
                    platform = excluded.platform,
                    provider = excluded.provider,
                    is_demo = excluded.is_demo,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    account_id,
                    deployment["id"],
                    deployment.get("user_id", ""),
                    login,
                    platform or "MT5",
                    provider,
                    server,
                    1 if _is_demo_server(server) else 0,
                    first_seen_at,
                    now,
                ),
            )

    def save_execution_report(
        self,
        deployment_id: str,
        payload: dict[str, Any],
    ) -> str:
        report_id = f"report_{uuid4().hex}"
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO execution_reports (
                    id, deployment_id, decision_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    deployment_id,
                    payload["decision_id"],
                    json.dumps(payload),
                    utc_now_iso(),
                ),
            )
        return report_id

    def sync_mt5_history_deals(
        self,
        deployment_id: str,
        *,
        account_login: str,
        account_server: str,
        orders: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        inserted_count = 0
        updated_count = 0
        profit_deals_count = 0
        net_profit_total = 0.0

        with self._connect() as connection:
            for order in orders:
                order_id = str(order.get("order_id") or order.get("deal_id") or "").strip()
                if not order_id:
                    continue
                entry = "out"
                net_profit = _deal_net_profit(order)
                if _is_profit_deal_entry(entry):
                    profit_deals_count += 1
                    net_profit_total += net_profit

                legacy_server_backfill = False
                existing = connection.execute(
                    """
                    SELECT id
                    FROM mt5_history_deals
                    WHERE account_login = ? AND account_server = ? AND deal_id = ?
                    """,
                    (account_login, account_server, order_id),
                ).fetchone()
                if existing is None and account_server:
                    existing = connection.execute(
                        """
                        SELECT id
                        FROM mt5_history_deals
                        WHERE deployment_id = ?
                          AND account_login = ?
                          AND account_server = ''
                          AND deal_id = ?
                        """,
                        (deployment_id, account_login, order_id),
                    ).fetchone()
                    legacy_server_backfill = existing is not None
                if legacy_server_backfill:
                    connection.execute(
                        """
                        UPDATE mt5_history_deals
                        SET account_server = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (account_server, now, existing["id"]),
                    )
                deal_row_id = existing["id"] if existing else f"deal_{uuid4().hex}"
                connection.execute(
                    """
                        INSERT INTO mt5_history_deals (
                        id, deployment_id, account_login, account_server, deal_id,
                        order_id, position_id, symbol, mt_type, entry, volume, price,
                        open_price, close_price, profit, commission, swap, net_profit,
                        deal_time, open_time, close_time, comment,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_login, account_server, deal_id) DO UPDATE SET
                        deployment_id = excluded.deployment_id,
                        order_id = excluded.order_id,
                        position_id = excluded.position_id,
                        symbol = excluded.symbol,
                        mt_type = excluded.mt_type,
                        entry = excluded.entry,
                        volume = excluded.volume,
                        price = excluded.price,
                        open_price = excluded.open_price,
                        close_price = excluded.close_price,
                        profit = excluded.profit,
                        commission = excluded.commission,
                        swap = excluded.swap,
                        net_profit = excluded.net_profit,
                        deal_time = excluded.deal_time,
                        open_time = excluded.open_time,
                        close_time = excluded.close_time,
                        comment = excluded.comment,
                        updated_at = excluded.updated_at
                    """,
                    (
                        deal_row_id,
                        deployment_id,
                        account_login,
                        account_server,
                        order_id,
                        order_id,
                        order_id,
                        str(order.get("symbol") or ""),
                        str(order.get("mt_type") or ""),
                        entry,
                        float(order.get("volume") or 0),
                        float(order.get("close_price") or 0),
                        float(order.get("open_price") or 0),
                        float(order.get("close_price") or 0),
                        float(order.get("profit") or 0),
                        float(order.get("commission") or 0),
                        float(order.get("swap") or 0),
                        net_profit,
                        int(order.get("close_time") or 0),
                        int(order.get("open_time") or 0),
                        int(order.get("close_time") or 0),
                        str(order.get("comment") or ""),
                        now,
                        now,
                    ),
                )
                if existing:
                    updated_count += 1
                else:
                    inserted_count += 1

        return {
            "received_count": len(orders),
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "profit_orders_count": profit_deals_count,
            "profit_deals_count": profit_deals_count,
            "net_profit": round(net_profit_total, 2),
        }

    def list_ai_providers(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
        official_only: bool = False,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = []
        if official_only:
            conditions.append("api_key <> ''")
        if keyword:
            conditions.append("(name LIKE ? OR provider_type LIKE ? OR base_url LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return self._paged_query(
            table="ai_providers",
            where=where,
            params=params,
            page=page,
            size=size,
            order_by="sort ASC, updated_at DESC",
            mapper=self._public_provider_row,
        )

    def save_ai_provider(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        provider_name = str(payload.get("name") or "").strip()
        provider_id = str(payload.get("id") or "")
        if not provider_id and provider_name:
            with self._connect() as connection:
                row = connection.execute("SELECT * FROM ai_providers WHERE name = ?", (provider_name,)).fetchone()
            if row:
                provider_id = str(row["id"])
        provider_id = provider_id or f"aip_{uuid4().hex}"
        existing = self.get_ai_provider(provider_id)
        api_key = str(payload.get("api_key") or "")
        if not api_key and existing:
            api_key = existing["api_key"]
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url and existing:
            base_url = str(existing.get("base_url") or "")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_providers (
                    id, name, provider_type, base_url, api_key, enabled, sort,
                    remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    provider_type = excluded.provider_type,
                    base_url = excluded.base_url,
                    api_key = excluded.api_key,
                    enabled = excluded.enabled,
                    sort = excluded.sort,
                    remark = excluded.remark,
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    provider_name,
                    str(payload.get("provider_type") or "openai_compatible").strip(),
                    base_url,
                    api_key.strip(),
                    1 if payload.get("enabled", True) else 0,
                    int(payload.get("sort") or 9999),
                    str(payload.get("remark") or ""),
                    now,
                    now,
                ),
            )
        provider = self.get_ai_provider(provider_id)
        if provider is None:
            raise RuntimeError("ai_provider_save_failed")
        return self._mask_provider(provider)

    def get_ai_provider(self, provider_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ai_providers WHERE id = ?", (provider_id,)).fetchone()
        return self._provider_row(row) if row else None

    def get_default_ai_model(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    m.*,
                    p.name AS provider_name,
                    p.provider_type AS provider_type,
                    COALESCE(NULLIF(m.base_url, ''), p.base_url) AS provider_base_url,
                    p.api_key AS provider_api_key
                FROM ai_models m
                JOIN ai_providers p ON p.id = m.provider_id
                WHERE m.enabled = 1 AND p.enabled = 1
                ORDER BY m.is_default DESC, m.sort ASC, m.updated_at DESC
                LIMIT 1
                """,
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["is_default"] = bool(data["is_default"])
        return data

    def delete_ai_provider(self, provider_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM ai_models WHERE provider_id = ?", (provider_id,))
            connection.execute("DELETE FROM ai_providers WHERE id = ?", (provider_id,))

    def clear_ai_provider_key(self, provider_id: str) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                "UPDATE ai_providers SET api_key = '', updated_at = ? WHERE id = ?",
                (now, provider_id),
            )

    def list_ai_models(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
        provider_id: str = "",
    ) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if keyword:
            clauses.append("(m.name LIKE ? OR m.display_name LIKE ? OR p.name LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like])
        if provider_id:
            clauses.append("m.provider_id = ?")
            params.append(provider_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._paged_query_sql(
            count_sql=f"SELECT COUNT(*) FROM ai_models m LEFT JOIN ai_providers p ON p.id = m.provider_id {where}",
            list_sql=f"""
                SELECT
                    m.*,
                    p.name AS provider_name,
                    p.provider_type AS provider_type,
                    p.api_key AS provider_api_key
                FROM ai_models m
                LEFT JOIN ai_providers p ON p.id = m.provider_id
                {where}
                ORDER BY m.sort ASC, m.updated_at DESC
                LIMIT ? OFFSET ?
            """,
            params=params,
            page=page,
            size=size,
            mapper=self._model_row,
        )

    def list_public_ai_model_options(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.id,
                    m.provider_id,
                    m.name,
                    m.display_name,
                    COALESCE(NULLIF(m.base_url, ''), p.base_url) AS base_url,
                    m.is_default,
                    m.sort,
                    p.name AS provider_name,
                    p.provider_type AS provider_type,
                    CASE WHEN p.api_key <> '' THEN 1 ELSE 0 END AS official_available
                FROM ai_models m
                JOIN ai_providers p ON p.id = m.provider_id
                WHERE m.enabled = 1 AND p.enabled = 1
                ORDER BY p.sort ASC, m.sort ASC, m.updated_at DESC
                """,
            ).fetchall()
        return {
            "list": [
                {
                    "id": str(row["id"] or ""),
                    "provider_id": str(row["provider_id"] or ""),
                    "provider_name": str(row["provider_name"] or ""),
                    "provider_type": str(row["provider_type"] or ""),
                    "model": str(row["name"] or ""),
                    "display_name": str(row["display_name"] or row["name"] or ""),
                    "base_url": str(row["base_url"] or ""),
                    "is_default": bool(row["is_default"]),
                    "official_available": bool(row["official_available"]),
                }
                for row in rows
            ],
        }

    def save_ai_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        model_id = str(payload.get("id") or f"aim_{uuid4().hex}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_models (
                    id, provider_id, name, display_name, base_url, context_window,
                    input_token_rate, output_token_rate, billing_multiplier,
                    is_default, enabled, sort, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    name = excluded.name,
                    display_name = excluded.display_name,
                    base_url = excluded.base_url,
                    context_window = excluded.context_window,
                    input_token_rate = excluded.input_token_rate,
                    output_token_rate = excluded.output_token_rate,
                    billing_multiplier = excluded.billing_multiplier,
                    is_default = excluded.is_default,
                    enabled = excluded.enabled,
                    sort = excluded.sort,
                    remark = excluded.remark,
                    updated_at = excluded.updated_at
                """,
                (
                    model_id,
                    str(payload.get("provider_id") or ""),
                    str(payload.get("name") or "").strip(),
                    str(payload.get("display_name") or "").strip(),
                    str(payload.get("base_url") or "").strip(),
                    int(payload.get("context_window") or 0),
                    float(payload.get("input_token_rate") or 0),
                    float(payload.get("output_token_rate") or 0),
                    float(payload.get("billing_multiplier") or 1),
                    1 if payload.get("is_default", False) else 0,
                    1 if payload.get("enabled", True) else 0,
                    int(payload.get("sort") or 9999),
                    str(payload.get("remark") or ""),
                    now,
                    now,
                ),
            )
        model = self.get_ai_model(model_id)
        if model is None:
            raise RuntimeError("ai_model_save_failed")
        return model

    def get_ai_model(self, model_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    m.*,
                    p.name AS provider_name,
                    p.provider_type AS provider_type,
                    COALESCE(NULLIF(m.base_url, ''), p.base_url) AS provider_base_url
                FROM ai_models m
                LEFT JOIN ai_providers p ON p.id = m.provider_id
                WHERE m.id = ?
                """,
                (model_id,),
            ).fetchone()
        return self._model_row(row) if row else None

    def delete_ai_model(self, model_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM ai_models WHERE id = ?", (model_id,))

    def list_ai_user_quotas(self, *, page: int, size: int, keyword: str = "") -> dict[str, Any]:
        where = ""
        params: list[Any] = []
        if keyword:
            where = "WHERE user_id LIKE ?"
            params.append(f"%{keyword}%")
        return self._paged_query(
            table="ai_user_quotas",
            where=where,
            params=params,
            page=page,
            size=size,
            order_by="updated_at DESC",
            mapper=self._quota_row,
        )

    def save_ai_user_quota(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        user_id = str(payload.get("user_id") or "").strip()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_user_quotas (
                    user_id, monthly_quota, extra_quota, used_tokens,
                    reset_at, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    monthly_quota = excluded.monthly_quota,
                    extra_quota = excluded.extra_quota,
                    used_tokens = excluded.used_tokens,
                    reset_at = excluded.reset_at,
                    remark = excluded.remark,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    int(payload.get("monthly_quota") or 0),
                    int(payload.get("extra_quota") or 0),
                    int(payload.get("used_tokens") or 0),
                    str(payload.get("reset_at") or ""),
                    str(payload.get("remark") or ""),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ai_user_quotas WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._quota_row(row)

    def list_ai_usage_logs(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if keyword:
            clauses.append("(deployment_id LIKE ? OR strategy_code LIKE ? OR endpoint LIKE ? OR error_message LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._paged_query(
            table="ai_usage_logs",
            where=where,
            params=params,
            page=page,
            size=size,
            order_by="created_at DESC",
            mapper=self._usage_row,
        )

    def save_ai_usage_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        log_id = str(payload.get("id") or f"ailog_{uuid4().hex}")
        input_tokens = int(payload.get("input_tokens") or 0)
        output_tokens = int(payload.get("output_tokens") or 0)
        total_tokens = int(payload.get("total_tokens") or (input_tokens + output_tokens))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_usage_logs (
                    id, user_id, deployment_id, strategy_code, endpoint,
                    provider_id, model_id, input_tokens, output_tokens, total_tokens,
                    official_tokens, custom_tokens, success, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    str(payload.get("user_id") or ""),
                    str(payload.get("deployment_id") or ""),
                    str(payload.get("strategy_code") or ""),
                    str(payload.get("endpoint") or ""),
                    str(payload.get("provider_id") or ""),
                    str(payload.get("model_id") or ""),
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    int(payload.get("official_tokens") or total_tokens),
                    int(payload.get("custom_tokens") or 0),
                    1 if payload.get("success", True) else 0,
                    str(payload.get("error_message") or ""),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM ai_usage_logs WHERE id = ?", (log_id,)).fetchone()
        return self._usage_row(row)

    def _paged_query(
        self,
        *,
        table: str,
        where: str,
        params: list[Any],
        page: int,
        size: int,
        order_by: str,
        mapper: Any,
    ) -> dict[str, Any]:
        return self._paged_query_sql(
            count_sql=f"SELECT COUNT(*) FROM {table} {where}",
            list_sql=f"SELECT * FROM {table} {where} ORDER BY {order_by} LIMIT ? OFFSET ?",
            params=params,
            page=page,
            size=size,
            mapper=mapper,
        )

    def _paged_query_sql(
        self,
        *,
        count_sql: str,
        list_sql: str,
        params: list[Any],
        page: int,
        size: int,
        mapper: Any,
    ) -> dict[str, Any]:
        page = max(1, int(page or 1))
        size = max(1, min(100, int(size or 20)))
        offset = (page - 1) * size
        with self._connect() as connection:
            total = int(connection.execute(count_sql, params).fetchone()[0])
            rows = connection.execute(list_sql, [*params, size, offset]).fetchall()
        return {
            "total": total,
            "list": [mapper(row) for row in rows],
        }

    @staticmethod
    def _provider_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return data

    @classmethod
    def _public_provider_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        return cls._mask_provider(cls._provider_row(row))

    @staticmethod
    def _mask_provider(data: dict[str, Any]) -> dict[str, Any]:
        public = dict(data)
        api_key = str(public.get("api_key") or "")
        public["api_key_masked"] = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("****" if api_key else "")
        public.pop("api_key", None)
        return public

    @staticmethod
    def _model_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["is_default"] = bool(data["is_default"])
        api_key = str(data.get("provider_api_key") or "")
        data["provider_api_key_masked"] = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("****" if api_key else "")
        data.pop("provider_api_key", None)
        return data

    @staticmethod
    def _quota_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["available_tokens"] = int(data["monthly_quota"]) + int(data["extra_quota"]) - int(data["used_tokens"])
        return data

    @staticmethod
    def _usage_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["success"] = bool(data["success"])
        return data

    @staticmethod
    def _official_strategy_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        try:
            data["default_config"] = json.loads(data.pop("default_config_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            data["default_config"] = {}
        return data

    @staticmethod
    def _deployment_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["config"] = json.loads(data.pop("config_json"))
        return data


class MySQLStore(SqliteStore):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
    ) -> None:
        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password

    def _connect(self) -> MySqlConnection:
        return MySqlConnection(
            pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset="utf8mb4",
                autocommit=False,
                connect_timeout=10,
                read_timeout=30,
                write_timeout=30,
            ),
        )

    def initialize(self) -> None:
        required_tables = {
            "deployments",
            "decisions",
            "heartbeats",
            "execution_reports",
            "mt5_history_deals",
            "ai_providers",
            "ai_models",
            "ai_user_quotas",
            "ai_usage_logs",
            "official_ai_strategies",
            "deployment_activity_logs",
            "deployment_accounts",
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                """
            ).fetchall()
        existing = {str(row["table_name"]) for row in rows}
        missing = sorted(required_tables - existing)
        if missing:
            raise RuntimeError(f"mysql_schema_missing_tables:{','.join(missing)}")

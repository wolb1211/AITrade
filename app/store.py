from __future__ import annotations

import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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


def normalized_utc_iso(value: str) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def decimal_string(value: Any, default: str = "0") -> str:
    try:
        amount = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, ValueError):
        amount = Decimal(default)
    return format(amount, "f")


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


def _bucket_timestamp(timestamp: int, bucket_type: str) -> int:
    """Return the UTC timestamp for an Asia/Shanghai hour/day boundary."""
    value = datetime.fromtimestamp(int(timestamp), LOCAL_TIMEZONE)
    if bucket_type == "hour":
        value = value.replace(minute=0, second=0, microsecond=0)
    else:
        value = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(value.timestamp())


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
            self._migrate_sqlite_user_id(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE,
                    password_hash TEXT,
                    nickname TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending_activation',
                    vip_level INTEGER NOT NULL DEFAULT 0,
                    vip_expires_at TEXT NOT NULL DEFAULT '',
                    max_strategy_keys INTEGER NOT NULL DEFAULT 10,
                    agent_level INTEGER NOT NULL DEFAULT 0,
                    invite_code TEXT UNIQUE,
                    referrer_user_id INTEGER,
                    referred_at TEXT NOT NULL DEFAULT '',
                    ai_balance NUMERIC NOT NULL DEFAULT 0,
                    email_verified_at TEXT NOT NULL DEFAULT '',
                    last_login_at TEXT NOT NULL DEFAULT '',
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL DEFAULT '',
                    remark TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_balance_ledger (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    entry_type TEXT NOT NULL,
                    amount NUMERIC NOT NULL,
                    balance_before NUMERIC NOT NULL,
                    balance_after NUMERIC NOT NULL,
                    operator_id TEXT NOT NULL DEFAULT '',
                    reference_id TEXT UNIQUE,
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_balance_ledger_user_time
                    ON ai_balance_ledger(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_ai_balance_ledger_type_time
                    ON ai_balance_ledger(entry_type, created_at);

                CREATE TABLE IF NOT EXISTS email_verification_codes (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_verification_email_purpose
                    ON email_verification_codes(email, purpose, created_at);

                CREATE TABLE IF NOT EXISTS user_sessions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE INDEX IF NOT EXISTS idx_user_sessions_user_id
                    ON user_sessions(user_id, created_at);

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
                    account_login TEXT NOT NULL DEFAULT '',
                    account_server TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    timeframe TEXT NOT NULL DEFAULT '',
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
                    open_price REAL NOT NULL DEFAULT 0,
                    close_price REAL NOT NULL DEFAULT 0,
                    open_time INTEGER NOT NULL DEFAULT 0,
                    close_time INTEGER NOT NULL DEFAULT 0,
                    deal_time INTEGER NOT NULL DEFAULT 0,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_login, account_server, deal_id),
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );

                CREATE INDEX IF NOT EXISTS idx_mt5_history_deployment_close
                    ON mt5_history_deals(deployment_id, close_time);
                CREATE INDEX IF NOT EXISTS idx_mt5_history_deployment_symbol_close
                    ON mt5_history_deals(deployment_id, symbol, close_time);

                CREATE TABLE IF NOT EXISTS mt_order_time_summaries (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    account_login TEXT NOT NULL DEFAULT '',
                    account_server TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    bucket_type TEXT NOT NULL,
                    bucket_start INTEGER NOT NULL,
                    order_count INTEGER NOT NULL DEFAULT 0,
                    win_count INTEGER NOT NULL DEFAULT 0,
                    loss_count INTEGER NOT NULL DEFAULT 0,
                    total_volume REAL NOT NULL DEFAULT 0,
                    gross_profit REAL NOT NULL DEFAULT 0,
                    gross_loss REAL NOT NULL DEFAULT 0,
                    commission REAL NOT NULL DEFAULT 0,
                    swap REAL NOT NULL DEFAULT 0,
                    net_profit REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(deployment_id, account_login, account_server, symbol, bucket_type, bucket_start),
                    FOREIGN KEY(deployment_id) REFERENCES deployments(id)
                );

                CREATE INDEX IF NOT EXISTS idx_mt_order_summary_user_bucket
                    ON mt_order_time_summaries(user_id, bucket_type, bucket_start);
                CREATE INDEX IF NOT EXISTS idx_mt_order_summary_deployment_bucket
                    ON mt_order_time_summaries(deployment_id, bucket_type, bucket_start);

                CREATE TABLE IF NOT EXISTS mt_order_archived_deals (
                    account_login TEXT NOT NULL,
                    account_server TEXT NOT NULL DEFAULT '',
                    deal_id TEXT NOT NULL,
                    deployment_id TEXT NOT NULL,
                    close_time INTEGER NOT NULL DEFAULT 0,
                    archived_at TEXT NOT NULL,
                    PRIMARY KEY(account_login, account_server, deal_id)
                );

                CREATE INDEX IF NOT EXISTS idx_mt_order_archive_deployment
                    ON mt_order_archived_deals(deployment_id, close_time);

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

                CREATE TABLE IF NOT EXISTS ai_templates (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    request_type TEXT NOT NULL DEFAULT 'openai_compatible',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_endpoints (
                    id TEXT PRIMARY KEY,
                    owner_type TEXT NOT NULL DEFAULT 'gl',
                    user_id TEXT NOT NULL DEFAULT '',
                    template_code TEXT NOT NULL DEFAULT 'openai_compatible',
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key TEXT NOT NULL DEFAULT '',
                    strict_json INTEGER NOT NULL DEFAULT 1,
                    context_window INTEGER NOT NULL DEFAULT 0,
                    input_token_rate REAL NOT NULL DEFAULT 1,
                    output_token_rate REAL NOT NULL DEFAULT 1,
                    billing_multiplier REAL NOT NULL DEFAULT 1,
                    input_price_per_million NUMERIC NOT NULL DEFAULT 0,
                    output_price_per_million NUMERIC NOT NULL DEFAULT 0,
                    supports_vision INTEGER NOT NULL DEFAULT 0,
                    vision_test_status TEXT NOT NULL DEFAULT 'untested',
                    vision_tested_at TEXT NOT NULL DEFAULT '',
                    vision_test_error TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    selectable_by_user INTEGER NOT NULL DEFAULT 0,
                    sort INTEGER NOT NULL DEFAULT 9999,
                    remark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                    account_login TEXT NOT NULL DEFAULT '',
                    account_server TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL DEFAULT '',
                    timeframe TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    official_tokens INTEGER NOT NULL DEFAULT 0,
                    custom_tokens INTEGER NOT NULL DEFAULT 0,
                    billing_source TEXT NOT NULL DEFAULT '',
                    input_price_snapshot NUMERIC NOT NULL DEFAULT 0,
                    output_price_snapshot NUMERIC NOT NULL DEFAULT 0,
                    charged_amount NUMERIC NOT NULL DEFAULT 0,
                    balance_after NUMERIC,
                    success INTEGER NOT NULL DEFAULT 1,
                    provider_called INTEGER NOT NULL DEFAULT 1,
                    response_source TEXT NOT NULL DEFAULT 'provider',
                    cache_id TEXT,
                    error_message TEXT NOT NULL DEFAULT '',
                    request_snapshot TEXT NOT NULL DEFAULT '',
                    response_preview TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ai_usage_user_time
                    ON ai_usage_logs(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_ai_usage_user_model_time
                    ON ai_usage_logs(user_id, model_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_ai_usage_user_deployment_time
                    ON ai_usage_logs(user_id, deployment_id, created_at);

                CREATE TABLE IF NOT EXISTS ai_usage_monthly_summaries (
                    user_id TEXT NOT NULL,
                    month_key TEXT NOT NULL,
                    model_id TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    deployment_id TEXT NOT NULL DEFAULT '',
                    strategy_code TEXT NOT NULL DEFAULT '',
                    billing_source TEXT NOT NULL DEFAULT '',
                    calls INTEGER NOT NULL DEFAULT 0,
                    success_calls INTEGER NOT NULL DEFAULT 0,
                    provider_calls INTEGER NOT NULL DEFAULT 0,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    provider_input_tokens INTEGER NOT NULL DEFAULT 0,
                    provider_output_tokens INTEGER NOT NULL DEFAULT 0,
                    official_tokens INTEGER NOT NULL DEFAULT 0,
                    custom_tokens INTEGER NOT NULL DEFAULT 0,
                    charged_amount NUMERIC NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(user_id, month_key, model_id, deployment_id, billing_source)
                );

                CREATE INDEX IF NOT EXISTS idx_ai_usage_monthly_user_month
                    ON ai_usage_monthly_summaries(user_id, month_key);

                CREATE TABLE IF NOT EXISTS ai_response_cache (
                    id TEXT PRIMARY KEY,
                    cache_key TEXT NOT NULL UNIQUE,
                    endpoint TEXT NOT NULL DEFAULT '',
                    provider_id TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT '',
                    response_json TEXT NOT NULL,
                    response_preview TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL,
                    last_hit_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ai_response_cache_expires
                    ON ai_response_cache(expires_at);

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
                    open_ai_endpoint_id TEXT NOT NULL DEFAULT '',
                    position_ai_endpoint_id TEXT NOT NULL DEFAULT '',
                    default_config_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort INTEGER NOT NULL DEFAULT 9999,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ea_downloads (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    oss_url TEXT NOT NULL,
                    file_name TEXT NOT NULL DEFAULT '',
                    file_size INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort INTEGER NOT NULL DEFAULT 9999,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS guide_articles (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    content_json TEXT NOT NULL DEFAULT '[]',
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
            self._ensure_decision_columns(connection)
            self._ensure_ai_usage_columns(connection)
            self._ensure_ai_usage_monthly_columns(connection)
            self._ensure_ai_model_columns(connection)
            self._ensure_ai_endpoint_tables(connection)
            self._ensure_official_strategy_columns(connection)
            self._ensure_official_strategy_seed(connection)
            self._ensure_user_columns(connection)
        self._backfill_ai_usage_monthly_summaries()
        self._backfill_ai_cache_stats()
        self._backfill_order_time_summaries()
        self._ensure_existing_users()

    def _migrate_sqlite_user_id(self, connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if not table:
            return
        columns = connection.execute("PRAGMA table_info(users)").fetchall()
        id_column = next((row for row in columns if row["name"] == "id"), None)
        if id_column and str(id_column["type"] or "").upper() == "INTEGER":
            return
        connection.executescript(
            """
            ALTER TABLE users RENAME TO users_legacy;
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                password_hash TEXT,
                nickname TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending_activation',
                vip_level INTEGER NOT NULL DEFAULT 0,
                vip_expires_at TEXT NOT NULL DEFAULT '',
                max_strategy_keys INTEGER NOT NULL DEFAULT 10,
                agent_level INTEGER NOT NULL DEFAULT 0,
                invite_code TEXT UNIQUE,
                referrer_user_id INTEGER,
                referred_at TEXT NOT NULL DEFAULT '',
                ai_balance NUMERIC NOT NULL DEFAULT 0,
                email_verified_at TEXT NOT NULL DEFAULT '',
                last_login_at TEXT NOT NULL DEFAULT '',
                remark TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO users (
                id, email, password_hash, nickname, status, vip_level,
                vip_expires_at, max_strategy_keys, agent_level, invite_code,
                referrer_user_id, referred_at, email_verified_at,
                last_login_at, remark, created_at, updated_at
            )
            SELECT CAST(id AS INTEGER), email, password_hash, nickname, status, vip_level,
                   COALESCE(vip_expires_at, ''), COALESCE(max_strategy_keys, 10),
                   0, NULL, NULL, '', email_verified_at, last_login_at, remark, created_at, updated_at
            FROM users_legacy
            WHERE id <> '' AND id NOT GLOB '*[^0-9]*';
            DROP TABLE users_legacy;
            """
        )

    def _ensure_user_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        migrations = {
            "vip_expires_at": "ALTER TABLE users ADD COLUMN vip_expires_at TEXT NOT NULL DEFAULT ''",
            "max_strategy_keys": "ALTER TABLE users ADD COLUMN max_strategy_keys INTEGER NOT NULL DEFAULT 10",
            "ai_balance": "ALTER TABLE users ADD COLUMN ai_balance NUMERIC NOT NULL DEFAULT 0",
            "agent_level": "ALTER TABLE users ADD COLUMN agent_level INTEGER NOT NULL DEFAULT 0",
            "invite_code": "ALTER TABLE users ADD COLUMN invite_code TEXT",
            "referrer_user_id": "ALTER TABLE users ADD COLUMN referrer_user_id INTEGER",
            "referred_at": "ALTER TABLE users ADD COLUMN referred_at TEXT NOT NULL DEFAULT ''",
            "remark": "ALTER TABLE users ADD COLUMN remark TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uk_users_invite_code ON users(invite_code)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_user_id, created_at)")
        now = utc_now_iso()
        connection.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, remark, updated_at) VALUES (?, ?, ?, ?)",
            ("ai_credit_limit", "10.000000", "官方 AI 默认信用额度（元）", now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, remark, updated_at) VALUES (?, ?, ?, ?)",
            ("ai_low_balance_threshold", "10.000000", "客户端低余额提醒阈值（元）", now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, remark, updated_at) VALUES (?, ?, ?, ?)",
            ("ai_usage_detail_retention_days", "60", "AI 调用明细保存天数", now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, remark, updated_at) VALUES (?, ?, ?, ?)",
            ("ai_cache_enabled", "1", "AI 相同请求缓存开关", now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, remark, updated_at) VALUES (?, ?, ?, ?)",
            ("ai_cache_ttl_seconds", "120", "AI 相同请求缓存秒数", now),
        )

        connection.execute(
            "INSERT OR IGNORE INTO system_settings (setting_key, setting_value, remark, updated_at) VALUES (?, ?, ?, ?)",
            ("order_detail_retention_days", "365", "订单明细保存天数", now),
        )

    def _backfill_ai_usage_monthly_summaries(self) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            marker = connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = ?",
                ("ai_usage_monthly_summary_backfilled",),
            ).fetchone()
            if marker is not None:
                return
            connection.execute(
                """
                INSERT INTO ai_usage_monthly_summaries (
                    user_id, month_key, model_id, provider_id, deployment_id,
                    strategy_code, billing_source, calls, success_calls,
                    input_tokens, output_tokens, official_tokens, custom_tokens,
                    charged_amount, updated_at
                )
                SELECT user_id, SUBSTR(created_at, 1, 7), model_id, MAX(provider_id),
                       deployment_id, MAX(strategy_code), billing_source,
                       COUNT(*), COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(official_tokens), 0), COALESCE(SUM(custom_tokens), 0),
                       COALESCE(SUM(charged_amount), 0), ?
                FROM ai_usage_logs
                GROUP BY user_id, SUBSTR(created_at, 1, 7), model_id,
                         deployment_id, billing_source
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                ("ai_usage_monthly_summary_backfilled", "1", "AI 月度汇总历史数据已初始化", now),
            )

    def _upsert_order_summary_row(
        self,
        connection: sqlite3.Connection | MySqlConnection,
        row: dict[str, Any],
        *,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO mt_order_time_summaries (
                id, user_id, deployment_id, account_login, account_server,
                symbol, bucket_type, bucket_start, order_count, win_count,
                loss_count, total_volume, gross_profit, gross_loss, commission,
                swap, net_profit, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deployment_id, account_login, account_server, symbol, bucket_type, bucket_start)
            DO UPDATE SET
                user_id = excluded.user_id,
                order_count = excluded.order_count,
                win_count = excluded.win_count,
                loss_count = excluded.loss_count,
                total_volume = excluded.total_volume,
                gross_profit = excluded.gross_profit,
                gross_loss = excluded.gross_loss,
                commission = excluded.commission,
                swap = excluded.swap,
                net_profit = excluded.net_profit,
                updated_at = excluded.updated_at
            """,
            (
                f"ordersum_{uuid4().hex}", row["user_id"], row["deployment_id"],
                row["account_login"], row["account_server"], row["symbol"],
                row["bucket_type"], row["bucket_start"], row["order_count"],
                row["win_count"], row["loss_count"], row["total_volume"],
                row["gross_profit"], row["gross_loss"], row["commission"],
                row["swap"], row["net_profit"], now, now,
            ),
        )

    def _refresh_order_summary_bucket(
        self,
        connection: sqlite3.Connection | MySqlConnection,
        *,
        deployment_id: str,
        bucket_type: str,
        bucket_start: int,
        now: str,
    ) -> None:
        seconds = 3600 if bucket_type == "hour" else 86400
        connection.execute(
            """
            DELETE FROM mt_order_time_summaries
            WHERE deployment_id = ? AND bucket_type = ? AND bucket_start = ?
            """,
            (deployment_id, bucket_type, bucket_start),
        )
        rows = connection.execute(
            """
            SELECT d.user_id, h.deployment_id, h.account_login, h.account_server,
                   h.symbol, COUNT(*) order_count,
                   COALESCE(SUM(CASE WHEN h.net_profit > 0 THEN 1 ELSE 0 END), 0) win_count,
                   COALESCE(SUM(CASE WHEN h.net_profit < 0 THEN 1 ELSE 0 END), 0) loss_count,
                   COALESCE(SUM(h.volume), 0) total_volume,
                   COALESCE(SUM(CASE WHEN h.net_profit > 0 THEN h.net_profit ELSE 0 END), 0) gross_profit,
                   COALESCE(SUM(CASE WHEN h.net_profit < 0 THEN h.net_profit ELSE 0 END), 0) gross_loss,
                   COALESCE(SUM(h.commission), 0) commission,
                   COALESCE(SUM(h.swap), 0) swap,
                   COALESCE(SUM(h.net_profit), 0) net_profit
            FROM mt5_history_deals h
            JOIN deployments d ON d.id = h.deployment_id
            WHERE h.deployment_id = ?
              AND LOWER(h.entry) IN ('out', 'out_by', 'inout')
              AND COALESCE(NULLIF(h.close_time, 0), h.deal_time) >= ?
              AND COALESCE(NULLIF(h.close_time, 0), h.deal_time) < ?
            GROUP BY d.user_id, h.deployment_id, h.account_login, h.account_server, h.symbol
            """,
            (deployment_id, bucket_start, bucket_start + seconds),
        ).fetchall()
        for source in rows:
            self._upsert_order_summary_row(connection, {
                **dict(source),
                "bucket_type": bucket_type,
                "bucket_start": bucket_start,
            }, now=now)

    def _backfill_order_time_summaries(self) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            marker = connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = ?",
                ("order_time_summaries_backfilled",),
            ).fetchone()
            if marker is not None:
                return
            buckets: set[tuple[str, str, int]] = set()
            time_expr = "COALESCE(NULLIF(close_time, 0), deal_time)"
            for bucket_type, seconds in (("hour", 3600), ("day", 86400)):
                if isinstance(connection, MySqlConnection):
                    bucket_expr = f"FLOOR(({time_expr} + 28800) / {seconds}) * {seconds} - 28800"
                else:
                    bucket_expr = f"CAST(({time_expr} + 28800) / {seconds} AS INTEGER) * {seconds} - 28800"
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT deployment_id, {bucket_expr} bucket_start
                    FROM mt5_history_deals
                    WHERE LOWER(entry) IN ('out', 'out_by', 'inout')
                      AND {time_expr} > 0
                    """
                ).fetchall()
                buckets.update(
                    (str(row["deployment_id"]), bucket_type, int(row["bucket_start"]))
                    for row in rows
                )
            for deployment_id, bucket_type, bucket_start in buckets:
                self._refresh_order_summary_bucket(
                    connection,
                    deployment_id=deployment_id,
                    bucket_type=bucket_type,
                    bucket_start=bucket_start,
                    now=now,
                )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    remark = excluded.remark,
                    updated_at = excluded.updated_at
                """,
                ("order_time_summaries_backfilled", "1", "订单小时和每日汇总已初始化", now),
            )

    def _ensure_existing_users(self) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT user_id FROM deployments WHERE user_id <> ''"
            ).fetchall()
            for row in rows:
                raw_user_id = str(row["user_id"] or "").strip()
                if not raw_user_id.isdigit():
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO users (
                        id, email, password_hash, nickname, status, vip_level,
                        vip_expires_at, max_strategy_keys, email_verified_at,
                        last_login_at, remark, created_at, updated_at
                    ) VALUES (?, NULL, NULL, '', 'pending_activation', 0, '', 10, '', '', ?, ?, ?)
                    """,
                    (int(raw_user_id), "", now, now),
                )
            connection.execute(
                "UPDATE users SET remark = '' WHERE remark = ?",
                ("由现有策略数据自动补充",),
            )

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

    def _ensure_decision_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(decisions)").fetchall()
        }
        migrations = {
            "account_login": "ALTER TABLE decisions ADD COLUMN account_login TEXT NOT NULL DEFAULT ''",
            "account_server": "ALTER TABLE decisions ADD COLUMN account_server TEXT NOT NULL DEFAULT ''",
            "symbol": "ALTER TABLE decisions ADD COLUMN symbol TEXT NOT NULL DEFAULT ''",
            "timeframe": "ALTER TABLE decisions ADD COLUMN timeframe TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    def _ensure_ai_usage_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ai_usage_logs)").fetchall()
        }
        migrations = {
            "account_login": "ALTER TABLE ai_usage_logs ADD COLUMN account_login TEXT NOT NULL DEFAULT ''",
            "account_server": "ALTER TABLE ai_usage_logs ADD COLUMN account_server TEXT NOT NULL DEFAULT ''",
            "symbol": "ALTER TABLE ai_usage_logs ADD COLUMN symbol TEXT NOT NULL DEFAULT ''",
            "timeframe": "ALTER TABLE ai_usage_logs ADD COLUMN timeframe TEXT NOT NULL DEFAULT ''",
            "response_preview": "ALTER TABLE ai_usage_logs ADD COLUMN response_preview TEXT NOT NULL DEFAULT ''",
            "billing_source": "ALTER TABLE ai_usage_logs ADD COLUMN billing_source TEXT NOT NULL DEFAULT ''",
            "input_price_snapshot": "ALTER TABLE ai_usage_logs ADD COLUMN input_price_snapshot NUMERIC NOT NULL DEFAULT 0",
            "output_price_snapshot": "ALTER TABLE ai_usage_logs ADD COLUMN output_price_snapshot NUMERIC NOT NULL DEFAULT 0",
            "charged_amount": "ALTER TABLE ai_usage_logs ADD COLUMN charged_amount NUMERIC NOT NULL DEFAULT 0",
            "balance_after": "ALTER TABLE ai_usage_logs ADD COLUMN balance_after NUMERIC",
            "provider_called": "ALTER TABLE ai_usage_logs ADD COLUMN provider_called INTEGER NOT NULL DEFAULT 1",
            "response_source": "ALTER TABLE ai_usage_logs ADD COLUMN response_source TEXT NOT NULL DEFAULT 'provider'",
            "cache_id": "ALTER TABLE ai_usage_logs ADD COLUMN cache_id TEXT",
            "request_snapshot": "ALTER TABLE ai_usage_logs ADD COLUMN request_snapshot TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    def _ensure_ai_usage_monthly_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ai_usage_monthly_summaries)").fetchall()
        }
        migrations = {
            "provider_calls": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN provider_calls INTEGER NOT NULL DEFAULT 0",
            "cache_hits": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN cache_hits INTEGER NOT NULL DEFAULT 0",
            "provider_input_tokens": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN provider_input_tokens INTEGER NOT NULL DEFAULT 0",
            "provider_output_tokens": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN provider_output_tokens INTEGER NOT NULL DEFAULT 0",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    def _backfill_ai_cache_stats(self) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            marker = connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = ?",
                ("ai_cache_stats_backfilled",),
            ).fetchone()
            if marker is not None:
                return
            connection.execute(
                """
                UPDATE ai_usage_monthly_summaries
                SET provider_calls = calls,
                    cache_hits = 0,
                    provider_input_tokens = input_tokens,
                    provider_output_tokens = output_tokens
                """
            )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                ("ai_cache_stats_backfilled", "1", "AI 缓存统计历史数据已初始化", now),
            )

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

    def _ensure_ai_endpoint_tables(self, connection: sqlite3.Connection) -> None:
        now = utc_now_iso()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_templates (
                code TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                request_type TEXT NOT NULL DEFAULT 'openai_compatible',
                enabled INTEGER NOT NULL DEFAULT 1,
                remark TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_endpoints (
                id TEXT PRIMARY KEY,
                owner_type TEXT NOT NULL DEFAULT 'gl',
                user_id TEXT NOT NULL DEFAULT '',
                template_code TEXT NOT NULL DEFAULT 'openai_compatible',
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                strict_json INTEGER NOT NULL DEFAULT 1,
                context_window INTEGER NOT NULL DEFAULT 0,
                input_token_rate REAL NOT NULL DEFAULT 1,
                output_token_rate REAL NOT NULL DEFAULT 1,
                billing_multiplier REAL NOT NULL DEFAULT 1,
                input_price_per_million NUMERIC NOT NULL DEFAULT 0,
                output_price_per_million NUMERIC NOT NULL DEFAULT 0,
                supports_vision INTEGER NOT NULL DEFAULT 0,
                vision_test_status TEXT NOT NULL DEFAULT 'untested',
                vision_tested_at TEXT NOT NULL DEFAULT '',
                vision_test_error TEXT NOT NULL DEFAULT '',
                is_default INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                selectable_by_user INTEGER NOT NULL DEFAULT 0,
                sort INTEGER NOT NULL DEFAULT 9999,
                remark TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ai_endpoints)").fetchall()
        }
        migrations = {
            "owner_type": "ALTER TABLE ai_endpoints ADD COLUMN owner_type TEXT NOT NULL DEFAULT 'gl'",
            "user_id": "ALTER TABLE ai_endpoints ADD COLUMN user_id TEXT NOT NULL DEFAULT ''",
            "template_code": "ALTER TABLE ai_endpoints ADD COLUMN template_code TEXT NOT NULL DEFAULT 'openai_compatible'",
            "name": "ALTER TABLE ai_endpoints ADD COLUMN name TEXT NOT NULL DEFAULT ''",
            "base_url": "ALTER TABLE ai_endpoints ADD COLUMN base_url TEXT NOT NULL DEFAULT ''",
            "model": "ALTER TABLE ai_endpoints ADD COLUMN model TEXT NOT NULL DEFAULT ''",
            "api_key": "ALTER TABLE ai_endpoints ADD COLUMN api_key TEXT NOT NULL DEFAULT ''",
            "strict_json": "ALTER TABLE ai_endpoints ADD COLUMN strict_json INTEGER NOT NULL DEFAULT 1",
            "context_window": "ALTER TABLE ai_endpoints ADD COLUMN context_window INTEGER NOT NULL DEFAULT 0",
            "input_token_rate": "ALTER TABLE ai_endpoints ADD COLUMN input_token_rate REAL NOT NULL DEFAULT 1",
            "output_token_rate": "ALTER TABLE ai_endpoints ADD COLUMN output_token_rate REAL NOT NULL DEFAULT 1",
            "billing_multiplier": "ALTER TABLE ai_endpoints ADD COLUMN billing_multiplier REAL NOT NULL DEFAULT 1",
            "input_price_per_million": "ALTER TABLE ai_endpoints ADD COLUMN input_price_per_million NUMERIC NOT NULL DEFAULT 0",
            "output_price_per_million": "ALTER TABLE ai_endpoints ADD COLUMN output_price_per_million NUMERIC NOT NULL DEFAULT 0",
            "supports_vision": "ALTER TABLE ai_endpoints ADD COLUMN supports_vision INTEGER NOT NULL DEFAULT 0",
            "vision_test_status": "ALTER TABLE ai_endpoints ADD COLUMN vision_test_status TEXT NOT NULL DEFAULT 'untested'",
            "vision_tested_at": "ALTER TABLE ai_endpoints ADD COLUMN vision_tested_at TEXT NOT NULL DEFAULT ''",
            "vision_test_error": "ALTER TABLE ai_endpoints ADD COLUMN vision_test_error TEXT NOT NULL DEFAULT ''",
            "is_default": "ALTER TABLE ai_endpoints ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0",
            "enabled": "ALTER TABLE ai_endpoints ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1",
            "selectable_by_user": "ALTER TABLE ai_endpoints ADD COLUMN selectable_by_user INTEGER NOT NULL DEFAULT 0",
            "sort": "ALTER TABLE ai_endpoints ADD COLUMN sort INTEGER NOT NULL DEFAULT 9999",
            "remark": "ALTER TABLE ai_endpoints ADD COLUMN remark TEXT NOT NULL DEFAULT ''",
            "created_at": "ALTER TABLE ai_endpoints ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
            "updated_at": "ALTER TABLE ai_endpoints ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        connection.execute(
            """
            INSERT OR IGNORE INTO ai_templates (
                code, name, request_type, enabled, remark, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "openai_compatible",
                "OpenAI 兼容接口",
                "openai_compatible",
                1,
                "适用于 OpenAI、DeepSeek、通义千问兼容模式和大多数中转商。",
                now,
                now,
            ),
        )
        count_row = connection.execute("SELECT COUNT(*) FROM ai_endpoints").fetchone()
        if int(count_row[0] if count_row else 0) > 0:
            return
        rows = connection.execute(
            """
            SELECT
                m.*,
                p.name AS provider_name,
                p.api_key AS provider_api_key,
                COALESCE(NULLIF(m.base_url, ''), p.base_url) AS endpoint_base_url
            FROM ai_models m
            JOIN ai_providers p ON p.id = m.provider_id
            WHERE m.enabled = 1 AND p.enabled = 1 AND p.api_key <> ''
            ORDER BY m.is_default DESC, p.sort ASC, m.sort ASC, m.updated_at DESC
            """
        ).fetchall()
        for index, row in enumerate(rows):
            connection.execute(
                """
                INSERT INTO ai_endpoints (
                    id, owner_type, user_id, template_code, name, base_url, model, api_key,
                    context_window, input_token_rate, output_token_rate, billing_multiplier,
                    is_default, enabled, selectable_by_user, sort, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"aie_{uuid4().hex}",
                    "gl",
                    "",
                    "openai_compatible",
                    f"{row['provider_name']} / {row['display_name'] or row['name']}",
                    str(row["endpoint_base_url"] or ""),
                    str(row["name"] or ""),
                    str(row["provider_api_key"] or ""),
                    int(row["context_window"] or 0),
                    float(row["input_token_rate"] or 1),
                    float(row["output_token_rate"] or 1),
                    float(row["billing_multiplier"] or 1),
                    1 if (index == 0 or row["is_default"]) else 0,
                    1,
                    1,
                    int(row["sort"] or 9999),
                    str(row["remark"] or ""),
                    now,
                    now,
                ),
            )

    def _ensure_official_strategy_columns(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(official_ai_strategies)").fetchall()
        }
        migrations = {
            "open_ai_endpoint_id": "ALTER TABLE official_ai_strategies ADD COLUMN open_ai_endpoint_id TEXT NOT NULL DEFAULT ''",
            "position_ai_endpoint_id": "ALTER TABLE official_ai_strategies ADD COLUMN position_ai_endpoint_id TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

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
                WHERE user_id = ? AND status <> 'deleted'
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

    def update_user_deployment_status(self, *, user_id: int, deployment_id: str, status: str) -> dict[str, Any]:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"active", "paused"}:
            raise RuntimeError("invalid_deployment_status")
        now = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ? AND user_id = ? AND status <> 'deleted'",
                (deployment_id, str(user_id)),
            ).fetchone()
            if row is None:
                raise RuntimeError("deployment_not_found")
            connection.execute(
                "UPDATE deployments SET status = ?, updated_at = ? WHERE id = ?",
                (normalized_status, now, deployment_id),
            )
            updated = connection.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,)).fetchone()
        return self._deployment_row(updated)

    def delete_user_deployment(self, *, user_id: int, deployment_id: str) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM deployments WHERE id = ? AND user_id = ? AND status <> 'deleted'",
                (deployment_id, str(user_id)),
            ).fetchone()
            if row is None:
                raise RuntimeError("deployment_not_found")
            connection.execute(
                "UPDATE deployments SET status = 'deleted', updated_at = ? WHERE id = ?",
                (now, deployment_id),
            )

    def update_user_deployment_ai_config(
        self,
        *,
        user_id: int,
        deployment_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deployments WHERE id = ? AND user_id = ? AND status <> 'deleted'",
                (deployment_id, str(user_id)),
            ).fetchone()
            if row is None:
                raise RuntimeError("deployment_not_found")
            deployment = self._deployment_row(row)
            config = dict(deployment.get("config") or {})
            for prefix in ("open", "position"):
                mode = str(payload.get(f"{prefix}_ai_mode") or config.get(f"{prefix}_ai_mode") or "official").strip().lower()
                if mode not in {"official", "custom"}:
                    raise RuntimeError("invalid_ai_mode")
                config[f"{prefix}_ai_mode"] = mode
                if mode == "official":
                    endpoint_id = str(payload.get(f"{prefix}_ai_endpoint_id") or "").strip()
                    endpoint = connection.execute(
                        """
                        SELECT id, model, supports_vision FROM ai_endpoints
                        WHERE id = ? AND owner_type = 'gl' AND enabled = 1
                          AND selectable_by_user = 1 AND api_key <> ''
                        """,
                        (endpoint_id,),
                    ).fetchone()
                    if endpoint is None:
                        raise RuntimeError("invalid_ai_endpoint")
                    config[f"{prefix}_ai_endpoint_id"] = str(endpoint["id"] or "")
                    config[f"{prefix}_ai_model"] = str(endpoint["model"] or "")
                    config[f"{prefix}_ai_base_url"] = ""
                    config[f"{prefix}_ai_key"] = ""
                    config[f"{prefix}_ai_vision_verified"] = bool(endpoint["supports_vision"])
                else:
                    base_url = str(payload.get(f"{prefix}_ai_base_url") or "").strip()
                    model = str(payload.get(f"{prefix}_ai_model") or "").strip()
                    new_key = str(payload.get(f"{prefix}_ai_key") or "").strip()
                    existing_key = str(config.get(f"{prefix}_ai_key") or "").strip()
                    if not base_url or not model or not (new_key or existing_key):
                        raise RuntimeError("custom_ai_config_required")
                    config[f"{prefix}_ai_endpoint_id"] = ""
                    config[f"{prefix}_ai_base_url"] = base_url.rstrip("/")
                    config[f"{prefix}_ai_model"] = model
                    if new_key:
                        config[f"{prefix}_ai_key"] = new_key
                    config[f"{prefix}_ai_vision_verified"] = bool(
                        payload.get(f"{prefix}_ai_vision_verified", config.get(f"{prefix}_ai_vision_verified", False))
                    )
            config["ai_user_configured"] = True
            connection.execute(
                "UPDATE deployments SET config_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(config, ensure_ascii=False), now, deployment_id),
            )
            updated = connection.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,)).fetchone()
        return self._deployment_row(updated)

    def update_user_deployment_settings(
        self,
        *,
        user_id: int,
        deployment_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        deployment = self.update_user_deployment_ai_config(
            user_id=user_id,
            deployment_id=deployment_id,
            payload=payload,
        )
        config = dict(deployment.get("config") or {})
        size_mode = str(payload.get("position_size_mode") or config.get("position_size_mode") or "fixed")
        risk_mode = str(payload.get("risk_base_mode") or config.get("risk_base_mode") or "fixed_loss")
        status = str(payload.get("status") or deployment.get("status") or "active")
        if size_mode not in {"fixed", "risk"} or risk_mode not in {"fixed_loss", "balance_percent"}:
            raise RuntimeError("invalid_strategy_settings")
        if status not in {"active", "paused"}:
            raise RuntimeError("invalid_deployment_status")
        try:
            fixed_volume = float(payload.get("fixed_volume", config.get("fixed_volume", 0.01)))
            risk_amount = float(payload.get("risk_amount", config.get("risk_amount", 100)))
            risk_percent = float(payload.get("risk_percent", config.get("risk_percent", 1)))
            max_positions = int(payload.get("max_positions", config.get("max_positions", 1)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid_strategy_settings") from exc
        if fixed_volume < 0 or risk_amount < 0 or risk_percent <= 0 or max_positions < 1:
            raise RuntimeError("invalid_strategy_settings")
        config.update({
            "position_size_mode": size_mode,
            "fixed_volume": fixed_volume,
            "lot": fixed_volume,
            "risk_base_mode": risk_mode,
            "risk_amount": risk_amount,
            "risk_percent": risk_percent,
            "max_positions": max_positions,
            "allow_add": bool(payload.get("allow_add", config.get("allow_add", False))),
        })
        if deployment.get("strategy_code") == "CUSTOM_AI_V1":
            if "open_logic" in payload:
                config["open_logic"] = str(payload.get("open_logic") or "").strip()
            if "position_logic" in payload:
                config["position_logic"] = str(payload.get("position_logic") or "").strip()
            if "ea_description" in payload:
                ea_description = str(payload.get("ea_description") or "").strip()
                if len(ea_description) > 1000:
                    raise RuntimeError("invalid_strategy_description")
                config["ea_description"] = ea_description
            compiled_config = payload.get("_compiled_config")
            if isinstance(compiled_config, dict):
                config.update(compiled_config)
            for prefix in ("open", "position"):
                data_type = str(payload.get(f"{prefix}_data_type") or config.get(f"{prefix}_data_type") or "kline").strip().lower()
                try:
                    requested_count = int(
                        payload.get(
                            f"{prefix}_kline_count",
                            config.get(f"{prefix}_requested_kline_count") or config.get(f"{prefix}_kline_count") or 100,
                        ),
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("invalid_strategy_data_settings") from exc
                if data_type not in {"kline", "screenshot", "both"} or not 10 <= requested_count <= 1000:
                    raise RuntimeError("invalid_strategy_data_settings")
                config[f"{prefix}_data_type"] = data_type
                config[f"{prefix}_requested_kline_count"] = requested_count
                indicator_count = int(config.get(f"{prefix}_indicator_kline_count") or 100)
                config[f"{prefix}_kline_count"] = (
                    max(requested_count, indicator_count) if data_type in {"kline", "both"} else 1
                )
                if data_type in {"screenshot", "both"} and not bool(config.get(f"{prefix}_ai_vision_verified", False)):
                    raise RuntimeError("ai_vision_test_required")
        strategy_name = str(payload.get("name") or deployment.get("strategy_name") or "").strip()
        mt_login = str(payload.get("mt_login") if "mt_login" in payload else deployment.get("mt_login") or "").strip()
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments
                SET strategy_name = ?, status = ?, mt_login = ?, config_json = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status <> 'deleted'
                """,
                (strategy_name, status, mt_login or None, json.dumps(config, ensure_ascii=False), now, deployment_id, str(user_id)),
            )
            updated = connection.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,)).fetchone()
        if updated is None:
            raise RuntimeError("deployment_not_found")
        return self._deployment_row(updated)

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
            history_summary = connection.execute(
                """
                SELECT COALESCE(SUM(net_profit), 0) net_profit
                FROM mt_order_time_summaries
                WHERE deployment_id = ? AND bucket_type = 'day'
                """,
                (deployment_id,),
            ).fetchone()

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

        stats["pnl"] = round(float(history_summary["net_profit"] or 0), 2) if history_summary else 0.0
        return stats

    def deployment_detail_stats(self, deployment_id: str) -> dict[str, Any]:
        stats = self.deployment_runtime_stats(deployment_id)
        with self._connect() as connection:
            summary_row = connection.execute(
                """
                SELECT COALESCE(SUM(order_count), 0) closed_count,
                       COALESCE(SUM(win_count), 0) win_count,
                       COALESCE(SUM(loss_count), 0) loss_count,
                       COUNT(DISTINCT CASE WHEN symbol <> '' THEN symbol END) symbol_count
                FROM mt_order_time_summaries
                WHERE deployment_id = ? AND bucket_type = 'day'
                """,
                (deployment_id,),
            ).fetchone()
            history_rows = connection.execute(
                """
                SELECT bucket_start, COALESCE(SUM(net_profit), 0) net_profit
                FROM mt_order_time_summaries
                WHERE deployment_id = ? AND bucket_type = 'day'
                GROUP BY bucket_start
                ORDER BY bucket_start
                """,
                (deployment_id,),
            ).fetchall()

        closed_count = int(summary_row["closed_count"] or 0) if summary_row else 0
        win_count = int(summary_row["win_count"] or 0) if summary_row else 0
        loss_count = int(summary_row["loss_count"] or 0) if summary_row else 0
        flat_count = max(0, closed_count - win_count - loss_count)
        curve: list[dict[str, Any]] = []
        cumulative_pnl = 0.0

        for row in history_rows:
            pnl = round(float(row["net_profit"] or 0), 2)
            cumulative_pnl = round(cumulative_pnl + pnl, 2)
            curve.append(
                {
                    "time": int(row["bucket_start"] or 0),
                    "pnl": pnl,
                    "cumulative_pnl": cumulative_pnl,
                },
            )

        return {
            "summary": {
                **stats,
                "win_count": win_count,
                "loss_count": loss_count,
                "flat_count": flat_count,
                "win_rate": round((win_count / closed_count) * 100, 2) if closed_count else 0.0,
                "traded_symbol_count": int(summary_row["symbol_count"] or 0) if summary_row else 0,
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

    def admin_deployment_order_overview(
        self,
        deployment_id: str,
        *,
        period: str = "all",
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            deployment = connection.execute(
                "SELECT * FROM deployments WHERE id = ?",
                (deployment_id,),
            ).fetchone()
            if deployment is None:
                raise RuntimeError("deployment_not_found")
            start_iso, end_iso, _, _, _ = _period_bounds(period)
            decision_rows = connection.execute(
                """
                SELECT endpoint, response_json
                FROM decisions
                WHERE deployment_id = ? AND created_at >= ? AND created_at <= ?
                """,
                (deployment_id, start_iso, end_iso),
            ).fetchall()
            usage = connection.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) input_tokens,
                       COALESCE(SUM(output_tokens), 0) output_tokens,
                       COALESCE(SUM(total_tokens), 0) total_tokens,
                       COALESCE(SUM(official_tokens), 0) official_tokens,
                       COALESCE(SUM(custom_tokens), 0) custom_tokens
                FROM ai_usage_logs
                WHERE deployment_id = ? AND created_at >= ? AND created_at <= ?
                """,
                (deployment_id, start_iso, end_iso),
            ).fetchone()
        analysis_count = len(decision_rows)
        signal_count = 0
        order_count = 0
        for row in decision_rows:
            try:
                payload = json.loads(str(row["response_json"] or "{}"))
            except (TypeError, json.JSONDecodeError):
                payload = {}
            action = str(payload.get("action") or "").upper()
            if action and action != "HOLD":
                signal_count += 1
            if str(row["endpoint"] or "") == "open" and action in {"BUY", "SELL"}:
                order_count += 1
        result = self.list_user_orders(
            user_id=int(deployment["user_id"]),
            deployment_id=deployment_id,
            page=page,
            size=size,
            start_at=start_iso,
            end_at=end_iso,
        )
        result["runtime_summary"] = {
            "analysis_count": analysis_count,
            "signal_count": signal_count,
            "order_count": order_count,
            "input_tokens": int(usage["input_tokens"] or 0) if usage else 0,
            "output_tokens": int(usage["output_tokens"] or 0) if usage else 0,
            "total_tokens": int(usage["total_tokens"] or 0) if usage else 0,
            "official_tokens": int(usage["official_tokens"] or 0) if usage else 0,
            "custom_tokens": int(usage["custom_tokens"] or 0) if usage else 0,
        }
        return result

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
            deployment_data = self._deployment_row(deployment)
            official_strategy = self.get_official_ai_strategy(str(deployment_data["strategy_code"] or ""))
            if official_strategy is not None:
                deployment_data["strategy_name"] = official_strategy["name"]
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
                    account_login, account_server, symbol,
                    COALESCE(SUM(total_volume), 0) volume,
                    COALESCE(SUM(net_profit), 0) net_profit,
                    COALESCE(SUM(order_count), 0) order_count,
                    COALESCE(SUM(win_count), 0) win_count,
                    COALESCE(SUM(loss_count), 0) loss_count,
                    MAX(bucket_start) close_time,
                    MAX(updated_at) updated_at
                FROM mt_order_time_summaries
                WHERE deployment_id = ?
                  AND bucket_type = 'day'
                  AND bucket_start >= ?
                  AND bucket_start <= ?
                GROUP BY account_login, account_server, symbol
                """,
                (deployment_id, _bucket_timestamp(start_ts, "day"), _bucket_timestamp(end_ts, "day")),
            ).fetchall()

        normalized_accounts = self._normalized_account_items(
            [
                {
                    "login": str(row["login"] or ""),
                    "provider": str(row["provider"] or ""),
                    "server": str(row["server"] or ""),
                    "last_seen_at": str(row["last_seen_at"] or ""),
                }
                for row in account_rows
            ],
        )
        account_map = {
            (account["login"], account["server"]): account
            for account in normalized_accounts
        }
        preferred_server_by_login = {
            account["login"]: account["server"]
            for account in normalized_accounts
            if account["login"] and account["server"]
        }
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in history_rows:
            if not _is_profit_deal_entry("out"):
                continue
            login = str(row["account_login"] or "")
            raw_server = str(row["account_server"] or "")
            server = raw_server or preferred_server_by_login.get(login, "")
            symbol = str(row["symbol"] or "")
            key = (login, server, symbol)
            account_info = account_map.get((login, server), {})
            item = groups.setdefault(
                key,
                {
                    "deployment_id": deployment_id,
                    "user_id": str(deployment_data["user_id"] or ""),
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
            row_count = int(row["order_count"] or 0)
            row_wins = int(row["win_count"] or 0)
            row_losses = int(row["loss_count"] or 0)
            item["close_order_count"] += row_count
            item["volume"] = round(float(item["volume"]) + float(row["volume"] or 0), 4)
            item["pnl"] = round(float(item["pnl"]) + profit, 2)
            item["win_count"] += row_wins
            item["loss_count"] += row_losses
            item["flat_count"] += max(0, row_count - row_wins - row_losses)
            close_time = int(row["close_time"] or 0)
            item["last_close_time"] = max(int(item["last_close_time"] or 0), close_time)
            item["last_active_at"] = max(str(item["last_active_at"] or ""), str(row["updated_at"] or ""))

        items = []
        for item in groups.values():
            closed_count = int(item["close_order_count"] or 0)
            item["win_rate"] = round((int(item["win_count"] or 0) / closed_count) * 100, 2) if closed_count else 0.0
            items.append(item)
        return {
            "deployment": deployment_data,
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
            where.append("(account_server = ? OR account_server = '')")
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
        billing_settings = self.get_ai_billing_settings()
        credit_limit = Decimal(str(billing_settings["credit_limit"]))
        warning_threshold = Decimal(str(billing_settings["low_balance_threshold"]))
        month_start = datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00+00:00")
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
            wallet_user_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS user_count,
                    COALESCE(SUM(ai_balance), 0) AS total_balance,
                    COALESCE(SUM(CASE WHEN ai_balance < ? THEN 1 ELSE 0 END), 0) AS low_balance_users,
                    COALESCE(SUM(CASE WHEN ai_balance <= ? THEN 1 ELSE 0 END), 0) AS exhausted_users
                FROM users
                """,
                (float(warning_threshold), float(-credit_limit)),
            ).fetchone()
            wallet_ledger_row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN entry_type = 'admin_recharge' THEN amount ELSE 0 END), 0) AS total_recharge,
                    COALESCE(SUM(CASE WHEN entry_type = 'admin_deduction' THEN -amount ELSE 0 END), 0) AS total_deduction,
                    COALESCE(SUM(CASE WHEN entry_type = 'ai_charge' THEN -amount ELSE 0 END), 0) AS total_ai_charged
                FROM ai_balance_ledger
                """
            ).fetchone()
            monthly_charge_row = connection.execute(
                """
                SELECT COALESCE(SUM(charged_amount), 0) AS charged_amount
                FROM ai_usage_logs
                WHERE created_at >= ?
                """,
                (month_start,),
            ).fetchone()
            history_rows = connection.execute(
                """
                SELECT deployment_id, COALESCE(SUM(net_profit), 0) net_profit
                FROM mt_order_time_summaries
                WHERE bucket_type = 'day'
                GROUP BY deployment_id
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
                "user_count": int(wallet_user_row["user_count"] or 0) if wallet_user_row else 0,
                "strategy_user_count": len({item["user_id"] for item in by_deployment.values()}),
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
                "total_balance": decimal_string(wallet_user_row["total_balance"] if wallet_user_row else 0),
                "total_recharge": decimal_string(wallet_ledger_row["total_recharge"] if wallet_ledger_row else 0),
                "total_deduction": decimal_string(wallet_ledger_row["total_deduction"] if wallet_ledger_row else 0),
                "total_ai_charged": decimal_string(wallet_ledger_row["total_ai_charged"] if wallet_ledger_row else 0),
                "monthly_ai_charged": decimal_string(monthly_charge_row["charged_amount"] if monthly_charge_row else 0),
                "low_balance_user_count": int(wallet_user_row["low_balance_users"] or 0) if wallet_user_row else 0,
                "credit_exhausted_user_count": int(wallet_user_row["exhausted_users"] or 0) if wallet_user_row else 0,
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

    def list_admin_custom_strategies(
        self, *, page: int, size: int, keyword: str = "", unsupported_only: bool = False,
    ) -> dict[str, Any]:
        clauses = ["d.strategy_code = 'CUSTOM_AI_V1'", "d.status <> 'deleted'"]
        params: list[Any] = []
        if keyword:
            like = f"%{keyword}%"
            clauses.append(
                "(d.strategy_name LIKE ? OR d.user_id LIKE ? OR d.mt_login LIKE ? "
                "OR d.config_json LIKE ? OR u.email LIKE ? OR u.nickname LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])
        if unsupported_only:
            clauses.append(
                "d.config_json LIKE '%\"unsupported_condition_count\": %' "
                "AND d.config_json NOT LIKE '%\"unsupported_condition_count\": 0%'"
            )
        where = f"WHERE {' AND '.join(clauses)}"
        return self._paged_query_sql(
            count_sql=f"""
                SELECT COUNT(*)
                FROM deployments d
                LEFT JOIN users u ON u.id = d.user_id
                {where}
            """,
            list_sql=f"""
                SELECT d.*, u.email, u.nickname
                FROM deployments d
                LEFT JOIN users u ON u.id = d.user_id
                {where}
                ORDER BY d.updated_at DESC, d.id DESC
                LIMIT ? OFFSET ?
            """,
            params=params,
            page=page,
            size=size,
            mapper=self._admin_custom_strategy_row,
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
                    open_model_id, position_model_id, open_ai_endpoint_id, position_ai_endpoint_id,
                    default_config_json, enabled, sort, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    open_ai_endpoint_id = excluded.open_ai_endpoint_id,
                    position_ai_endpoint_id = excluded.position_ai_endpoint_id,
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
                    "",
                    "",
                    str(payload.get("open_ai_endpoint_id") or ""),
                    str(payload.get("position_ai_endpoint_id") or ""),
                    json.dumps(config, ensure_ascii=False),
                    1 if payload.get("enabled", True) else 0,
                    int(payload.get("sort") or 9999),
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
        saved = self.get_official_ai_strategy(strategy_id)
        if saved is None:
            raise RuntimeError("official_strategy_save_failed")
        return saved

    def list_public_official_ai_strategies(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    s.*,
                    open_endpoint.name AS open_endpoint_name,
                    open_endpoint.model AS open_endpoint_model,
                    position_endpoint.name AS position_endpoint_name,
                    position_endpoint.model AS position_endpoint_model
                FROM official_ai_strategies s
                LEFT JOIN ai_endpoints open_endpoint ON open_endpoint.id = s.open_ai_endpoint_id
                LEFT JOIN ai_endpoints position_endpoint ON position_endpoint.id = s.position_ai_endpoint_id
                WHERE s.enabled = 1
                ORDER BY s.sort ASC, s.updated_at DESC
                """
            ).fetchall()
        return {
            "list": [
                {
                    "id": str(row["id"] or ""),
                    "code": str(row["code"] or ""),
                    "name": str(row["name"] or ""),
                    "badge": str(row["badge"] or "Gainlab"),
                    "version": str(row["version"] or "1.0"),
                    "status": str(row["status"] or "active"),
                    "summary": str(row["summary"] or ""),
                    "open_logic": str(row["open_logic"] or ""),
                    "position_logic": str(row["position_logic"] or ""),
                    "open_data_type": str(row["open_data_type"] or "kline"),
                    "open_kline_count": int(row["open_kline_count"] or 100),
                    "position_data_type": str(row["position_data_type"] or "kline"),
                    "position_kline_count": int(row["position_kline_count"] or 100),
                    "call_mode": str(row["call_mode"] or "bar"),
                    "call_val": float(row["call_value"] or 1),
                    "open_ai_endpoint_id": str(row["open_ai_endpoint_id"] or ""),
                    "open_ai_endpoint_name": str(row["open_endpoint_name"] or ""),
                    "open_ai_endpoint_model": str(row["open_endpoint_model"] or ""),
                    "position_ai_endpoint_id": str(row["position_ai_endpoint_id"] or ""),
                    "position_ai_endpoint_name": str(row["position_endpoint_name"] or ""),
                    "position_ai_endpoint_model": str(row["position_endpoint_model"] or ""),
                    "default_config": self._official_strategy_row(row).get("default_config", {}),
                }
                for row in rows
            ],
        }

    def admin_official_strategy_detail(self, strategy_id: str, *, period: str = "all") -> dict[str, Any]:
        strategy = self.get_official_ai_strategy(strategy_id)
        if strategy is None:
            raise RuntimeError("official_strategy_not_found")
        stats = self._admin_strategy_code_stats(
            str(strategy["code"]),
            period=period,
            strategy_name=str(strategy["name"] or ""),
        )
        return {
            "strategy": strategy,
            **stats,
        }

    def _admin_strategy_code_stats(
        self,
        strategy_code: str,
        *,
        period: str = "all",
        strategy_name: str = "",
    ) -> dict[str, Any]:
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
                SELECT
                    decision.deployment_id,
                    decision.endpoint,
                    decision.account_login,
                    decision.account_server,
                    decision.symbol,
                    decision.timeframe,
                    decision.response_json,
                    decision.created_at
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
                    account_login,
                    account_server,
                    symbol,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(official_tokens), 0) AS official_tokens,
                    COALESCE(SUM(custom_tokens), 0) AS custom_tokens
                FROM ai_usage_logs
                WHERE strategy_code = ?
                  AND created_at >= ?
                  AND created_at <= ?
                GROUP BY deployment_id, account_login, account_server, symbol
                """,
                (strategy_code, start_iso, end_iso),
            ).fetchall()
            history_rows = connection.execute(
                """
                SELECT
                    s.deployment_id,
                    s.account_login,
                    s.account_server,
                    s.symbol,
                    COALESCE(SUM(s.order_count), 0) close_order_count,
                    COALESCE(SUM(s.net_profit), 0) net_profit,
                    s.bucket_start close_time,
                    MAX(s.updated_at) updated_at
                FROM mt_order_time_summaries s
                JOIN deployments d ON d.id = s.deployment_id
                WHERE d.strategy_code = ?
                  AND s.bucket_type = ?
                  AND s.bucket_start >= ?
                  AND s.bucket_start <= ?
                GROUP BY s.deployment_id, s.account_login, s.account_server,
                         s.symbol, s.bucket_start
                """,
                (
                    strategy_code, bucket,
                    _bucket_timestamp(start_ts, bucket),
                    _bucket_timestamp(end_ts, bucket),
                ),
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
                "name": deployment["strategy_name"] or strategy_name,
                "deployment_key": str(deployment.get("config", {}).get("deployment_key") or ""),
                "status": deployment["status"],
                "account_login": "",
                "account_provider": "",
                "account_server": "",
                "account_type": "",
                "symbol": "",
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

        raw_accounts_by_deployment: dict[str, list[dict[str, Any]]] = {}
        for row in account_rows:
            raw_accounts_by_deployment.setdefault(str(row["deployment_id"] or ""), []).append(
                {
                    "login": str(row["login"] or ""),
                    "provider": str(row["provider"] or ""),
                    "server": str(row["server"] or ""),
                    "account_type": "demo" if row["is_demo"] else "real",
                    "last_seen_at": str(row["last_seen_at"] or ""),
                },
            )
            item = by_deployment.get(row["deployment_id"])
            if item is None or item["account_login"]:
                continue
            item["account_login"] = row["login"]
            item["account_provider"] = row["provider"]
            item["account_server"] = row["server"]
            item["account_type"] = "demo" if row["is_demo"] else "real"

        accounts_by_deployment = {
            deployment_id: self._normalized_account_items(accounts)
            for deployment_id, accounts in raw_accounts_by_deployment.items()
        }
        preferred_server_by_deployment_login = {
            (deployment_id, account["login"]): account["server"]
            for deployment_id, accounts in accounts_by_deployment.items()
            for account in accounts
            if account["login"] and account["server"]
        }

        total_analysis = 0
        total_signals = 0
        total_orders = 0
        active_deployments: set[str] = set()
        decision_account_stats: dict[tuple[str, str, str, str], dict[str, Any]] = {}
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
            account_login = str(row["account_login"] or "")
            if account_login:
                account_server = (
                    str(row["account_server"] or "")
                    or preferred_server_by_deployment_login.get((str(row["deployment_id"] or ""), account_login), "")
                )
                account_symbol = str(row["symbol"] or "").strip().upper()
                account_key = (str(row["deployment_id"] or ""), account_login, account_server, account_symbol)
                account_stat = decision_account_stats.setdefault(
                    account_key,
                    {
                        "analysis_count": 0,
                        "signal_count": 0,
                        "order_count": 0,
                        "last_active_at": "",
                    },
                )
                account_stat["analysis_count"] = int(account_stat["analysis_count"] or 0) + 1
                account_stat["last_active_at"] = max(str(account_stat["last_active_at"] or ""), str(row["created_at"] or ""))
                if action and action != "HOLD":
                    account_stat["signal_count"] = int(account_stat["signal_count"] or 0) + 1
                if row["endpoint"] == "open" and action in {"BUY", "SELL"}:
                    account_stat["order_count"] = int(account_stat["order_count"] or 0) + 1

        for row in activity_rows:
            item = by_deployment.get(row["deployment_id"])
            if item is None:
                continue
            active_deployments.add(row["deployment_id"])
            item["activity_count"] += 1
            item["last_active_at"] = max(str(item["last_active_at"] or ""), str(row["created_at"] or ""))

        usage_account_stats: dict[tuple[str, str, str, str], dict[str, int]] = {}
        deployment_usage_stats: dict[str, dict[str, int]] = {}
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
                deployment_stats = deployment_usage_stats.setdefault(
                    str(row["deployment_id"] or ""),
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "official_tokens": 0,
                        "custom_tokens": 0,
                        "total_tokens": 0,
                    },
                )
                deployment_stats["input_tokens"] += input_tokens
                deployment_stats["output_tokens"] += output_tokens
                deployment_stats["official_tokens"] += official_tokens
                deployment_stats["custom_tokens"] += custom_tokens
                deployment_stats["total_tokens"] += row_total_tokens
                account_login = str(row["account_login"] or "")
                if account_login:
                    account_server = (
                        str(row["account_server"] or "")
                        or preferred_server_by_deployment_login.get((str(row["deployment_id"] or ""), account_login), "")
                    )
                    account_symbol = str(row["symbol"] or "").strip().upper()
                    usage_key = (
                        str(row["deployment_id"] or ""),
                        account_login,
                        account_server,
                        account_symbol,
                    )
                    account_stats = usage_account_stats.setdefault(
                        usage_key,
                        {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "official_tokens": 0,
                            "custom_tokens": 0,
                            "total_tokens": 0,
                        },
                    )
                    account_stats["input_tokens"] += input_tokens
                    account_stats["output_tokens"] += output_tokens
                    account_stats["official_tokens"] += official_tokens
                    account_stats["custom_tokens"] += custom_tokens
                    account_stats["total_tokens"] += row_total_tokens

        for deployment_id, stats in deployment_usage_stats.items():
            item = by_deployment.get(deployment_id)
            if item is not None:
                item.update(stats)

        total_pnl = 0.0
        total_close_orders = 0
        pnl_buckets: dict[str, float] = {}
        account_trade_stats: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        account_symbols: dict[tuple[str, str, str], set[str]] = {}
        for row in history_rows:
            profit = float(row["net_profit"] or 0)
            close_order_count = int(row["close_order_count"] or 0)
            total_pnl += profit
            total_close_orders += close_order_count
            close_time = int(row["close_time"] or 0)
            bucket_key = _time_bucket(close_time, bucket)
            pnl_buckets[bucket_key] = round(pnl_buckets.get(bucket_key, 0.0) + profit, 2)
            item = by_deployment.get(row["deployment_id"])
            if item is not None:
                active_deployments.add(row["deployment_id"])
                item["last_active_at"] = max(str(item["last_active_at"] or ""), str(row["updated_at"] or ""))
                item["pnl"] = round(float(item["pnl"]) + profit, 2)
                item["close_order_count"] += close_order_count
            symbol = str(row["symbol"] or "").strip().upper()
            account_base_key = (
                str(row["deployment_id"] or ""),
                str(row["account_login"] or ""),
                str(row["account_server"] or "")
                or preferred_server_by_deployment_login.get(
                    (str(row["deployment_id"] or ""), str(row["account_login"] or "")),
                    "",
                ),
            )
            account_key = (*account_base_key, symbol)
            account_stats = account_trade_stats.setdefault(
                account_key,
                {
                    "pnl": 0.0,
                    "close_order_count": 0,
                    "last_active_at": "",
                },
            )
            if symbol:
                account_symbols.setdefault(account_base_key, set()).add(symbol)
            account_stats["pnl"] = round(float(account_stats["pnl"] or 0) + profit, 2)
            account_stats["close_order_count"] = int(account_stats["close_order_count"] or 0) + close_order_count
            account_stats["last_active_at"] = max(
                str(account_stats["last_active_at"] or ""),
                str(row["updated_at"] or ""),
            )

        for item in by_deployment.values():
            if not item["last_active_at"]:
                item["last_active_at"] = item["updated_at"]

        expanded_deployments: list[dict[str, Any]] = []
        for deployment_id, item in by_deployment.items():
            accounts = accounts_by_deployment.get(deployment_id) or []
            if not accounts:
                expanded_deployments.append(item)
                continue
            for account in accounts:
                account_base_key = (deployment_id, account["login"], account["server"])
                symbols = set(account_symbols.get(account_base_key) or set())
                symbols.update(
                    key[3]
                    for key in decision_account_stats
                    if key[:3] == account_base_key and key[3]
                )
                display_symbols = sorted(symbols)
                for symbol in display_symbols:
                    account_item = dict(item)
                    account_item["account_login"] = account["login"]
                    account_item["account_provider"] = account["provider"]
                    account_item["account_server"] = account["server"]
                    account_item["account_type"] = account["account_type"]
                    account_item["analysis_count"] = 0
                    account_item["signal_count"] = 0
                    account_item["order_count"] = 0
                    account_item["input_tokens"] = 0
                    account_item["output_tokens"] = 0
                    account_item["official_tokens"] = 0
                    account_item["custom_tokens"] = 0
                    account_item["total_tokens"] = 0
                    account_item["pnl"] = 0.0
                    account_item["close_order_count"] = 0
                    trade_stats = account_trade_stats.get((*account_base_key, symbol), {})
                    decision_stats = decision_account_stats.get((*account_base_key, symbol), {})
                    usage_stats = usage_account_stats.get((*account_base_key, symbol), {})
                    if decision_stats:
                        account_item["analysis_count"] = int(decision_stats.get("analysis_count") or 0)
                        account_item["signal_count"] = int(decision_stats.get("signal_count") or 0)
                        account_item["order_count"] = int(decision_stats.get("order_count") or 0)
                    if usage_stats:
                        account_item["input_tokens"] = int(usage_stats.get("input_tokens") or 0)
                        account_item["output_tokens"] = int(usage_stats.get("output_tokens") or 0)
                        account_item["official_tokens"] = int(usage_stats.get("official_tokens") or 0)
                        account_item["custom_tokens"] = int(usage_stats.get("custom_tokens") or 0)
                        account_item["total_tokens"] = int(usage_stats.get("total_tokens") or 0)
                    account_item["symbol"] = symbol
                    account_item["pnl"] = round(float(trade_stats.get("pnl") or 0), 2)
                    account_item["close_order_count"] = int(trade_stats.get("close_order_count") or 0)
                    account_item["last_active_at"] = max(
                        str(account_item.get("last_active_at") or ""),
                        str(account.get("last_seen_at") or ""),
                        str(trade_stats.get("last_active_at") or ""),
                        str(decision_stats.get("last_active_at") or ""),
                    )
                    expanded_deployments.append(account_item)
        expanded_deployments = [
            item
            for item in expanded_deployments
            if str(item.get("symbol") or "")
            or float(item.get("pnl") or 0)
            or int(item.get("close_order_count") or 0)
            or int(item.get("analysis_count") or 0)
            or int(item.get("signal_count") or 0)
            or int(item.get("order_count") or 0)
        ]

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
            "deployments": deployments_list,
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

    def bind_deployment_login(
        self,
        raw_key: str,
        *,
        login: str,
        platform: str = "MT5",
        server: str = "",
    ) -> dict[str, Any] | None:
        """Bind a deployment to its first MT login and reject later mismatches."""
        normalized_login = str(login or "").strip()
        if not normalized_login or normalized_login == "unknown":
            raise RuntimeError("invalid_deployment_account")

        key_hash = hash_deployment_key(raw_key)
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments
                SET mt_platform = ?, mt_login = ?, mt_server = ?, updated_at = ?
                WHERE key_hash = ?
                  AND (mt_login IS NULL OR TRIM(mt_login) = '')
                """,
                (
                    str(platform or "MT5").strip(),
                    normalized_login,
                    str(server or "").strip(),
                    now,
                    key_hash,
                ),
            )
            row = connection.execute(
                "SELECT * FROM deployments WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()

        if row is None:
            return None
        deployment = self._deployment_row(row)
        if str(deployment.get("mt_login") or "").strip() != normalized_login:
            raise RuntimeError("deployment_account_mismatch")
        return deployment

    def set_deployment_login(self, raw_key: str, login: str) -> dict[str, Any] | None:
        """Set or clear the MT login from the strategy editor."""
        normalized_login = str(login or "").strip()
        key_hash = hash_deployment_key(raw_key)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE deployments
                SET mt_login = ?, mt_platform = NULL, mt_server = NULL, updated_at = ?
                WHERE key_hash = ?
                """,
                (normalized_login or None, utc_now_iso(), key_hash),
            )
            row = connection.execute(
                "SELECT * FROM deployments WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
        return self._deployment_row(row) if row else None

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
        *,
        account_login: str = "",
        account_server: str = "",
        symbol: str = "",
        timeframe: str = "",
    ) -> dict[str, Any]:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO decisions (
                        id, deployment_id, endpoint, request_id,
                        account_login, account_server, symbol, timeframe,
                        response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        response["decision_id"],
                        deployment_id,
                        endpoint,
                        request_id,
                        account_login,
                        account_server,
                        symbol,
                        timeframe,
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
        server = str(server or "").strip()
        provider = str(provider or "").strip()
        now = utc_now_iso()
        with self._connect() as connection:
            if not server:
                existing_with_server = connection.execute(
                    """
                    SELECT id
                    FROM deployment_accounts
                    WHERE deployment_id = ? AND login = ? AND server != ''
                    ORDER BY last_seen_at DESC
                    LIMIT 1
                    """,
                    (deployment["id"], login),
                ).fetchone()
                if existing_with_server is not None:
                    connection.execute(
                        """
                        UPDATE deployment_accounts
                        SET last_seen_at = ?
                        WHERE id = ?
                        """,
                        (now, existing_with_server["id"]),
                    )
                    return
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
            if server:
                connection.execute(
                    """
                    DELETE FROM deployment_accounts
                    WHERE deployment_id = ? AND login = ? AND server = ''
                    """,
                    (deployment["id"], login),
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
        archived_count = 0
        profit_deals_count = 0
        net_profit_total = 0.0
        affected_buckets: set[tuple[str, str, int]] = set()

        with self._connect() as connection:
            for order in orders:
                order_id = str(order.get("order_id") or order.get("deal_id") or "").strip()
                if not order_id:
                    continue
                archived = connection.execute(
                    """
                    SELECT 1 FROM mt_order_archived_deals
                    WHERE account_login = ? AND account_server = ? AND deal_id = ?
                    """,
                    (account_login, account_server, order_id),
                ).fetchone()
                if archived is not None:
                    archived_count += 1
                    continue
                entry = "out"
                net_profit = _deal_net_profit(order)
                if _is_profit_deal_entry(entry):
                    profit_deals_count += 1
                    net_profit_total += net_profit

                legacy_server_backfill = False
                existing = connection.execute(
                    """
                    SELECT id, deployment_id,
                           COALESCE(NULLIF(close_time, 0), deal_time) close_timestamp
                    FROM mt5_history_deals
                    WHERE account_login = ? AND account_server = ? AND deal_id = ?
                    """,
                    (account_login, account_server, order_id),
                ).fetchone()
                if existing is None and account_server:
                    existing = connection.execute(
                        """
                        SELECT id, deployment_id,
                               COALESCE(NULLIF(close_time, 0), deal_time) close_timestamp
                        FROM mt5_history_deals
                        WHERE deployment_id = ?
                          AND account_login = ?
                          AND account_server = ''
                          AND deal_id = ?
                        """,
                        (deployment_id, account_login, order_id),
                    ).fetchone()
                    legacy_server_backfill = existing is not None
                if existing and int(existing["close_timestamp"] or 0) > 0:
                    old_timestamp = int(existing["close_timestamp"])
                    old_deployment_id = str(existing["deployment_id"] or deployment_id)
                    for bucket_type in ("hour", "day"):
                        affected_buckets.add(
                            (old_deployment_id, bucket_type, _bucket_timestamp(old_timestamp, bucket_type))
                        )
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

                close_timestamp = int(order.get("close_time") or 0)
                if close_timestamp > 0:
                    for bucket_type in ("hour", "day"):
                        affected_buckets.add(
                            (deployment_id, bucket_type, _bucket_timestamp(close_timestamp, bucket_type))
                        )

            for affected_deployment, bucket_type, bucket_start in affected_buckets:
                self._refresh_order_summary_bucket(
                    connection,
                    deployment_id=affected_deployment,
                    bucket_type=bucket_type,
                    bucket_start=bucket_start,
                    now=now,
                )

        return {
            "received_count": len(orders),
            "inserted_count": inserted_count,
            "updated_count": updated_count,
            "archived_count": archived_count,
            "profit_orders_count": profit_deals_count,
            "profit_deals_count": profit_deals_count,
            "net_profit": round(net_profit_total, 2),
        }

    def list_ai_endpoints(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
        owner_type: str = "",
        user_id: str = "",
        selectable_only: bool = False,
        enabled_only: bool = False,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if keyword:
            clauses.append("(name LIKE ? OR model LIKE ? OR base_url LIKE ? OR remark LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        if owner_type:
            clauses.append("owner_type = ?")
            params.append(owner_type)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if enabled_only:
            clauses.append("enabled = 1")
        if selectable_only:
            clauses.append("selectable_by_user = 1")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._paged_query(
            table="ai_endpoints",
            where=where,
            params=params,
            page=page,
            size=size,
            order_by="sort ASC, is_default DESC, updated_at DESC",
            mapper=self._public_ai_endpoint_row,
        )

    def save_ai_endpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        endpoint_id = str(payload.get("id") or f"aie_{uuid4().hex}")
        existing = self.get_private_ai_endpoint(endpoint_id)
        api_key = str(payload.get("api_key") or "")
        if not api_key and existing:
            api_key = str(existing.get("api_key") or "")
        input_price = decimal_string(
            payload.get("input_price_per_million")
            if "input_price_per_million" in payload
            else (existing or {}).get("input_price_per_million"),
        )
        output_price = decimal_string(
            payload.get("output_price_per_million")
            if "output_price_per_million" in payload
            else (existing or {}).get("output_price_per_million"),
        )
        effective_vision_config = {
            "base_url": payload.get("base_url") if "base_url" in payload else (existing or {}).get("base_url"),
            "model": payload.get("model") if "model" in payload else (existing or {}).get("model"),
            "api_key": api_key,
        }
        vision_config_changed = bool(existing) and any(
            str(effective_vision_config[field] or "").strip() != str(existing.get(field) or "").strip()
            for field in effective_vision_config
        )
        supports_vision = bool((existing or {}).get("supports_vision")) and not vision_config_changed
        vision_test_status = str((existing or {}).get("vision_test_status") or "untested") if not vision_config_changed else "untested"
        vision_tested_at = str((existing or {}).get("vision_tested_at") or "") if not vision_config_changed else ""
        vision_test_error = str((existing or {}).get("vision_test_error") or "") if not vision_config_changed else ""
        with self._connect() as connection:
            if payload.get("is_default", False) and str(payload.get("owner_type") or "gl") == "gl":
                connection.execute("UPDATE ai_endpoints SET is_default = 0 WHERE owner_type = 'gl'")
            connection.execute(
                """
                INSERT INTO ai_endpoints (
                    id, owner_type, user_id, template_code, name, base_url, model,
                    api_key, strict_json, context_window, input_token_rate, output_token_rate,
                    billing_multiplier, input_price_per_million, output_price_per_million,
                    supports_vision, vision_test_status, vision_tested_at, vision_test_error,
                    is_default, enabled, selectable_by_user,
                    sort, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner_type = excluded.owner_type,
                    user_id = excluded.user_id,
                    template_code = excluded.template_code,
                    name = excluded.name,
                    base_url = excluded.base_url,
                    model = excluded.model,
                    api_key = excluded.api_key,
                    strict_json = excluded.strict_json,
                    context_window = excluded.context_window,
                    input_token_rate = excluded.input_token_rate,
                    output_token_rate = excluded.output_token_rate,
                    billing_multiplier = excluded.billing_multiplier,
                    input_price_per_million = excluded.input_price_per_million,
                    output_price_per_million = excluded.output_price_per_million,
                    supports_vision = excluded.supports_vision,
                    vision_test_status = excluded.vision_test_status,
                    vision_tested_at = excluded.vision_tested_at,
                    vision_test_error = excluded.vision_test_error,
                    is_default = excluded.is_default,
                    enabled = excluded.enabled,
                    selectable_by_user = excluded.selectable_by_user,
                    sort = excluded.sort,
                    remark = excluded.remark,
                    updated_at = excluded.updated_at
                """,
                (
                    endpoint_id,
                    str(payload.get("owner_type") or "gl"),
                    str(payload.get("user_id") or ""),
                    str(payload.get("template_code") or "openai_compatible"),
                    str(payload.get("name") or "").strip(),
                    str(payload.get("base_url") or "").strip(),
                    str(payload.get("model") or "").strip(),
                    api_key.strip(),
                    1 if payload.get("strict_json", True) else 0,
                    int(payload.get("context_window") or 0),
                    float(payload.get("input_token_rate") or 1),
                    float(payload.get("output_token_rate") or 1),
                    float(payload.get("billing_multiplier") or 1),
                    input_price,
                    output_price,
                    1 if supports_vision else 0,
                    vision_test_status,
                    vision_tested_at,
                    vision_test_error,
                    1 if payload.get("is_default", False) else 0,
                    1 if payload.get("enabled", True) else 0,
                    1 if payload.get("selectable_by_user", False) else 0,
                    int(payload.get("sort") or 9999),
                    str(payload.get("remark") or ""),
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
        endpoint = self.get_ai_endpoint(endpoint_id)
        if endpoint is None:
            raise RuntimeError("ai_endpoint_save_failed")
        return endpoint

    def save_ai_endpoint_vision_test(
        self,
        endpoint_id: str,
        *,
        passed: bool,
        error_message: str = "",
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ai_endpoints
                SET supports_vision = ?, vision_test_status = ?, vision_tested_at = ?,
                    vision_test_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (1 if passed else 0, "passed" if passed else "failed", now, str(error_message or "")[:500], now, endpoint_id),
            )
        endpoint = self.get_ai_endpoint(endpoint_id)
        if endpoint is None:
            raise RuntimeError("ai_endpoint_not_found")
        return endpoint

    def get_ai_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        endpoint = self.get_private_ai_endpoint(endpoint_id)
        return self._mask_ai_endpoint_key(endpoint) if endpoint else None

    def get_private_ai_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        if not endpoint_id:
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,)).fetchone()
        return self._private_ai_endpoint_row(row) if row else None

    def get_default_ai_endpoint(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM ai_endpoints
                WHERE owner_type = 'gl' AND enabled = 1 AND api_key <> ''
                ORDER BY is_default DESC, sort ASC, updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._private_ai_endpoint_row(row) if row else None

    def delete_ai_endpoint(self, endpoint_id: str) -> None:
        with self._connect() as connection:
            official_reference = connection.execute(
                """
                SELECT COUNT(*)
                FROM official_ai_strategies
                WHERE open_ai_endpoint_id = ? OR position_ai_endpoint_id = ?
                """,
                (endpoint_id, endpoint_id),
            ).fetchone()[0]
            deployment_reference = connection.execute(
                """
                SELECT COUNT(*)
                FROM deployments
                WHERE config_json LIKE ?
                """,
                (f'%"{endpoint_id}"%',),
            ).fetchone()[0]
            if int(official_reference or 0) > 0 or int(deployment_reference or 0) > 0:
                raise RuntimeError("ai_endpoint_in_use")
            connection.execute("DELETE FROM ai_endpoints WHERE id = ?", (endpoint_id,))

    def list_ea_downloads(self, *, include_disabled: bool = False) -> dict[str, Any]:
        where = "" if include_disabled else "WHERE enabled = 1"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ea_downloads {where} ORDER BY sort ASC, updated_at DESC"
            ).fetchall()
        return {"list": [self._ea_download_row(row) for row in rows]}

    def save_ea_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        download_id = str(payload.get("id") or f"ead_{uuid4().hex}").strip()
        name = str(payload.get("name") or "").strip()
        oss_url = str(payload.get("oss_url") or "").strip()
        if not name:
            raise RuntimeError("ea_download_name_required")
        if not oss_url:
            raise RuntimeError("ea_download_url_required")
        now = utc_now_iso()
        sort_value = int(payload["sort"]) if payload.get("sort") is not None else 9999
        existing = None
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM ea_downloads WHERE id = ?", (download_id,)).fetchone()
            connection.execute(
                """
                INSERT INTO ea_downloads (
                    id, name, description, oss_url, file_name, file_size,
                    enabled, sort, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    oss_url = excluded.oss_url,
                    file_name = excluded.file_name,
                    file_size = excluded.file_size,
                    enabled = excluded.enabled,
                    sort = excluded.sort,
                    updated_at = excluded.updated_at
                """,
                (
                    download_id,
                    name,
                    str(payload.get("description") or ""),
                    oss_url,
                    str(payload.get("file_name") or ""),
                    max(0, int(payload.get("file_size") or 0)),
                    1 if payload.get("enabled", True) else 0,
                    sort_value,
                    str(existing["created_at"]) if existing else now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM ea_downloads WHERE id = ?", (download_id,)).fetchone()
        return self._ea_download_row(row)

    def delete_ea_download(self, download_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM ea_downloads WHERE id = ?", (download_id,))

    def list_guide_articles(
        self,
        *,
        include_disabled: bool = False,
        include_content: bool = False,
    ) -> dict[str, Any]:
        where = "" if include_disabled else "WHERE enabled = 1"
        columns = "*" if include_content else "id, title, summary, enabled, sort, created_at, updated_at"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM guide_articles {where} ORDER BY sort ASC, updated_at DESC"
            ).fetchall()
        return {"list": [self._guide_article_row(row) for row in rows]}

    def get_guide_article(self, article_id: str, *, include_disabled: bool = False) -> dict[str, Any] | None:
        enabled_clause = "" if include_disabled else "AND enabled = 1"
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM guide_articles WHERE id = ? {enabled_clause}",
                (article_id,),
            ).fetchone()
        return self._guide_article_row(row) if row else None

    def save_guide_article(self, payload: dict[str, Any]) -> dict[str, Any]:
        article_id = str(payload.get("id") or f"guide_{uuid4().hex}").strip()
        title = str(payload.get("title") or "").strip()
        if not title:
            raise RuntimeError("guide_title_required")
        raw_content = payload.get("content")
        if not isinstance(raw_content, list):
            raw_content = []
        content: list[dict[str, str]] = []
        for raw_block in raw_content[:200]:
            if not isinstance(raw_block, dict):
                continue
            block_type = str(raw_block.get("type") or "paragraph").strip()
            if block_type not in {"heading", "paragraph", "image"}:
                continue
            block = {"type": block_type}
            if block_type == "image":
                url = str(raw_block.get("url") or "").strip()
                if not url:
                    continue
                block["url"] = url
                block["caption"] = str(raw_block.get("caption") or "").strip()
            else:
                text = str(raw_block.get("text") or "").strip()
                if not text:
                    continue
                block["text"] = text
            content.append(block)
        now = utc_now_iso()
        sort_value = int(payload["sort"]) if payload.get("sort") is not None else 9999
        with self._connect() as connection:
            existing = connection.execute("SELECT created_at FROM guide_articles WHERE id = ?", (article_id,)).fetchone()
            connection.execute(
                """
                INSERT INTO guide_articles (
                    id, title, summary, content_json, enabled, sort, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    content_json = excluded.content_json,
                    enabled = excluded.enabled,
                    sort = excluded.sort,
                    updated_at = excluded.updated_at
                """,
                (
                    article_id,
                    title,
                    str(payload.get("summary") or "").strip(),
                    json.dumps(content, ensure_ascii=False),
                    1 if payload.get("enabled", True) else 0,
                    sort_value,
                    str(existing["created_at"]) if existing else now,
                    now,
                ),
            )
        article = self.get_guide_article(article_id, include_disabled=True)
        if article is None:
            raise RuntimeError("guide_save_failed")
        return article

    def delete_guide_article(self, article_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM guide_articles WHERE id = ?", (article_id,))

    def list_ai_templates(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if keyword:
            clauses.append("(code LIKE ? OR name LIKE ? OR request_type LIKE ? OR remark LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._paged_query(
            table="ai_templates",
            where=where,
            params=params,
            page=page,
            size=size,
            order_by="enabled DESC, code ASC",
            mapper=self._template_row,
        )

    def save_ai_template(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        code = str(payload.get("code") or "").strip()
        if not code:
            raise RuntimeError("template_code_required")
        existing = self.get_ai_template(code)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_templates (
                    code, name, request_type, enabled, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    request_type = excluded.request_type,
                    enabled = excluded.enabled,
                    remark = excluded.remark,
                    updated_at = excluded.updated_at
                """,
                (
                    code,
                    str(payload.get("name") or "").strip(),
                    str(payload.get("request_type") or "openai_compatible").strip(),
                    1 if payload.get("enabled", True) else 0,
                    str(payload.get("remark") or ""),
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
        template = self.get_ai_template(code)
        if template is None:
            raise RuntimeError("ai_template_save_failed")
        return template

    def get_ai_template(self, code: str) -> dict[str, Any] | None:
        if not code:
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM ai_templates WHERE code = ?", (code,)).fetchone()
        return self._template_row(row) if row else None

    def delete_ai_template(self, code: str) -> None:
        if not code:
            return
        with self._connect() as connection:
            connection.execute("DELETE FROM ai_templates WHERE code = ?", (code,))

    def list_public_ai_model_options(self) -> dict[str, Any]:
        with self._connect() as connection:
            endpoint_rows = connection.execute(
                """
                SELECT *
                FROM ai_endpoints
                WHERE owner_type = 'gl'
                  AND enabled = 1
                  AND api_key <> ''
                  AND selectable_by_user = 1
                ORDER BY is_default DESC, sort ASC, updated_at DESC
                """,
            ).fetchall()
        return {
            "list": [
                {
                    "id": str(row["id"] or ""),
                    "provider_name": str(row["name"] or ""),
                    "provider_type": str(row["template_code"] or "openai_compatible"),
                    "model": str(row["model"] or ""),
                    "display_name": str(row["model"] or row["name"] or ""),
                    "base_url": str(row["base_url"] or ""),
                    "input_price_per_million": decimal_string(row["input_price_per_million"] or 0),
                    "output_price_per_million": decimal_string(row["output_price_per_million"] or 0),
                    "is_default": bool(row["is_default"]),
                    "official_available": True,
                    "supports_vision": bool(row["supports_vision"]),
                    "vision_test_status": str(row["vision_test_status"] or "untested"),
                }
                for row in endpoint_rows
            ],
        }

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

    def get_ai_billing_settings(self) -> dict[str, str]:
        values = {
            "credit_limit": "10.000000",
            "low_balance_threshold": "10.000000",
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT setting_key, setting_value
                FROM system_settings
                WHERE setting_key IN ('ai_credit_limit', 'ai_low_balance_threshold')
                """
            ).fetchall()
        for row in rows:
            if row["setting_key"] == "ai_credit_limit":
                values["credit_limit"] = decimal_string(row["setting_value"], "10")
            elif row["setting_key"] == "ai_low_balance_threshold":
                values["low_balance_threshold"] = decimal_string(row["setting_value"], "10")
        return values

    def get_ai_usage_detail_retention_days(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = ?",
                ("ai_usage_detail_retention_days",),
            ).fetchone()
        try:
            return max(1, min(3650, int(str(row["setting_value"])))) if row else 60
        except (TypeError, ValueError):
            return 60

    def get_order_detail_retention_days(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT setting_value FROM system_settings WHERE setting_key = ?",
                ("order_detail_retention_days",),
            ).fetchone()
        try:
            return max(30, min(3650, int(str(row["setting_value"])))) if row else 365
        except (TypeError, ValueError):
            return 365

    def get_ai_cache_settings(self) -> dict[str, Any]:
        values: dict[str, Any] = {"enabled": True, "ttl_seconds": 120}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT setting_key, setting_value
                FROM system_settings
                WHERE setting_key IN ('ai_cache_enabled', 'ai_cache_ttl_seconds')
                """
            ).fetchall()
        for row in rows:
            if row["setting_key"] == "ai_cache_enabled":
                values["enabled"] = str(row["setting_value"] or "1").strip().lower() not in {"0", "false", "no", "off"}
            elif row["setting_key"] == "ai_cache_ttl_seconds":
                try:
                    values["ttl_seconds"] = max(10, min(3600, int(str(row["setting_value"]))))
                except (TypeError, ValueError):
                    values["ttl_seconds"] = 120
        return values

    def save_ai_cache_settings(self, *, enabled: bool, ttl_seconds: int) -> dict[str, Any]:
        now = utc_now_iso()
        normalized_ttl = max(10, min(3600, int(ttl_seconds)))
        with self._connect() as connection:
            for key, value, remark in (
                ("ai_cache_enabled", "1" if enabled else "0", "AI 相同请求缓存开关"),
                ("ai_cache_ttl_seconds", str(normalized_ttl), "AI 相同请求缓存秒数"),
            ):
                connection.execute(
                    """
                    INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value,
                        remark = excluded.remark,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, remark, now),
                )
        return {"enabled": bool(enabled), "ttl_seconds": normalized_ttl}

    def get_ai_response_cache(self, cache_key: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM ai_response_cache
                WHERE cache_key = ? AND expires_at > ?
                """,
                (cache_key, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE ai_response_cache
                SET hit_count = hit_count + 1, last_hit_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, row["id"]),
            )
        data = dict(row)
        try:
            content = json.loads(str(data.pop("response_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(content, dict):
            return None
        data["content"] = content
        data["hit_count"] = int(data.get("hit_count") or 0) + 1
        return data

    def save_ai_response_cache(self, payload: dict[str, Any], *, ttl_seconds: int) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=max(10, min(3600, int(ttl_seconds))))).isoformat()
        cache_id = str(payload.get("id") or f"aicache_{uuid4().hex}")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_response_cache (
                    id, cache_key, endpoint, provider_id, model_id,
                    response_json, response_preview, input_tokens, output_tokens,
                    total_tokens, hit_count, expires_at, last_hit_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '', ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    endpoint = excluded.endpoint,
                    provider_id = excluded.provider_id,
                    model_id = excluded.model_id,
                    response_json = excluded.response_json,
                    response_preview = excluded.response_preview,
                    input_tokens = excluded.input_tokens,
                    output_tokens = excluded.output_tokens,
                    total_tokens = excluded.total_tokens,
                    hit_count = 0,
                    expires_at = excluded.expires_at,
                    last_hit_at = '',
                    updated_at = excluded.updated_at
                """,
                (
                    cache_id,
                    str(payload.get("cache_key") or ""),
                    str(payload.get("endpoint") or ""),
                    str(payload.get("provider_id") or ""),
                    str(payload.get("model_id") or ""),
                    json.dumps(payload.get("content") or {}, ensure_ascii=False, separators=(",", ":")),
                    str(payload.get("response_preview") or ""),
                    int(payload.get("input_tokens") or 0),
                    int(payload.get("output_tokens") or 0),
                    int(payload.get("total_tokens") or 0),
                    expires_at,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ai_response_cache WHERE cache_key = ?",
                (str(payload.get("cache_key") or ""),),
            ).fetchone()
        return dict(row) if row is not None else {}

    def cleanup_expired_ai_response_cache(self, *, batch_size: int = 5000, max_batches: int = 20) -> int:
        cutoff = utc_now_iso()
        deleted = 0
        normalized_batch = max(1, min(10000, int(batch_size)))
        for _ in range(max_batches):
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT id FROM ai_response_cache WHERE expires_at <= ? ORDER BY expires_at LIMIT ?",
                    (cutoff, normalized_batch),
                ).fetchall()
                ids = [str(row["id"]) for row in rows]
                if not ids:
                    break
                placeholders = ",".join("?" for _ in ids)
                connection.execute(f"DELETE FROM ai_response_cache WHERE id IN ({placeholders})", ids)
                deleted += len(ids)
            if len(ids) < normalized_batch:
                break
        return deleted

    def cleanup_expired_ai_usage_details(self, *, batch_size: int = 5000, max_batches: int = 100) -> int:
        retention_days = self.get_ai_usage_detail_retention_days()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        deleted = 0
        for _ in range(max_batches):
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT id FROM ai_usage_logs WHERE created_at < ? ORDER BY created_at LIMIT ?",
                    (cutoff, max(1, min(10000, int(batch_size)))),
                ).fetchall()
                ids = [str(row["id"]) for row in rows]
                if not ids:
                    break
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"DELETE FROM ai_balance_ledger WHERE entry_type = 'ai_charge' AND reference_id IN ({placeholders})",
                    ids,
                )
                connection.execute(
                    f"DELETE FROM ai_usage_logs WHERE id IN ({placeholders})",
                    ids,
                )
                deleted += len(ids)
            if len(ids) < batch_size:
                break
        return deleted

    def cleanup_expired_order_details(self, *, batch_size: int = 5000, max_batches: int = 100) -> int:
        """Delete old details only after both permanent summary levels exist."""
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=self.get_order_detail_retention_days())).timestamp())
        normalized_batch = max(1, min(10000, int(batch_size)))
        deleted = 0
        for _ in range(max_batches):
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT id, deployment_id, account_login, account_server, symbol,
                           COALESCE(NULLIF(close_time, 0), deal_time) close_timestamp
                    FROM mt5_history_deals
                    WHERE LOWER(entry) IN ('out', 'out_by', 'inout')
                      AND COALESCE(NULLIF(close_time, 0), deal_time) > 0
                      AND COALESCE(NULLIF(close_time, 0), deal_time) < ?
                    ORDER BY COALESCE(NULLIF(close_time, 0), deal_time)
                    LIMIT ?
                    """,
                    (cutoff, normalized_batch),
                ).fetchall()
                if not rows:
                    break
                summary_keys: set[tuple[str, str, str, str, str, int]] = set()
                for row in rows:
                    timestamp = int(row["close_timestamp"] or 0)
                    for bucket_type in ("hour", "day"):
                        summary_keys.add((
                            str(row["deployment_id"]), str(row["account_login"] or ""),
                            str(row["account_server"] or ""), str(row["symbol"] or ""),
                            bucket_type, _bucket_timestamp(timestamp, bucket_type),
                        ))
                existing_keys: set[tuple[str, str, str, str, str, int]] = set()
                bucket_pairs = sorted({(item[4], item[5]) for item in summary_keys})
                for bucket_type, bucket_start in bucket_pairs:
                    summary_rows = connection.execute(
                        """
                        SELECT deployment_id, account_login, account_server, symbol,
                               bucket_type, bucket_start
                        FROM mt_order_time_summaries
                        WHERE bucket_type = ? AND bucket_start = ?
                        """,
                        (bucket_type, bucket_start),
                    ).fetchall()
                    existing_keys.update(
                        (
                            str(item["deployment_id"]), str(item["account_login"] or ""),
                            str(item["account_server"] or ""), str(item["symbol"] or ""),
                            str(item["bucket_type"]), int(item["bucket_start"]),
                        )
                        for item in summary_rows
                    )
                safe_ids: list[str] = []
                safe_rows: list[Any] = []
                for row in rows:
                    timestamp = int(row["close_timestamp"] or 0)
                    base = (
                        str(row["deployment_id"]), str(row["account_login"] or ""),
                        str(row["account_server"] or ""), str(row["symbol"] or ""),
                    )
                    if all(
                        (*base, bucket_type, _bucket_timestamp(timestamp, bucket_type)) in existing_keys
                        for bucket_type in ("hour", "day")
                    ):
                        safe_ids.append(str(row["id"]))
                        safe_rows.append(row)
                if not safe_ids:
                    break
                archived_at = utc_now_iso()
                for row in safe_rows:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO mt_order_archived_deals (
                            account_login, account_server, deal_id, deployment_id,
                            close_time, archived_at
                        ) SELECT account_login, account_server, deal_id, deployment_id,
                                 COALESCE(NULLIF(close_time, 0), deal_time), ?
                          FROM mt5_history_deals WHERE id = ?
                        """,
                        (archived_at, str(row["id"])),
                    )
                placeholders = ",".join("?" for _ in safe_ids)
                connection.execute(f"DELETE FROM mt5_history_deals WHERE id IN ({placeholders})", safe_ids)
                deleted += len(safe_ids)
            if len(rows) < normalized_batch:
                break
        return deleted

    def save_ai_billing_settings(
        self,
        *,
        credit_limit: Decimal,
        low_balance_threshold: Decimal,
    ) -> dict[str, str]:
        now = utc_now_iso()
        rows = (
            ("ai_credit_limit", format(credit_limit, "f"), "官方 AI 默认信用额度（元）"),
            ("ai_low_balance_threshold", format(low_balance_threshold, "f"), "客户端低余额提醒阈值（元）"),
        )
        with self._connect() as connection:
            for key, value, remark in rows:
                connection.execute(
                    """
                    INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value,
                        remark = excluded.remark,
                        updated_at = excluded.updated_at
                    """,
                    (key, value, remark, now),
                )
        return self.get_ai_billing_settings()

    def adjust_ai_balance(
        self,
        *,
        user_id: int,
        amount: Decimal,
        entry_type: str,
        remark: str = "",
        operator_id: str = "admin",
        reference_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        ledger_id = f"aibl_{uuid4().hex}"
        with self._connect() as connection:
            lock_suffix = " FOR UPDATE" if isinstance(connection, MySqlConnection) else ""
            row = connection.execute(
                f"SELECT id, ai_balance FROM users WHERE id = ?{lock_suffix}",
                (user_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("user_not_found")
            balance_before = Decimal(str(row["ai_balance"] or 0))
            balance_after = balance_before + amount
            connection.execute(
                "UPDATE users SET ai_balance = ?, updated_at = ? WHERE id = ?",
                (format(balance_after, "f"), now, user_id),
            )
            connection.execute(
                """
                INSERT INTO ai_balance_ledger (
                    id, user_id, entry_type, amount, balance_before, balance_after,
                    operator_id, reference_id, remark, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ledger_id,
                    user_id,
                    entry_type,
                    format(amount, "f"),
                    format(balance_before, "f"),
                    format(balance_after, "f"),
                    operator_id,
                    reference_id,
                    remark,
                    now,
                ),
            )
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_not_found")
        return {"ledger_id": ledger_id, "user": user}

    def list_ai_balance_ledger(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
        user_id: int | None = None,
        entry_type: str = "",
        exclude_entry_type: str = "",
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("l.user_id = ?")
            params.append(user_id)
        if entry_type:
            clauses.append("l.entry_type = ?")
            params.append(entry_type)
        if exclude_entry_type:
            clauses.append("l.entry_type <> ?")
            params.append(exclude_entry_type)
        if keyword:
            like = f"%{keyword}%"
            keyword_clauses = ["u.email LIKE ?", "u.nickname LIKE ?", "l.remark LIKE ?"]
            params.extend([like, like, like])
            if keyword.isdigit():
                keyword_clauses.append("l.user_id = ?")
                params.append(int(keyword))
            clauses.append(f"({' OR '.join(keyword_clauses)})")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._paged_query_sql(
            count_sql=f"SELECT COUNT(*) FROM ai_balance_ledger l JOIN users u ON u.id = l.user_id {where}",
            list_sql=f"""
                SELECT l.*, u.email, u.nickname
                FROM ai_balance_ledger l
                JOIN users u ON u.id = l.user_id
                {where}
                ORDER BY l.created_at DESC, l.id DESC
                LIMIT ? OFFSET ?
            """,
            params=params,
            page=page,
            size=size,
            mapper=self._balance_ledger_row,
        )

    def get_user_portal_data(self, user_id: int) -> dict[str, Any]:
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_not_found")

        deployments = []
        summary = {
            "strategy_count": 0,
            "active_strategy_count": 0,
            "analysis_count": 0,
            "signal_count": 0,
            "order_count": 0,
            "official_tokens_used": 0,
            "custom_tokens_used": 0,
            "pnl": 0.0,
        }
        for deployment in self.list_web_deployments(str(user_id)):
            config = deployment["config"]
            stats = self.deployment_runtime_stats(deployment["id"])
            official_strategy = self.get_official_ai_strategy(deployment["strategy_code"])
            open_ai_mode = str(config.get("open_ai_mode") or "official")
            position_ai_mode = str(config.get("position_ai_mode") or "official")
            open_ai_endpoint_id = str(config.get("open_ai_endpoint_id") or "")
            position_ai_endpoint_id = str(config.get("position_ai_endpoint_id") or "")
            if official_strategy:
                if open_ai_mode == "official" and not open_ai_endpoint_id:
                    open_ai_endpoint_id = str(official_strategy.get("open_ai_endpoint_id") or "")
                if position_ai_mode == "official" and not position_ai_endpoint_id:
                    position_ai_endpoint_id = str(official_strategy.get("position_ai_endpoint_id") or "")
            open_ai_endpoint = self.get_ai_endpoint(open_ai_endpoint_id)
            position_ai_endpoint = self.get_ai_endpoint(position_ai_endpoint_id)
            item = {
                "id": deployment["id"],
                "deployment_key": str(config.get("deployment_key") or ""),
                "name": str(deployment["strategy_name"] or (official_strategy.get("name") if official_strategy else "") or ""),
                "status": deployment["status"],
                "strategy_code": deployment["strategy_code"],
                "mt_login": str(deployment.get("mt_login") or ""),
                "summary": str(official_strategy.get("summary") or config.get("summary") or "") if official_strategy else str(config.get("summary") or ""),
                "ea_description": str(config.get("ea_description") or ""),
                "strategy_type": str(config.get("strategy_type") or ("custom" if deployment["strategy_code"] == "CUSTOM_AI_V1" else "official")),
                "open_logic": str(config.get("open_logic") or ""),
                "position_logic": str(config.get("position_logic") or ""),
                "open_indicators": list(config.get("open_indicators") or []),
                "position_indicators": list(config.get("position_indicators") or []),
                "open_rule_plan": dict(config.get("open_rule_plan") or {}),
                "position_rule_plan": dict(config.get("position_rule_plan") or {}),
                "rule_engine_version": int(config.get("rule_engine_version") or 0),
                "unsupported_indicators": list(config.get("unsupported_indicators") or []),
                "compile_status": str(config.get("compile_status") or ""),
                "open_ai_mode": open_ai_mode,
                "open_ai_model": str(config.get("open_ai_model") or ""),
                "open_ai_endpoint_id": open_ai_endpoint_id,
                "open_ai_endpoint_name": str(open_ai_endpoint.get("name") or "") if open_ai_endpoint else "",
                "open_ai_base_url": str(config.get("open_ai_base_url") or ""),
                "open_ai_key_configured": bool(str(config.get("open_ai_key") or "").strip()),
                "open_ai_vision_verified": bool(config.get("open_ai_vision_verified", False)),
                "ai_user_configured": bool(config.get("ai_user_configured", False)),
                "position_ai_mode": position_ai_mode,
                "position_ai_model": str(config.get("position_ai_model") or ""),
                "position_ai_endpoint_id": position_ai_endpoint_id,
                "position_ai_endpoint_name": str(position_ai_endpoint.get("name") or "") if position_ai_endpoint else "",
                "position_ai_base_url": str(config.get("position_ai_base_url") or ""),
                "position_ai_key_configured": bool(str(config.get("position_ai_key") or "").strip()),
                "position_ai_vision_verified": bool(config.get("position_ai_vision_verified", False)),
                "open_data_type": str(config.get("open_data_type") or "kline"),
                "open_kline_count": int(config.get("open_kline_count") or 100),
                "open_requested_kline_count": int(config.get("open_requested_kline_count") or config.get("open_kline_count") or 100),
                "position_data_type": str(config.get("position_data_type") or "kline"),
                "position_kline_count": int(config.get("position_kline_count") or 100),
                "position_requested_kline_count": int(config.get("position_requested_kline_count") or config.get("position_kline_count") or 100),
                "call_mode": str(config.get("call_mode") or "bar"),
                "call_val": float(config.get("call_val") or 1),
                "position_size_mode": str(config.get("position_size_mode") or "fixed"),
                "fixed_volume": float(config.get("fixed_volume") or config.get("lot") or 0.01),
                "risk_base_mode": str(config.get("risk_base_mode") or "fixed_loss"),
                "risk_amount": float(config.get("risk_amount") or 0),
                "risk_percent": float(config.get("risk_percent") or 0),
                "allow_add": bool(config.get("allow_add", False)),
                "max_positions": int(config.get("max_positions") or 1),
                "updated_at": deployment["updated_at"],
                **stats,
            }
            deployments.append(item)
            summary["strategy_count"] += 1
            summary["active_strategy_count"] += 1 if deployment["status"] == "active" else 0
            for field in ("analysis_count", "signal_count", "order_count", "official_tokens_used", "custom_tokens_used"):
                summary[field] += int(stats[field] or 0)
            summary["pnl"] = round(float(summary["pnl"]) + float(stats["pnl"] or 0), 2)

        month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._connect() as connection:
            order_summary = connection.execute(
                """
                SELECT
                    COALESCE(SUM(order_count), 0) AS total,
                    COALESCE(SUM(win_count), 0) AS wins,
                    COALESCE(SUM(loss_count), 0) AS losses,
                    COALESCE(SUM(net_profit), 0) AS pnl,
                    COUNT(DISTINCT CASE WHEN symbol <> '' THEN symbol END) AS symbol_count
                FROM mt_order_time_summaries
                WHERE user_id = ? AND bucket_type = 'day'
                """,
                (str(user_id),),
            ).fetchone()
            order_rows = connection.execute(
                """
                SELECT h.*, d.strategy_name, d.id AS deployment_id
                FROM mt5_history_deals h
                JOIN deployments d ON d.id = h.deployment_id
                WHERE d.user_id = ? AND LOWER(h.entry) IN ('out', 'out_by', 'inout')
                ORDER BY COALESCE(NULLIF(h.close_time, 0), h.deal_time) DESC, h.updated_at DESC
                LIMIT 200
                """,
                (str(user_id),),
            ).fetchall()
            usage_summary = connection.execute(
                """
                SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS success_calls,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(official_tokens), 0) AS official_tokens,
                    COALESCE(SUM(custom_tokens), 0) AS custom_tokens,
                    COALESCE(SUM(charged_amount), 0) AS charged_amount
                FROM ai_usage_logs
                WHERE user_id = ? AND created_at >= ?
                """,
                (str(user_id), month_start),
            ).fetchone()
            usage_rows = connection.execute(
                """
                SELECT * FROM ai_usage_logs
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (str(user_id),),
            ).fetchall()
            wallet_totals = connection.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END), 0) AS total_credit,
                    COALESCE(SUM(CASE WHEN amount < 0 AND entry_type <> 'ai_charge' THEN -amount ELSE 0 END), 0) AS manual_debit,
                    (SELECT COALESCE(SUM(charged_amount), 0)
                     FROM ai_usage_monthly_summaries
                     WHERE user_id = ?) AS ai_total_debit
                FROM ai_balance_ledger
                WHERE user_id = ?
                """,
                (str(user_id), user_id),
            ).fetchone()

        orders = []
        for row in order_rows:
            orders.append({
                "order_id": str(row["order_id"] or row["deal_id"] or ""),
                "deployment_id": str(row["deployment_id"] or ""),
                "strategy_name": str(row["strategy_name"] or ""),
                "symbol": str(row["symbol"] or ""),
                "mt_type": str(row["mt_type"] or ""),
                "volume": float(row["volume"] or 0),
                "open_price": float(row["open_price"] or 0),
                "close_price": float(row["close_price"] or row["price"] or 0),
                "profit": float(row["profit"] or 0),
                "commission": float(row["commission"] or 0),
                "swap": float(row["swap"] or 0),
                "net_profit": float(row["net_profit"] or 0),
                "open_time": int(row["open_time"] or 0),
                "close_time": int(row["close_time"] or row["deal_time"] or 0),
                "comment": str(row["comment"] or ""),
            })

        usage = self._with_ai_usage_display_names([self._usage_row(row) for row in usage_rows])
        for item in usage:
            for internal_field in ("provider_called", "response_source", "cache_id"):
                item.pop(internal_field, None)
            for field in ("input_price_snapshot", "output_price_snapshot", "charged_amount", "balance_after"):
                item[field] = None if item.get(field) is None else decimal_string(item.get(field))

        ledger = self.list_ai_balance_ledger(
            page=1,
            size=100,
            user_id=user_id,
            exclude_entry_type="ai_charge",
        )
        total_orders = int(order_summary["total"] or 0) if order_summary else 0
        wins = int(order_summary["wins"] or 0) if order_summary else 0
        losses = int(order_summary["losses"] or 0) if order_summary else 0
        return {
            "user": user,
            "summary": summary,
            "strategies": deployments,
            "orders": {
                "total": total_orders,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / total_orders) * 100, 2) if total_orders else 0,
                "pnl": round(float(order_summary["pnl"] or 0), 2) if order_summary else 0,
                "symbol_count": int(order_summary["symbol_count"] or 0) if order_summary else 0,
                "list": orders,
            },
            "usage": {
                "calls": int(usage_summary["calls"] or 0) if usage_summary else 0,
                "success_calls": int(usage_summary["success_calls"] or 0) if usage_summary else 0,
                "input_tokens": int(usage_summary["input_tokens"] or 0) if usage_summary else 0,
                "output_tokens": int(usage_summary["output_tokens"] or 0) if usage_summary else 0,
                "official_tokens": int(usage_summary["official_tokens"] or 0) if usage_summary else 0,
                "custom_tokens": int(usage_summary["custom_tokens"] or 0) if usage_summary else 0,
                "charged_amount": decimal_string(usage_summary["charged_amount"] if usage_summary else 0),
                "list": usage,
            },
            "wallet": {
                "balance": user["ai_balance"],
                "credit_limit": user["credit_limit"],
                "available_balance": user["available_balance"],
                "low_balance_threshold": user["low_balance_threshold"],
                "balance_warning": user["balance_warning"],
                "credit_exhausted": user["credit_exhausted"],
                "total_credit": decimal_string(wallet_totals["total_credit"] if wallet_totals else 0),
                "total_debit": decimal_string(
                    Decimal(str(wallet_totals["manual_debit"] or 0))
                    + Decimal(str(wallet_totals["ai_total_debit"] or 0))
                    if wallet_totals else 0
                ),
                "ledger": ledger["list"],
            },
        }

    def list_user_orders(
        self,
        *,
        user_id: int,
        page: int,
        size: int,
        deployment_id: str = "",
        deployment_key: str = "",
        symbol: str = "",
        start_at: str = "",
        end_at: str = "",
    ) -> dict[str, Any]:
        normalized_page = max(1, int(page))
        normalized_size = max(1, min(100, int(size)))
        offset = (normalized_page - 1) * normalized_size
        retention_days = self.get_order_detail_retention_days()
        now_ts = int(datetime.now(timezone.utc).timestamp())
        detail_cutoff = now_ts - retention_days * 86400
        time_expr = "COALESCE(NULLIF(h.close_time, 0), h.deal_time)"
        clauses = ["d.user_id = ?", "LOWER(h.entry) IN ('out', 'out_by', 'inout')"]
        params: list[Any] = [str(user_id)]
        start_ts: int | None = None
        end_ts: int | None = None
        if deployment_id:
            clauses.append("h.deployment_id = ?")
            params.append(deployment_id)
        if symbol:
            clauses.append("UPPER(h.symbol) = UPPER(?)")
            params.append(symbol)
        if start_at:
            start_ts = int(datetime.fromisoformat(normalized_utc_iso(start_at)).timestamp())
            clauses.append(f"{time_expr} >= ?")
            params.append(start_ts)
        if end_at:
            end_ts = int(datetime.fromisoformat(normalized_utc_iso(end_at)).timestamp())
            clauses.append(f"{time_expr} <= ?")
            params.append(end_ts)
        where = f"WHERE {' AND '.join(clauses)}"

        deployments = self.list_web_deployments(str(user_id))
        deployment_map = {
            str(item["id"]): {
                "id": str(item["id"]),
                "key": str(item.get("config", {}).get("deployment_key") or ""),
                "name": str(item.get("strategy_name") or ""),
            }
            for item in deployments
        }

        with self._connect() as connection:
            detail_summary_row = connection.execute(
                f"""
                SELECT COUNT(*) total,
                       COALESCE(SUM(CASE WHEN h.net_profit > 0 THEN 1 ELSE 0 END), 0) wins,
                       COALESCE(SUM(CASE WHEN h.net_profit < 0 THEN 1 ELSE 0 END), 0) losses,
                       COALESCE(SUM(h.net_profit), 0) pnl,
                       COUNT(DISTINCT CASE WHEN h.symbol <> '' THEN h.symbol END) symbol_count
                FROM mt5_history_deals h
                JOIN deployments d ON d.id = h.deployment_id
                {where}
                """,
                params,
            ).fetchone()
            rows = connection.execute(
                f"""
                SELECT h.*, d.strategy_name
                FROM mt5_history_deals h
                JOIN deployments d ON d.id = h.deployment_id
                {where}
                ORDER BY {time_expr} DESC, h.updated_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, normalized_size, offset],
            ).fetchall()
            summary_clauses = ["user_id = ?", "bucket_type = 'day'"]
            summary_params: list[Any] = [str(user_id)]
            if deployment_id:
                summary_clauses.append("deployment_id = ?")
                summary_params.append(deployment_id)
            if symbol:
                summary_clauses.append("UPPER(symbol) = UPPER(?)")
                summary_params.append(symbol)
            if start_ts is not None:
                summary_clauses.append("bucket_start >= ?")
                summary_params.append(_bucket_timestamp(start_ts, "day"))
            if end_ts is not None:
                summary_clauses.append("bucket_start <= ?")
                summary_params.append(_bucket_timestamp(end_ts, "day"))
            summary_where = " AND ".join(summary_clauses)
            summary = connection.execute(
                f"""
                SELECT COALESCE(SUM(order_count), 0) total,
                       COALESCE(SUM(win_count), 0) wins,
                       COALESCE(SUM(loss_count), 0) losses,
                       COALESCE(SUM(net_profit), 0) pnl,
                       COUNT(DISTINCT CASE WHEN symbol <> '' THEN symbol END) symbol_count,
                       MIN(bucket_start) first_timestamp,
                       MAX(bucket_start) last_timestamp
                FROM mt_order_time_summaries
                WHERE {summary_where}
                """,
                summary_params,
            ).fetchone()
            summary_first_timestamp = int(summary["first_timestamp"] or 0) if summary else 0
            if start_ts is not None and start_ts >= detail_cutoff:
                summary = detail_summary_row
            symbol_rows = connection.execute(
                """
                SELECT DISTINCT symbol
                FROM mt_order_time_summaries
                WHERE user_id = ? AND bucket_type = 'day' AND symbol <> ''
                ORDER BY symbol
                """,
                (str(user_id),),
            ).fetchall()

            curve_start = start_ts if start_ts is not None else summary_first_timestamp
            curve_end = end_ts if end_ts is not None else now_ts
            span_seconds = max(0, curve_end - curve_start) if curve_start else 10 * 86400 + 1
            if span_seconds <= 3 * 86400 and curve_start >= detail_cutoff:
                curve_granularity = "order"
                curve_rows = connection.execute(
                    f"""
                    SELECT {time_expr} close_timestamp,
                           COALESCE(h.net_profit, 0) change_amount
                    FROM mt5_history_deals h
                    JOIN deployments d ON d.id = h.deployment_id
                    {where}
                    ORDER BY {time_expr}, h.updated_at, h.id
                    """,
                    params,
                ).fetchall()
            else:
                curve_granularity = "hour" if span_seconds <= 10 * 86400 else "day"
                curve_clauses = ["user_id = ?", "bucket_type = ?"]
                curve_params: list[Any] = [str(user_id), curve_granularity]
                if deployment_id:
                    curve_clauses.append("deployment_id = ?")
                    curve_params.append(deployment_id)
                if symbol:
                    curve_clauses.append("UPPER(symbol) = UPPER(?)")
                    curve_params.append(symbol)
                if start_ts is not None:
                    curve_clauses.append("bucket_start >= ?")
                    curve_params.append(_bucket_timestamp(start_ts, curve_granularity))
                if end_ts is not None:
                    curve_clauses.append("bucket_start <= ?")
                    curve_params.append(_bucket_timestamp(end_ts, curve_granularity))
                curve_rows = connection.execute(
                    f"""
                    SELECT bucket_start close_timestamp, COALESCE(SUM(net_profit), 0) change_amount
                    FROM mt_order_time_summaries
                    WHERE {' AND '.join(curve_clauses)}
                    GROUP BY bucket_start
                    ORDER BY bucket_start
                    """,
                    curve_params,
                ).fetchall()

        orders = []
        for row in rows:
            deployment = deployment_map.get(str(row["deployment_id"] or ""), {})
            orders.append({
                "order_id": str(row["order_id"] or row["deal_id"] or ""),
                "deployment_id": str(row["deployment_id"] or ""),
                "deployment_key": str(deployment.get("key") or ""),
                "strategy_name": str(deployment.get("name") or row["strategy_name"] or ""),
                "account_login": str(row["account_login"] or ""),
                "symbol": str(row["symbol"] or ""),
                "mt_type": str(row["mt_type"] or ""),
                "volume": float(row["volume"] or 0),
                "open_price": float(row["open_price"] or 0),
                "close_price": float(row["close_price"] or row["price"] or 0),
                "profit": float(row["profit"] or 0),
                "commission": float(row["commission"] or 0),
                "swap": float(row["swap"] or 0),
                "net_profit": float(row["net_profit"] or 0),
                "open_time": int(row["open_time"] or 0),
                "close_time": int(row["close_time"] or row["deal_time"] or 0),
                "comment": str(row["comment"] or ""),
            })

        curve = []
        cumulative = 0.0
        for row in curve_rows:
            change = float(row["change_amount"] or 0)
            cumulative = round(cumulative + change, 2)
            curve.append({
                "time": datetime.fromtimestamp(int(row["close_timestamp"]), LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
                "change": round(change, 2),
                "pnl": cumulative,
            })

        total = int(detail_summary_row["total"] or 0) if detail_summary_row else 0
        summary_total = int(summary["total"] or 0) if summary else 0
        wins = int(summary["wins"] or 0) if summary else 0
        losses = int(summary["losses"] or 0) if summary else 0
        return {
            "total": total,
            "page": normalized_page,
            "size": normalized_size,
            "pages": max(1, (total + normalized_size - 1) // normalized_size),
            "list": orders,
            "summary": {
                "total": summary_total,
                "wins": wins,
                "losses": losses,
                "win_rate": round((wins / summary_total) * 100, 2) if summary_total else 0,
                "pnl": round(float(summary["pnl"] or 0), 2) if summary else 0,
                "symbol_count": int(summary["symbol_count"] or 0) if summary else 0,
            },
            "curve": curve,
            "curve_granularity": curve_granularity,
            "detail_retention_days": retention_days,
            "filters": {
                "deployments": list(deployment_map.values()),
                "symbols": [str(row["symbol"] or "") for row in symbol_rows],
            },
        }

    def list_users(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
        status: str = "",
        vip_level: int | None = None,
        agent_level: int | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if keyword:
            like = f"%{keyword}%"
            keyword_clauses = ["u.email LIKE ?", "u.nickname LIKE ?", "u.remark LIKE ?"]
            params.extend([like, like, like])
            if keyword.isdigit():
                keyword_clauses.append("u.id = ?")
                params.append(int(keyword))
            clauses.append(f"({' OR '.join(keyword_clauses)})")
        if status:
            clauses.append("u.status = ?")
            params.append(status)
        if vip_level is not None:
            clauses.append("u.vip_level = ?")
            params.append(max(0, int(vip_level)))
        if agent_level is not None:
            clauses.append("u.agent_level = ?")
            params.append(max(0, int(agent_level)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._paged_query_sql(
            count_sql=f"SELECT COUNT(*) FROM users u {where}",
            list_sql=f"""
                SELECT u.*,
                       (SELECT COUNT(*) FROM deployments d WHERE d.user_id = u.id AND d.status <> 'deleted') AS strategy_count,
                       (SELECT COUNT(*) FROM users child WHERE child.referrer_user_id = u.id) AS referral_count,
                       (SELECT setting_value FROM system_settings WHERE setting_key = 'ai_credit_limit') AS credit_limit,
                       (SELECT setting_value FROM system_settings WHERE setting_key = 'ai_low_balance_threshold') AS low_balance_threshold
                FROM users u
                {where}
                ORDER BY u.created_at DESC, u.id DESC
                LIMIT ? OFFSET ?
            """,
            params=params,
            page=page,
            size=size,
            mapper=self._admin_user_row,
        )

    def get_user(self, user_id: int | str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.*,
                       (SELECT COUNT(*) FROM deployments d WHERE d.user_id = u.id AND d.status <> 'deleted') AS strategy_count,
                       (SELECT COUNT(*) FROM users child WHERE child.referrer_user_id = u.id) AS referral_count,
                       (SELECT setting_value FROM system_settings WHERE setting_key = 'ai_credit_limit') AS credit_limit,
                       (SELECT setting_value FROM system_settings WHERE setting_key = 'ai_low_balance_threshold') AS low_balance_threshold
                FROM users u
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()
        return self._user_row(row) if row else None

    def save_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        raw_user_id = str(payload.get("id") or "").strip()
        if raw_user_id and not raw_user_id.isdigit():
            raise RuntimeError("invalid_user_id")
        user_id = int(raw_user_id) if raw_user_id else None
        email = str(payload.get("email") or "").strip().lower() or None
        if user_id is None and not email:
            raise RuntimeError("user_email_required")
        nickname = str(payload.get("nickname") or "").strip()
        status = str(payload.get("status") or "pending_activation").strip()
        if status not in {"pending_activation", "active", "disabled"}:
            raise RuntimeError("invalid_user_status")
        vip_level = max(0, int(payload.get("vip_level") or 0))
        vip_expires_at = str(payload.get("vip_expires_at") or "").strip()
        max_strategy_keys = max(0, int(payload.get("max_strategy_keys", 10) or 0))
        requested_agent_level = payload.get("agent_level")
        agent_level = max(0, int(requested_agent_level or 0))

        try:
            with self._connect() as connection:
                existing = None
                if user_id is not None:
                    existing = connection.execute(
                        "SELECT id, email_verified_at, agent_level, invite_code, remark FROM users WHERE id = ?",
                        (user_id,),
                    ).fetchone()
                if existing and "agent_level" not in payload:
                    agent_level = int(existing["agent_level"] or 0)
                if "remark" in payload:
                    remark = str(payload.get("remark") or "")
                else:
                    remark = str(existing["remark"] or "") if existing else ""
                invite_code = str(existing["invite_code"] or "") if existing else ""
                if agent_level > 0 and not invite_code:
                    invite_code = self._generate_invite_code(connection)
                requested_verified = bool(payload.get("email_verified"))
                verified_at = now if requested_verified else ""
                if existing:
                    if "email_verified" not in payload:
                        verified_at = str(existing["email_verified_at"] or "")
                    connection.execute(
                        """
                        UPDATE users
                        SET email = ?, nickname = ?, status = ?, vip_level = ?,
                            vip_expires_at = ?, max_strategy_keys = ?,
                            agent_level = ?, invite_code = NULLIF(?, ''),
                            email_verified_at = ?, remark = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            email,
                            nickname,
                            status,
                            vip_level,
                            vip_expires_at,
                            max_strategy_keys,
                            agent_level,
                            invite_code,
                            verified_at,
                            remark,
                            now,
                            user_id,
                        ),
                    )
                else:
                    columns = "email, password_hash, nickname, status, vip_level, vip_expires_at, max_strategy_keys, agent_level, invite_code, email_verified_at, last_login_at, remark, created_at, updated_at"
                    values: list[Any] = [
                        email, None, nickname, status, vip_level, vip_expires_at,
                        max_strategy_keys, agent_level, invite_code or None, verified_at, "", remark, now, now,
                    ]
                    if user_id is not None:
                        columns = f"id, {columns}"
                        values.insert(0, user_id)
                    placeholders = ", ".join("?" for _ in values)
                    connection.execute(
                        f"INSERT INTO users ({columns}) VALUES ({placeholders})",
                        values,
                    )
                    if user_id is None:
                        row = connection.execute(
                            "SELECT id FROM users WHERE email = ?",
                            (email,),
                        ).fetchone()
                        user_id = int(row["id"])
        except DatabaseIntegrityError as exc:
            raise RuntimeError("user_email_exists") from exc

        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_save_failed")
        return user

    @staticmethod
    def _generate_invite_code(connection: Any) -> str:
        for _ in range(20):
            code = f"GL{uuid4().hex[:10].upper()}"
            exists = connection.execute("SELECT id FROM users WHERE invite_code = ?", (code,)).fetchone()
            if not exists:
                return code
        raise RuntimeError("invite_code_generation_failed")

    @staticmethod
    def _masked_email(email: str) -> str:
        local, separator, domain = str(email or "").partition("@")
        if not separator:
            return "-"
        visible = local[:2] if len(local) > 1 else local[:1]
        return f"{visible}***@{domain}"

    def get_agent_dashboard(self, user_id: int, *, page: int = 1, size: int = 20) -> dict[str, Any]:
        agent = self.get_user(user_id)
        if agent is None:
            raise RuntimeError("user_not_found")
        if int(agent.get("agent_level") or 0) <= 0:
            raise RuntimeError("agent_required")
        normalized_page = max(1, int(page or 1))
        normalized_size = max(1, min(100, int(size or 20)))
        offset = (normalized_page - 1) * normalized_size
        now = utc_now_iso()
        with self._connect() as connection:
            summary = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active_users,
                       COALESCE(SUM(CASE WHEN vip_level > 0 AND vip_expires_at <> '' AND vip_expires_at > ? THEN 1 ELSE 0 END), 0) AS active_vip_users
                FROM users
                WHERE referrer_user_id = ?
                """,
                (now, user_id),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT id, email, nickname, status, vip_level, vip_expires_at, referred_at, created_at
                FROM users
                WHERE referrer_user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, normalized_size, offset),
            ).fetchall()
        items = []
        for row in rows:
            expires_at = str(row["vip_expires_at"] or "")
            vip_active = int(row["vip_level"] or 0) > 0 and bool(expires_at) and expires_at > now
            items.append({
                "id": int(row["id"]),
                "email": self._masked_email(str(row["email"] or "")),
                "nickname": str(row["nickname"] or ""),
                "status": str(row["status"] or ""),
                "vip_level": int(row["vip_level"] or 0),
                "vip_active": vip_active,
                "vip_expires_at": expires_at,
                "referred_at": str(row["referred_at"] or row["created_at"] or ""),
                "created_at": str(row["created_at"] or ""),
            })
        total = int(summary["total"] or 0) if summary else 0
        return {
            "agent_level": int(agent.get("agent_level") or 0),
            "invite_code": str(agent.get("invite_code") or ""),
            "summary": {
                "total_users": total,
                "active_users": int(summary["active_users"] or 0) if summary else 0,
                "active_vip_users": int(summary["active_vip_users"] or 0) if summary else 0,
            },
            "page": normalized_page,
            "size": normalized_size,
            "pages": max(1, (total + normalized_size - 1) // normalized_size),
            "total": total,
            "list": items,
        }

    def get_auth_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ?",
                (email.strip().lower(),),
            ).fetchone()
        return dict(row) if row else None

    def get_auth_user_by_id(self, user_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def update_user_nickname(self, user_id: int, nickname: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET nickname = ?, updated_at = ? WHERE id = ?",
                (nickname.strip(), utc_now_iso(), user_id),
            )
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_not_found")
        return user

    def update_user_password(self, user_id: int, password_hash: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (password_hash, utc_now_iso(), user_id),
            )
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_not_found")
        return user

    def update_user_email(self, user_id: int, email: str) -> dict[str, Any]:
        now = utc_now_iso()
        try:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE users SET email = ?, email_verified_at = ?, updated_at = ? WHERE id = ?",
                    (email.strip().lower(), now, now, user_id),
                )
        except DatabaseIntegrityError as exc:
            raise RuntimeError("user_email_exists") from exc
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_not_found")
        return user

    def prepare_registration(self, *, email: str, password_hash: str, invite_code: str = "") -> dict[str, Any]:
        now = utc_now_iso()
        normalized = email.strip().lower()
        normalized_invite = str(invite_code or "").strip().upper()
        with self._connect() as connection:
            referrer_id: int | None = None
            if normalized_invite:
                referrer = connection.execute(
                    "SELECT id FROM users WHERE invite_code = ? AND agent_level > 0 AND status = 'active'",
                    (normalized_invite,),
                ).fetchone()
                if not referrer:
                    raise RuntimeError("invalid_invite_code")
                referrer_id = int(referrer["id"])
            row = connection.execute("SELECT * FROM users WHERE email = ?", (normalized,)).fetchone()
            if row and str(row["status"] or "") == "disabled":
                raise RuntimeError("user_disabled")
            if row and row["email_verified_at"]:
                raise RuntimeError("email_already_registered")
            if row:
                existing_referrer_id = row["referrer_user_id"] if "referrer_user_id" in row.keys() else None
                bound_referrer_id = existing_referrer_id or referrer_id
                referred_at = str(row["referred_at"] or "") if "referred_at" in row.keys() else ""
                if bound_referrer_id and not referred_at:
                    referred_at = now
                connection.execute(
                    "UPDATE users SET password_hash = ?, status = 'pending_activation', referrer_user_id = ?, referred_at = ?, updated_at = ? WHERE id = ?",
                    (password_hash, bound_referrer_id, referred_at, now, row["id"]),
                )
                user_id = int(row["id"])
            else:
                connection.execute(
                    """
                    INSERT INTO users (
                        email, password_hash, nickname, status, vip_level,
                        vip_expires_at, max_strategy_keys, referrer_user_id, referred_at, email_verified_at,
                        last_login_at, remark, created_at, updated_at
                    ) VALUES (?, ?, '', 'pending_activation', 0, '', 10, ?, ?, '', '', '', ?, ?)
                    """,
                    (normalized, password_hash, referrer_id, now if referrer_id else "", now, now),
                )
                created = connection.execute("SELECT id FROM users WHERE email = ?", (normalized,)).fetchone()
                user_id = int(created["id"])
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_save_failed")
        return user

    def save_verification_code(
        self,
        *,
        email: str,
        purpose: str,
        code_hash: str,
        expires_at: str,
    ) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                "UPDATE email_verification_codes SET consumed_at = ? WHERE email = ? AND purpose = ? AND consumed_at = ''",
                (now, email, purpose),
            )
            connection.execute(
                """
                INSERT INTO email_verification_codes (
                    id, email, purpose, code_hash, expires_at, consumed_at, attempt_count, created_at
                ) VALUES (?, ?, ?, ?, ?, '', 0, ?)
                """,
                (f"evc_{uuid4().hex}", email, purpose, code_hash, expires_at, now),
            )

    def latest_verification_created_at(self, *, email: str, purpose: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at FROM email_verification_codes WHERE email = ? AND purpose = ? ORDER BY created_at DESC LIMIT 1",
                (email, purpose),
            ).fetchone()
        return str(row["created_at"] or "") if row else ""

    def consume_verification_code(self, *, email: str, purpose: str, code_hash: str) -> str:
        now = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM email_verification_codes
                WHERE email = ? AND purpose = ? AND consumed_at = ''
                ORDER BY created_at DESC LIMIT 1
                """,
                (email, purpose),
            ).fetchone()
            if not row:
                return "invalid"
            if str(row["expires_at"] or "") <= now:
                connection.execute(
                    "UPDATE email_verification_codes SET consumed_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                return "expired"
            attempts = int(row["attempt_count"] or 0)
            if attempts >= 5:
                return "too_many_attempts"
            if str(row["code_hash"] or "") != code_hash:
                connection.execute(
                    "UPDATE email_verification_codes SET attempt_count = attempt_count + 1 WHERE id = ?",
                    (row["id"],),
                )
                return "invalid"
            connection.execute(
                "UPDATE email_verification_codes SET consumed_at = ? WHERE id = ?",
                (now, row["id"]),
            )
        return "ok"

    def activate_user(self, *, email: str, password_hash: str | None = None) -> dict[str, Any]:
        now = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute("SELECT id, status FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                raise RuntimeError("user_not_found")
            if str(row["status"] or "") == "disabled":
                raise RuntimeError("user_disabled")
            if password_hash:
                connection.execute(
                    """
                    UPDATE users SET password_hash = ?, email_verified_at = ?, status = 'active', updated_at = ?
                    WHERE id = ?
                    """,
                    (password_hash, now, now, row["id"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE users SET email_verified_at = ?, status = 'active', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, now, row["id"]),
                )
            user_id = int(row["id"])
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("user_not_found")
        return user

    def create_user_session(self, *, user_id: int, token_hash: str, expires_at: str) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_sessions (id, user_id, token_hash, expires_at, revoked_at, created_at)
                VALUES (?, ?, ?, ?, '', ?)
                """,
                (f"ses_{uuid4().hex}", user_id, token_hash, expires_at, now),
            )
            connection.execute(
                "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                (now, now, user_id),
            )

    def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        now = utc_now_iso()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.*,
                       (SELECT COUNT(*) FROM deployments d WHERE d.user_id = u.id AND d.status <> 'deleted') AS strategy_count
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.revoked_at = '' AND s.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
        return self._user_row(row) if row else None

    def revoke_session(self, token_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at = ''",
                (utc_now_iso(), token_hash),
            )

    def list_user_sessions(self, user_id: int, current_token_hash: str) -> list[dict[str, Any]]:
        now = utc_now_iso()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, expires_at, created_at,
                       CASE WHEN token_hash = ? THEN 1 ELSE 0 END AS is_current
                FROM user_sessions
                WHERE user_id = ? AND revoked_at = '' AND expires_at > ?
                ORDER BY created_at DESC
                """,
                (current_token_hash, user_id, now),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "created_at": str(row["created_at"]),
                "expires_at": str(row["expires_at"]),
                "is_current": bool(row["is_current"]),
            }
            for row in rows
        ]

    def revoke_user_session(self, user_id: int, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM user_sessions WHERE id = ? AND user_id = ? AND revoked_at = ''",
                (session_id, user_id),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE id = ?",
                (utc_now_iso(), session_id),
            )
        return True

    def revoke_other_user_sessions(self, user_id: int, current_token_hash: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE user_sessions SET revoked_at = ?
                WHERE user_id = ? AND token_hash != ? AND revoked_at = ''
                """,
                (utc_now_iso(), user_id, current_token_hash),
            )

    def revoke_all_user_sessions(self, user_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at = ''",
                (utc_now_iso(), user_id),
            )

    def list_ai_usage_logs(
        self,
        *,
        page: int,
        size: int,
        keyword: str = "",
        user_id: str = "",
        model_id: str = "",
        deployment_id: str = "",
        deployment_key: str = "",
        endpoint: str = "",
        billing_source: str = "",
        response_source: str = "",
        success: bool | None = None,
        start_at: str = "",
        end_at: str = "",
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if keyword:
            clauses.append("(l.deployment_id LIKE ? OR l.strategy_code LIKE ? OR l.endpoint LIKE ? OR l.error_message LIKE ? OR l.response_preview LIKE ? OR u.email LIKE ? OR d.strategy_name LIKE ?)")
            like = f"%{keyword}%"
            params.extend([like, like, like, like, like, like, like])
        if user_id:
            clauses.append("l.user_id = ?")
            params.append(user_id)
        if model_id:
            clauses.append("l.model_id = ?")
            params.append(model_id)
        if deployment_id:
            clauses.append("l.deployment_id = ?")
            params.append(deployment_id)
        if deployment_key:
            clauses.append("d.config_json LIKE ?")
            params.append(f'%"deployment_key": "{deployment_key}"%')
        if endpoint:
            clauses.append("l.endpoint = ?")
            params.append(endpoint)
        if billing_source:
            clauses.append("l.billing_source = ?")
            params.append(billing_source)
        if response_source:
            clauses.append("l.response_source = ?")
            params.append(response_source)
        if success is not None:
            clauses.append("l.success = ?")
            params.append(1 if success else 0)
        if start_at:
            clauses.append("l.created_at >= ?")
            params.append(normalized_utc_iso(start_at))
        if end_at:
            clauses.append("l.created_at <= ?")
            params.append(normalized_utc_iso(end_at))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        data = self._paged_query_sql(
            count_sql=f"""
                SELECT COUNT(*)
                FROM ai_usage_logs l
                LEFT JOIN deployments d ON d.id = l.deployment_id
                LEFT JOIN users u ON u.id = l.user_id
                {where}
            """,
            list_sql=f"""
                SELECT l.*, d.strategy_name, d.config_json, u.email user_email, u.nickname user_nickname
                FROM ai_usage_logs l
                LEFT JOIN deployments d ON d.id = l.deployment_id
                LEFT JOIN users u ON u.id = l.user_id
                {where}
                ORDER BY l.created_at DESC
                LIMIT ? OFFSET ?
            """,
            params=params,
            page=page,
            size=size,
            mapper=self._usage_row,
        )
        data["list"] = self._with_ai_usage_display_names(data["list"])
        for item in data["list"]:
            try:
                config = json.loads(str(item.pop("config_json", "") or "{}"))
            except json.JSONDecodeError:
                config = {}
            item["deployment_key"] = str(config.get("deployment_key") or "")
        with self._connect() as connection:
            summary = connection.execute(
                f"""
                SELECT COUNT(*) calls,
                       COALESCE(SUM(CASE WHEN l.success = 1 THEN 1 ELSE 0 END), 0) success_calls,
                       COALESCE(SUM(l.input_tokens), 0) input_tokens,
                       COALESCE(SUM(l.output_tokens), 0) output_tokens,
                       COALESCE(SUM(CASE WHEN l.provider_called = 1 THEN 1 ELSE 0 END), 0) provider_calls,
                       COALESCE(SUM(CASE WHEN l.response_source = 'cache' THEN 1 ELSE 0 END), 0) cache_hits,
                       COALESCE(SUM(CASE WHEN l.provider_called = 1 THEN l.input_tokens ELSE 0 END), 0) provider_input_tokens,
                       COALESCE(SUM(CASE WHEN l.provider_called = 1 THEN l.output_tokens ELSE 0 END), 0) provider_output_tokens,
                       COALESCE(SUM(l.charged_amount), 0) charged_amount
                FROM ai_usage_logs l
                LEFT JOIN deployments d ON d.id = l.deployment_id
                LEFT JOIN users u ON u.id = l.user_id
                {where}
                """,
                params,
            ).fetchone()
        data["summary"] = {
            "calls": int(summary["calls"] or 0),
            "success_calls": int(summary["success_calls"] or 0),
            "input_tokens": int(summary["input_tokens"] or 0),
            "output_tokens": int(summary["output_tokens"] or 0),
            "provider_calls": int(summary["provider_calls"] or 0),
            "cache_hits": int(summary["cache_hits"] or 0),
            "provider_input_tokens": int(summary["provider_input_tokens"] or 0),
            "provider_output_tokens": int(summary["provider_output_tokens"] or 0),
            "charged_amount": decimal_string(summary["charged_amount"] or 0),
        }
        calls = data["summary"]["calls"]
        data["summary"]["cache_hit_rate"] = round((data["summary"]["cache_hits"] / calls) * 100, 2) if calls else 0
        return data

    def list_user_ai_usage(
        self,
        *,
        user_id: int,
        page: int,
        size: int,
        model_id: str = "",
        deployment_id: str = "",
        start_at: str = "",
        end_at: str = "",
    ) -> dict[str, Any]:
        retention_days = self.get_ai_usage_detail_retention_days()
        retention_start = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        requested_start = normalized_utc_iso(start_at) if start_at else ""
        detail_start = max(retention_start, requested_start) if requested_start else retention_start
        clauses = ["user_id = ?", "created_at >= ?"]
        params: list[Any] = [str(user_id), detail_start]
        if model_id:
            clauses.append("model_id = ?")
            params.append(model_id)
        if deployment_id:
            clauses.append("deployment_id = ?")
            params.append(deployment_id)
        if end_at:
            clauses.append("created_at <= ?")
            params.append(normalized_utc_iso(end_at))
        where = f"WHERE {' AND '.join(clauses)}"
        data = self._paged_query_sql(
            count_sql=f"SELECT COUNT(*) FROM ai_usage_logs {where}",
            list_sql=f"SELECT * FROM ai_usage_logs {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params=params,
            page=page,
            size=size,
            mapper=self._usage_row,
        )
        data["list"] = self._with_ai_usage_display_names(data["list"])
        for item in data["list"]:
            for internal_field in ("provider_called", "response_source", "cache_id"):
                item.pop(internal_field, None)

        with self._connect() as connection:
            summary = connection.execute(
                f"""
                SELECT COUNT(*) calls,
                       COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) success_calls,
                       COALESCE(SUM(input_tokens), 0) input_tokens,
                       COALESCE(SUM(output_tokens), 0) output_tokens,
                       COALESCE(SUM(official_tokens), 0) official_tokens,
                       COALESCE(SUM(custom_tokens), 0) custom_tokens,
                       COALESCE(SUM(charged_amount), 0) charged_amount
                FROM ai_usage_logs {where}
                """,
                params,
            ).fetchone()
            model_rows = connection.execute(
                """
                SELECT model_id, MAX(provider_id) provider_id
                FROM ai_usage_monthly_summaries
                WHERE user_id = ? AND model_id <> ''
                GROUP BY model_id
                ORDER BY model_id
                """,
                (str(user_id),),
            ).fetchall()
            lifetime_summary = connection.execute(
                """
                SELECT COALESCE(SUM(calls), 0) calls,
                       COALESCE(SUM(success_calls), 0) success_calls,
                       COALESCE(SUM(input_tokens), 0) input_tokens,
                       COALESCE(SUM(output_tokens), 0) output_tokens,
                       COALESCE(SUM(official_tokens), 0) official_tokens,
                       COALESCE(SUM(custom_tokens), 0) custom_tokens,
                       COALESCE(SUM(charged_amount), 0) charged_amount
                FROM ai_usage_monthly_summaries
                WHERE user_id = ?
                """,
                (str(user_id),),
            ).fetchone()
            monthly_rows = connection.execute(
                """
                SELECT month_key,
                       COALESCE(SUM(calls), 0) calls,
                       COALESCE(SUM(success_calls), 0) success_calls,
                       COALESCE(SUM(input_tokens), 0) input_tokens,
                       COALESCE(SUM(output_tokens), 0) output_tokens,
                       COALESCE(SUM(official_tokens), 0) official_tokens,
                       COALESCE(SUM(custom_tokens), 0) custom_tokens,
                       COALESCE(SUM(charged_amount), 0) charged_amount
                FROM ai_usage_monthly_summaries
                WHERE user_id = ?
                GROUP BY month_key
                ORDER BY month_key DESC
                """,
                (str(user_id),),
            ).fetchall()
            user_row = connection.execute(
                "SELECT ai_balance FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

        deployments = self.list_web_deployments(str(user_id))
        deployment_map = {
            str(item["id"]): {
                "id": str(item["id"]),
                "key": str(item.get("config", {}).get("deployment_key") or ""),
                "name": str(item.get("strategy_name") or ""),
            }
            for item in deployments
        }
        for item in data["list"]:
            deployment = deployment_map.get(str(item.get("deployment_id") or ""), {})
            item["deployment_key"] = deployment.get("key", "")
            item["strategy_name"] = deployment.get("name", "")

        model_items = self._with_ai_usage_display_names([
            {"model_id": str(row["model_id"] or ""), "provider_id": str(row["provider_id"] or "")}
            for row in model_rows
        ])
        normalized_page = max(1, int(page))
        normalized_size = max(1, min(100, int(size)))
        total = int(data["total"] or 0)
        lifetime = lifetime_summary or {}
        return {
            **data,
            "page": normalized_page,
            "size": normalized_size,
            "pages": max(1, (total + normalized_size - 1) // normalized_size),
            "retention_days": retention_days,
            "detail_start_at": retention_start,
            "current_balance": decimal_string(user_row["ai_balance"] if user_row else 0),
            "lifetime_summary": {
                "calls": int(lifetime["calls"] or 0) if lifetime else 0,
                "success_calls": int(lifetime["success_calls"] or 0) if lifetime else 0,
                "input_tokens": int(lifetime["input_tokens"] or 0) if lifetime else 0,
                "output_tokens": int(lifetime["output_tokens"] or 0) if lifetime else 0,
                "official_tokens": int(lifetime["official_tokens"] or 0) if lifetime else 0,
                "custom_tokens": int(lifetime["custom_tokens"] or 0) if lifetime else 0,
                "charged_amount": decimal_string(lifetime["charged_amount"] if lifetime else 0),
            },
            "monthly_bills": [
                {
                    "month": str(row["month_key"] or ""),
                    "calls": int(row["calls"] or 0),
                    "success_calls": int(row["success_calls"] or 0),
                    "input_tokens": int(row["input_tokens"] or 0),
                    "output_tokens": int(row["output_tokens"] or 0),
                    "official_tokens": int(row["official_tokens"] or 0),
                    "custom_tokens": int(row["custom_tokens"] or 0),
                    "charged_amount": decimal_string(row["charged_amount"]),
                }
                for row in monthly_rows
            ],
            "summary": {
                "calls": int(summary["calls"] or 0),
                "success_calls": int(summary["success_calls"] or 0),
                "input_tokens": int(summary["input_tokens"] or 0),
                "output_tokens": int(summary["output_tokens"] or 0),
                "official_tokens": int(summary["official_tokens"] or 0),
                "custom_tokens": int(summary["custom_tokens"] or 0),
                "charged_amount": decimal_string(summary["charged_amount"]),
            },
            "filters": {
                "models": [
                    {
                        "id": str(item.get("model_id") or ""),
                        "name": str(item.get("model_name") or item.get("provider_name") or item.get("model_id") or ""),
                    }
                    for item in model_items
                ],
                "deployments": list(deployment_map.values()),
            },
        }

    def get_user_ai_usage_screenshot_preview_id(self, *, user_id: int, usage_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_usage_logs WHERE id = ? AND user_id = ? LIMIT 1",
                (str(usage_id or "").strip(), str(user_id)),
            ).fetchone()
        if row is None:
            return ""
        return str(self._usage_row(row).get("screenshot_preview_id") or "")

    def save_ai_usage_log(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        log_id = str(payload.get("id") or f"ailog_{uuid4().hex}")
        input_tokens = int(payload.get("input_tokens") or 0)
        output_tokens = int(payload.get("output_tokens") or 0)
        total_tokens = int(payload.get("total_tokens") or (input_tokens + output_tokens))
        billing_source = str(payload.get("billing_source") or "").strip().lower()
        input_price = Decimal(decimal_string(payload.get("input_price_snapshot")))
        output_price = Decimal(decimal_string(payload.get("output_price_snapshot")))
        charged_amount = Decimal("0")
        balance_after: Decimal | None = None
        if billing_source == "official":
            charged_amount = (
                (Decimal(input_tokens) * input_price + Decimal(output_tokens) * output_price)
                / Decimal("1000000")
            ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        official_tokens = (
            int(payload["official_tokens"])
            if payload.get("official_tokens") is not None
            else (total_tokens if billing_source != "custom" else 0)
        )
        custom_tokens = (
            int(payload["custom_tokens"])
            if payload.get("custom_tokens") is not None
            else (total_tokens if billing_source == "custom" else 0)
        )
        provider_called = bool(payload.get("provider_called", True))
        response_source = str(
            payload.get("response_source") or ("provider" if provider_called else "cache")
        ).strip().lower()
        if response_source not in {"provider", "cache", "fallback"}:
            response_source = "provider" if provider_called else "cache"
        cache_id = str(payload.get("cache_id") or "").strip()
        provider_input_tokens = input_tokens if provider_called else 0
        provider_output_tokens = output_tokens if provider_called else 0
        with self._connect() as connection:
            raw_user_id = str(payload.get("user_id") or "").strip()
            if billing_source == "official" and raw_user_id.isdigit():
                lock_suffix = " FOR UPDATE" if isinstance(connection, MySqlConnection) else ""
                user_row = connection.execute(
                    f"SELECT id, ai_balance FROM users WHERE id = ?{lock_suffix}",
                    (int(raw_user_id),),
                ).fetchone()
                if user_row is None:
                    raise RuntimeError("user_not_found")
                balance_after = Decimal(str(user_row["ai_balance"] or 0)) - charged_amount
                if charged_amount > 0:
                    connection.execute(
                        "UPDATE users SET ai_balance = ?, updated_at = ? WHERE id = ?",
                        (format(balance_after, "f"), now, int(raw_user_id)),
                    )
                    connection.execute(
                        """
                        INSERT INTO ai_balance_ledger (
                            id, user_id, entry_type, amount, balance_before, balance_after,
                            operator_id, reference_id, remark, created_at
                        ) VALUES (?, ?, 'ai_charge', ?, ?, ?, 'system', ?, ?, ?)
                        """,
                        (
                            f"aibl_{uuid4().hex}",
                            int(raw_user_id),
                            format(-charged_amount, "f"),
                            format(balance_after + charged_amount, "f"),
                            format(balance_after, "f"),
                            log_id,
                            f"官方 AI 调用 · {str(payload.get('endpoint') or '')} · {str(payload.get('model_id') or '')}",
                            now,
                        ),
                    )
            connection.execute(
                """
                INSERT INTO ai_usage_logs (
                    id, user_id, deployment_id, strategy_code, endpoint,
                    provider_id, model_id, account_login, account_server, symbol, timeframe,
                    input_tokens, output_tokens, total_tokens,
                    official_tokens, custom_tokens, billing_source,
                    input_price_snapshot, output_price_snapshot, charged_amount, balance_after,
                    success, provider_called, response_source, cache_id,
                    error_message, request_snapshot, response_preview, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    str(payload.get("user_id") or ""),
                    str(payload.get("deployment_id") or ""),
                    str(payload.get("strategy_code") or ""),
                    str(payload.get("endpoint") or ""),
                    str(payload.get("provider_id") or ""),
                    str(payload.get("model_id") or ""),
                    str(payload.get("account_login") or ""),
                    str(payload.get("account_server") or ""),
                    str(payload.get("symbol") or ""),
                    str(payload.get("timeframe") or ""),
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    official_tokens,
                    custom_tokens,
                    billing_source,
                    format(input_price, "f"),
                    format(output_price, "f"),
                    format(charged_amount, "f"),
                    None if balance_after is None else format(balance_after, "f"),
                    1 if payload.get("success", True) else 0,
                    1 if provider_called else 0,
                    response_source,
                    cache_id or None,
                    str(payload.get("error_message") or ""),
                    str(payload.get("request_snapshot") or ""),
                    str(payload.get("response_preview") or ""),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_usage_monthly_summaries (
                    user_id, month_key, model_id, provider_id, deployment_id,
                    strategy_code, billing_source, calls, success_calls,
                    provider_calls, cache_hits, input_tokens, output_tokens,
                    provider_input_tokens, provider_output_tokens,
                    official_tokens, custom_tokens,
                    charged_amount, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, month_key, model_id, deployment_id, billing_source) DO UPDATE SET
                    provider_id = excluded.provider_id,
                    strategy_code = excluded.strategy_code,
                    calls = ai_usage_monthly_summaries.calls + excluded.calls,
                    success_calls = ai_usage_monthly_summaries.success_calls + excluded.success_calls,
                    provider_calls = ai_usage_monthly_summaries.provider_calls + excluded.provider_calls,
                    cache_hits = ai_usage_monthly_summaries.cache_hits + excluded.cache_hits,
                    input_tokens = ai_usage_monthly_summaries.input_tokens + excluded.input_tokens,
                    output_tokens = ai_usage_monthly_summaries.output_tokens + excluded.output_tokens,
                    provider_input_tokens = ai_usage_monthly_summaries.provider_input_tokens + excluded.provider_input_tokens,
                    provider_output_tokens = ai_usage_monthly_summaries.provider_output_tokens + excluded.provider_output_tokens,
                    official_tokens = ai_usage_monthly_summaries.official_tokens + excluded.official_tokens,
                    custom_tokens = ai_usage_monthly_summaries.custom_tokens + excluded.custom_tokens,
                    charged_amount = ai_usage_monthly_summaries.charged_amount + excluded.charged_amount,
                    updated_at = excluded.updated_at
                """,
                (
                    raw_user_id,
                    now[:7],
                    str(payload.get("model_id") or ""),
                    str(payload.get("provider_id") or ""),
                    str(payload.get("deployment_id") or ""),
                    str(payload.get("strategy_code") or ""),
                    billing_source,
                    1 if payload.get("success", True) else 0,
                    1 if provider_called else 0,
                    1 if response_source == "cache" else 0,
                    input_tokens,
                    output_tokens,
                    provider_input_tokens,
                    provider_output_tokens,
                    official_tokens,
                    custom_tokens,
                    format(charged_amount, "f"),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM ai_usage_logs WHERE id = ?", (log_id,)).fetchone()
        data = self._usage_row(row)
        enriched = self._with_ai_usage_display_names([data])
        return enriched[0] if enriched else data

    def _with_ai_usage_display_names(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = sorted(
            {
                str(value)
                for row in rows
                for value in (row.get("provider_id"), row.get("model_id"))
                if str(value or "").strip()
            }
        )
        if not ids:
            return rows

        placeholders = ",".join("?" for _ in ids)
        endpoint_map: dict[str, dict[str, Any]] = {}
        provider_map: dict[str, str] = {}
        model_map: dict[str, dict[str, str]] = {}
        deployment_configs: dict[str, dict[str, Any]] = {}
        custom_deployment_ids = sorted(
            {
                str(row.get("deployment_id") or "")
                for row in rows
                if str(row.get("provider_id") or "").startswith("custom_")
                and str(row.get("deployment_id") or "").strip()
            }
        )
        with self._connect() as connection:
            for endpoint in connection.execute(
                f"SELECT id, name, model FROM ai_endpoints WHERE id IN ({placeholders})",
                ids,
            ).fetchall():
                endpoint_map[str(endpoint["id"])] = dict(endpoint)

            for provider in connection.execute(
                f"SELECT id, name FROM ai_providers WHERE id IN ({placeholders})",
                ids,
            ).fetchall():
                provider_map[str(provider["id"])] = str(provider["name"] or "")

            for model in connection.execute(
                f"SELECT id, provider_id, name, display_name FROM ai_models WHERE id IN ({placeholders})",
                ids,
            ).fetchall():
                model_map[str(model["id"])] = {
                    "provider_id": str(model["provider_id"] or ""),
                    "name": str(model["name"] or ""),
                    "display_name": str(model["display_name"] or ""),
                }

            if custom_deployment_ids:
                deployment_placeholders = ",".join("?" for _ in custom_deployment_ids)
                for deployment in connection.execute(
                    f"SELECT id, config_json FROM deployments WHERE id IN ({deployment_placeholders})",
                    custom_deployment_ids,
                ).fetchall():
                    try:
                        config = json.loads(str(deployment["config_json"] or "{}"))
                    except json.JSONDecodeError:
                        config = {}
                    deployment_configs[str(deployment["id"])] = config if isinstance(config, dict) else {}

        custom_model_placeholders = {
            "",
            "custom",
            "custom_ai",
            "custom_model",
            "custom_open",
            "custom_position",
            "自定义模型",
        }
        for row in rows:
            provider_id = str(row.get("provider_id") or "")
            model_id = str(row.get("model_id") or "")
            is_custom_provider = provider_id.startswith("custom_")
            is_custom_model = model_id.startswith("custom_")
            provider_endpoint = endpoint_map.get(provider_id)
            model_endpoint = endpoint_map.get(model_id)
            legacy_model = model_map.get(model_id, {})

            provider_name = (
                str((provider_endpoint or {}).get("name") or "")
                or str((model_endpoint or {}).get("name") or "")
                or provider_map.get(provider_id, "")
                or provider_map.get(legacy_model.get("provider_id", ""), "")
                or provider_id
            )
            model_name = (
                str((model_endpoint or {}).get("model") or "")
                or str((provider_endpoint or {}).get("model") or "")
                or legacy_model.get("display_name", "")
                or legacy_model.get("name", "")
                or model_id
            )
            if is_custom_provider:
                provider_name = "自定义AI"
                custom_model_name = model_id.strip()
                if custom_model_name in custom_model_placeholders:
                    config = deployment_configs.get(str(row.get("deployment_id") or ""), {})
                    endpoint = str(row.get("endpoint") or "")
                    prefix = "position" if endpoint == "position" else "open"
                    custom_model_name = str(config.get(f"{prefix}_ai_model") or "").strip()
                model_name = (
                    custom_model_name
                    if custom_model_name and custom_model_name not in custom_model_placeholders
                    else "自定义模型"
                )

            row["provider_name"] = provider_name
            row["model_name"] = model_name
        return rows

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
    def _normalized_account_items(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        preferred_server_by_login: dict[str, dict[str, Any]] = {}
        for account in accounts:
            login = str(account.get("login") or "")
            server = str(account.get("server") or "")
            if not login or not server:
                continue
            current = preferred_server_by_login.get(login)
            if current is None or str(account.get("last_seen_at") or "") > str(current.get("last_seen_at") or ""):
                preferred_server_by_login[login] = account

        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for account in accounts:
            login = str(account.get("login") or "")
            if not login:
                continue
            server = str(account.get("server") or "")
            preferred = preferred_server_by_login.get(login)
            if not server and preferred is not None:
                server = str(preferred.get("server") or "")
            key = (login, server)
            item = merged.setdefault(
                key,
                {
                    **account,
                    "login": login,
                    "server": server,
                    "provider": "",
                    "last_seen_at": "",
                },
            )
            item["provider"] = str(item.get("provider") or account.get("provider") or "")
            item["last_seen_at"] = max(str(item.get("last_seen_at") or ""), str(account.get("last_seen_at") or ""))
            if preferred is not None:
                item["provider"] = str(item.get("provider") or preferred.get("provider") or "")
                item["account_type"] = item.get("account_type") or preferred.get("account_type")

        return sorted(
            merged.values(),
            key=lambda item: (str(item.get("last_seen_at") or ""), str(item.get("server") or "")),
            reverse=True,
        )

    @staticmethod
    def _format_symbol_set(symbols: Any) -> str:
        if not symbols:
            return ""
        if isinstance(symbols, set):
            values = sorted(str(symbol).strip() for symbol in symbols if str(symbol).strip())
        else:
            values = [str(symbols).strip()]
        return ", ".join(values[:4]) + ("..." if len(values) > 4 else "")

    @staticmethod
    def _private_ai_endpoint_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["is_default"] = bool(data["is_default"])
        data["selectable_by_user"] = bool(data["selectable_by_user"])
        data["strict_json"] = bool(data.get("strict_json", 1))
        data["supports_vision"] = bool(data.get("supports_vision", 0))
        data["input_price_per_million"] = decimal_string(data.get("input_price_per_million"))
        data["output_price_per_million"] = decimal_string(data.get("output_price_per_million"))
        data["provider_id"] = str(data.get("id") or "")
        data["model_id"] = str(data.get("id") or "")
        data["provider_name"] = str(data.get("name") or "")
        data["provider_type"] = str(data.get("template_code") or "openai_compatible")
        data["provider_base_url"] = str(data.get("base_url") or "")
        data["provider_api_key"] = str(data.get("api_key") or "")
        return data

    @classmethod
    def _public_ai_endpoint_row(cls, row: sqlite3.Row | DbRow) -> dict[str, Any]:
        return cls._mask_ai_endpoint_key(cls._private_ai_endpoint_row(row))

    @staticmethod
    def _mask_ai_endpoint_key(data: dict[str, Any]) -> dict[str, Any]:
        public = dict(data)
        api_key = str(public.get("api_key") or public.get("provider_api_key") or "")
        public["api_key_masked"] = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("****" if api_key else "")
        public.pop("api_key", None)
        public.pop("provider_api_key", None)
        return public

    @staticmethod
    def _template_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        return data

    @staticmethod
    def _quota_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["available_tokens"] = int(data["monthly_quota"]) + int(data["extra_quota"]) - int(data["used_tokens"])
        return data

    @staticmethod
    def _balance_ledger_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        data = dict(row)
        for field in ("amount", "balance_before", "balance_after"):
            data[field] = decimal_string(data.get(field))
        return data

    @staticmethod
    def _user_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        data = dict(row)
        data["vip_level"] = int(data.get("vip_level") or 0)
        data["agent_level"] = int(data.get("agent_level") or 0)
        data["referrer_user_id"] = int(data.get("referrer_user_id") or 0) or None
        data["referral_count"] = int(data.get("referral_count") or 0)
        data["invite_code"] = str(data.get("invite_code") or "")
        data["max_strategy_keys"] = int(data.get("max_strategy_keys") or 0)
        data["strategy_count"] = int(data.get("strategy_count") or 0)
        balance = Decimal(decimal_string(data.get("ai_balance")))
        credit_limit = Decimal(decimal_string(data.get("credit_limit"), "10"))
        low_balance_threshold = Decimal(decimal_string(data.get("low_balance_threshold"), "10"))
        data["ai_balance"] = format(balance, "f")
        data["credit_limit"] = format(credit_limit, "f")
        data["available_balance"] = format(balance + credit_limit, "f")
        data["low_balance_threshold"] = format(low_balance_threshold, "f")
        data["balance_warning"] = balance < low_balance_threshold
        data["credit_exhausted"] = balance <= -credit_limit
        expires_at = str(data.get("vip_expires_at") or "")
        vip_level = data["vip_level"]
        vip_expired = vip_level > 0 and not expires_at
        if expires_at:
            try:
                expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=LOCAL_TIMEZONE)
                vip_expired = expires.astimezone(timezone.utc) <= datetime.now(timezone.utc)
            except ValueError:
                vip_expired = False
        data["vip_expired"] = vip_expired
        data["vip_active"] = vip_level > 0 and bool(expires_at) and not vip_expired
        data["email_verified"] = bool(data.get("email_verified_at"))
        data["password_configured"] = bool(data.get("password_hash"))
        data.pop("password_hash", None)
        data.pop("remark", None)
        return data

    @staticmethod
    def _admin_user_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        remark = str(dict(row).get("remark") or "")
        data = SqliteStore._user_row(row)
        data["remark"] = remark
        return data

    @staticmethod
    def _usage_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["success"] = bool(data["success"])
        data["provider_called"] = bool(data.get("provider_called", True))
        for field in ("input_price_snapshot", "output_price_snapshot", "charged_amount"):
            data[field] = decimal_string(data.get(field))
        data["balance_after"] = None if data.get("balance_after") is None else decimal_string(data.get("balance_after"))
        data["screenshot_preview_id"] = ""
        data["screenshot_metadata"] = {}
        try:
            snapshot = json.loads(str(data.get("request_snapshot") or "{}"))
            messages = snapshot.get("messages") if isinstance(snapshot, dict) else []
            user_content = messages[1].get("content") if isinstance(messages, list) and len(messages) > 1 else ""
            payload = json.loads(user_content) if isinstance(user_content, str) else {}
            screenshot = payload.get("screenshot") if isinstance(payload, dict) else {}
            if isinstance(screenshot, dict):
                data["screenshot_metadata"] = screenshot
                data["screenshot_preview_id"] = str(screenshot.get("preview_id") or "")
        except (TypeError, json.JSONDecodeError, IndexError):
            pass
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
    def _ea_download_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        data["file_size"] = int(data.get("file_size") or 0)
        data["sort"] = int(data.get("sort") or 0)
        return data

    @staticmethod
    def _guide_article_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        data["sort"] = int(data.get("sort") or 0)
        if "content_json" in data:
            try:
                content = json.loads(str(data.pop("content_json") or "[]"))
            except (TypeError, json.JSONDecodeError):
                content = []
            data["content"] = content if isinstance(content, list) else []
        return data

    @staticmethod
    def _deployment_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["config"] = json.loads(data.pop("config_json"))
        return data

    @staticmethod
    def _admin_custom_strategy_row(row: sqlite3.Row | DbRow) -> dict[str, Any]:
        data = dict(row)
        try:
            config = json.loads(str(data.pop("config_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            config = {}

        def list_value(key: str) -> list[Any]:
            value = config.get(key)
            return value if isinstance(value, list) else []

        return {
            "id": str(data.get("id") or ""),
            "user_id": str(data.get("user_id") or ""),
            "email": str(data.get("email") or ""),
            "nickname": str(data.get("nickname") or ""),
            "name": str(data.get("strategy_name") or ""),
            "status": str(data.get("status") or ""),
            "deployment_key": str(config.get("deployment_key") or ""),
            "mt_login": str(data.get("mt_login") or ""),
            "symbol": str(data.get("symbol") or ""),
            "timeframe": str(data.get("timeframe") or ""),
            "ea_description": str(config.get("ea_description") or ""),
            "summary": str(config.get("summary") or ""),
            "open_logic": str(config.get("open_logic") or ""),
            "position_logic": str(config.get("position_logic") or ""),
            "open_prompt_template": str(config.get("open_prompt_template") or ""),
            "position_prompt_template": str(config.get("position_prompt_template") or ""),
            "open_indicators": list_value("open_indicators"),
            "position_indicators": list_value("position_indicators"),
            "open_rule_plan": dict(config.get("open_rule_plan") or {}),
            "position_rule_plan": dict(config.get("position_rule_plan") or {}),
            "rule_engine_version": int(config.get("rule_engine_version") or 0),
            "unsupported_conditions": list_value("unsupported_conditions"),
            "unsupported_condition_count": int(
                config.get("unsupported_condition_count") or len(list_value("unsupported_conditions"))
            ),
            "visual_conditions": list_value("visual_conditions"),
            "unsupported_indicators": list_value("unsupported_indicators"),
            "warnings": list_value("warnings"),
            "compile_status": str(config.get("compile_status") or ""),
            "prompt_version": str(config.get("prompt_version") or ""),
            "open_data_type": str(config.get("open_data_type") or "kline"),
            "open_kline_count": int(config.get("open_kline_count") or 0),
            "open_requested_kline_count": int(config.get("open_requested_kline_count") or 0),
            "position_data_type": str(config.get("position_data_type") or "kline"),
            "position_kline_count": int(config.get("position_kline_count") or 0),
            "position_requested_kline_count": int(config.get("position_requested_kline_count") or 0),
            "open_ai_mode": str(config.get("open_ai_mode") or "official"),
            "open_ai_model": str(config.get("open_ai_model") or ""),
            "position_ai_mode": str(config.get("position_ai_mode") or "official"),
            "position_ai_model": str(config.get("position_ai_model") or ""),
            "position_size_mode": str(config.get("position_size_mode") or "fixed"),
            "fixed_volume": float(config.get("fixed_volume") or config.get("lot") or 0),
            "risk_base_mode": str(config.get("risk_base_mode") or "fixed_loss"),
            "risk_amount": float(config.get("risk_amount") or 0),
            "risk_percent": float(config.get("risk_percent") or 0),
            "max_positions": int(config.get("max_positions") or 1),
            "allow_add": bool(config.get("allow_add", False)),
            "created_at": str(data.get("created_at") or ""),
            "updated_at": str(data.get("updated_at") or ""),
        }


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
        self._ensure_mysql_user_table()
        self._ensure_mysql_auth_tables()
        self._ensure_mysql_wallet_tables()
        self._ensure_mysql_ai_cache_table()
        self._ensure_mysql_ea_download_table()
        self._ensure_mysql_guide_article_table()
        self._ensure_mysql_order_summary_table()
        required_tables = {
            "users",
            "email_verification_codes",
            "user_sessions",
            "system_settings",
            "ai_balance_ledger",
            "ai_usage_monthly_summaries",
            "ai_response_cache",
            "deployments",
            "decisions",
            "heartbeats",
            "execution_reports",
            "mt5_history_deals",
            "mt_order_time_summaries",
            "mt_order_archived_deals",
            "ai_templates",
            "ai_endpoints",
            "ai_user_quotas",
            "ai_usage_logs",
            "official_ai_strategies",
            "ea_downloads",
            "guide_articles",
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
        self._ensure_mysql_decision_columns()
        self._ensure_mysql_history_indexes()
        self._ensure_mysql_ai_usage_columns()
        self._ensure_mysql_official_strategy_columns()
        self._ensure_mysql_ai_template_endpoint_seed()
        self._ensure_existing_users()
        self._backfill_ai_usage_monthly_summaries()
        self._backfill_ai_cache_stats()
        self._backfill_order_time_summaries()

    def _ensure_mysql_order_summary_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mt_order_time_summaries (
                    id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    deployment_id VARCHAR(128) NOT NULL,
                    account_login VARCHAR(64) NOT NULL DEFAULT '',
                    account_server VARCHAR(128) NOT NULL DEFAULT '',
                    symbol VARCHAR(32) NOT NULL DEFAULT '',
                    bucket_type VARCHAR(8) NOT NULL,
                    bucket_start BIGINT NOT NULL,
                    order_count BIGINT NOT NULL DEFAULT 0,
                    win_count BIGINT NOT NULL DEFAULT 0,
                    loss_count BIGINT NOT NULL DEFAULT 0,
                    total_volume DECIMAL(24,8) NOT NULL DEFAULT 0,
                    gross_profit DECIMAL(24,8) NOT NULL DEFAULT 0,
                    gross_loss DECIMAL(24,8) NOT NULL DEFAULT 0,
                    commission DECIMAL(24,8) NOT NULL DEFAULT 0,
                    swap DECIMAL(24,8) NOT NULL DEFAULT 0,
                    net_profit DECIMAL(24,8) NOT NULL DEFAULT 0,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_mt_order_summary_dimension (
                        deployment_id, account_login, account_server, symbol, bucket_type, bucket_start
                    ),
                    KEY idx_mt_order_summary_user_bucket (user_id, bucket_type, bucket_start),
                    KEY idx_mt_order_summary_deployment_bucket (deployment_id, bucket_type, bucket_start),
                    CONSTRAINT fk_mt_order_summary_deployment
                        FOREIGN KEY (deployment_id) REFERENCES deployments(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mt_order_archived_deals (
                    account_login VARCHAR(64) NOT NULL,
                    account_server VARCHAR(128) NOT NULL DEFAULT '',
                    deal_id VARCHAR(128) NOT NULL,
                    deployment_id VARCHAR(128) NOT NULL,
                    close_time BIGINT NOT NULL DEFAULT 0,
                    archived_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (account_login, account_server, deal_id),
                    KEY idx_mt_order_archive_deployment (deployment_id, close_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )

    def _ensure_mysql_ea_download_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ea_downloads (
                    id VARCHAR(64) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    description TEXT NOT NULL,
                    oss_url VARCHAR(1000) NOT NULL,
                    file_name VARCHAR(255) NOT NULL DEFAULT '',
                    file_size BIGINT NOT NULL DEFAULT 0,
                    enabled TINYINT(1) NOT NULL DEFAULT 1,
                    sort INT NOT NULL DEFAULT 9999,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_ea_downloads_enabled_sort (enabled, sort)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )

    def _ensure_mysql_guide_article_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guide_articles (
                    id VARCHAR(64) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    summary VARCHAR(1000) NOT NULL DEFAULT '',
                    content_json LONGTEXT NOT NULL,
                    enabled TINYINT(1) NOT NULL DEFAULT 1,
                    sort INT NOT NULL DEFAULT 9999,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_guide_articles_enabled_sort (enabled, sort)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )

    def _ensure_mysql_user_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT NOT NULL AUTO_INCREMENT,
                    email VARCHAR(255) NULL,
                    password_hash VARCHAR(255) NULL,
                    nickname VARCHAR(100) NOT NULL DEFAULT '',
                    status VARCHAR(32) NOT NULL DEFAULT 'pending_activation',
                    vip_level INT NOT NULL DEFAULT 0,
                    vip_expires_at VARCHAR(40) NOT NULL DEFAULT '',
                    max_strategy_keys INT NOT NULL DEFAULT 10,
                    agent_level INT NOT NULL DEFAULT 0,
                    invite_code VARCHAR(32) NULL,
                    referrer_user_id BIGINT NULL,
                    referred_at VARCHAR(40) NOT NULL DEFAULT '',
                    ai_balance DECIMAL(18,6) NOT NULL DEFAULT 0.000000,
                    email_verified_at VARCHAR(40) NOT NULL DEFAULT '',
                    last_login_at VARCHAR(40) NOT NULL DEFAULT '',
                    remark TEXT NULL,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_users_email (email),
                    UNIQUE KEY uk_users_invite_code (invite_code),
                    KEY idx_users_status_vip (status, vip_level),
                    KEY idx_users_referrer (referrer_user_id, created_at),
                    KEY idx_users_created_at (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            rows = connection.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE, EXTRA
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'users'
                """
            ).fetchall()
            columns = {str(row["COLUMN_NAME"]) for row in rows}
            id_row = next((row for row in rows if str(row["COLUMN_NAME"]) == "id"), None)
            id_type = str(id_row.get("DATA_TYPE") or "") if id_row else ""
            id_extra = str(id_row.get("EXTRA") or "") if id_row else ""
            if id_type.lower() != "bigint" or "auto_increment" not in id_extra.lower():
                connection.execute("DELETE FROM users WHERE id NOT REGEXP '^[0-9]+$'")
                connection.execute("ALTER TABLE users MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT")
            migrations = {
                "vip_expires_at": "ALTER TABLE users ADD COLUMN vip_expires_at VARCHAR(40) NOT NULL DEFAULT '' AFTER vip_level",
                "max_strategy_keys": "ALTER TABLE users ADD COLUMN max_strategy_keys INT NOT NULL DEFAULT 10 AFTER vip_expires_at",
                "ai_balance": "ALTER TABLE users ADD COLUMN ai_balance DECIMAL(18,6) NOT NULL DEFAULT 0.000000 AFTER max_strategy_keys",
                "agent_level": "ALTER TABLE users ADD COLUMN agent_level INT NOT NULL DEFAULT 0 AFTER max_strategy_keys",
                "invite_code": "ALTER TABLE users ADD COLUMN invite_code VARCHAR(32) NULL AFTER agent_level",
                "referrer_user_id": "ALTER TABLE users ADD COLUMN referrer_user_id BIGINT NULL AFTER invite_code",
                "referred_at": "ALTER TABLE users ADD COLUMN referred_at VARCHAR(40) NOT NULL DEFAULT '' AFTER referrer_user_id",
                "remark": "ALTER TABLE users ADD COLUMN remark TEXT NULL AFTER last_login_at",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            index_rows = connection.execute("SHOW INDEX FROM users").fetchall()
            indexes = {str(row["Key_name"]) for row in index_rows}
            if "uk_users_invite_code" not in indexes:
                connection.execute("ALTER TABLE users ADD UNIQUE KEY uk_users_invite_code (invite_code)")
            if "idx_users_referrer" not in indexes:
                connection.execute("ALTER TABLE users ADD KEY idx_users_referrer (referrer_user_id, created_at)")

    def _ensure_mysql_wallet_tables(self) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key VARCHAR(64) NOT NULL,
                    setting_value VARCHAR(255) NOT NULL DEFAULT '',
                    remark VARCHAR(255) NOT NULL DEFAULT '',
                    updated_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (setting_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_balance_ledger (
                    id VARCHAR(64) NOT NULL,
                    user_id BIGINT NOT NULL,
                    entry_type VARCHAR(32) NOT NULL,
                    amount DECIMAL(18,6) NOT NULL,
                    balance_before DECIMAL(18,6) NOT NULL,
                    balance_after DECIMAL(18,6) NOT NULL,
                    operator_id VARCHAR(64) NOT NULL DEFAULT '',
                    reference_id VARCHAR(128) NULL,
                    remark VARCHAR(1000) NOT NULL DEFAULT '',
                    created_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_ai_balance_ledger_reference (reference_id),
                    KEY idx_ai_balance_ledger_user_time (user_id, created_at),
                    KEY idx_ai_balance_ledger_type_time (entry_type, created_at),
                    CONSTRAINT fk_ai_balance_ledger_user FOREIGN KEY (user_id) REFERENCES users (id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_usage_monthly_summaries (
                    user_id VARCHAR(64) NOT NULL,
                    month_key CHAR(7) NOT NULL,
                    model_id VARCHAR(128) NOT NULL DEFAULT '',
                    provider_id VARCHAR(128) NOT NULL DEFAULT '',
                    deployment_id VARCHAR(128) NOT NULL DEFAULT '',
                    strategy_code VARCHAR(128) NOT NULL DEFAULT '',
                    billing_source VARCHAR(16) NOT NULL DEFAULT '',
                    calls BIGINT NOT NULL DEFAULT 0,
                    success_calls BIGINT NOT NULL DEFAULT 0,
                    provider_calls BIGINT NOT NULL DEFAULT 0,
                    cache_hits BIGINT NOT NULL DEFAULT 0,
                    input_tokens BIGINT NOT NULL DEFAULT 0,
                    output_tokens BIGINT NOT NULL DEFAULT 0,
                    provider_input_tokens BIGINT NOT NULL DEFAULT 0,
                    provider_output_tokens BIGINT NOT NULL DEFAULT 0,
                    official_tokens BIGINT NOT NULL DEFAULT 0,
                    custom_tokens BIGINT NOT NULL DEFAULT 0,
                    charged_amount DECIMAL(24,6) NOT NULL DEFAULT 0.000000,
                    updated_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (user_id, month_key, model_id, deployment_id, billing_source),
                    KEY idx_ai_usage_monthly_user_month (user_id, month_key)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_key = excluded.setting_key
                """,
                ("ai_credit_limit", "10.000000", "官方 AI 默认信用额度（元）", now),
            )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_key = excluded.setting_key
                """,
                ("ai_low_balance_threshold", "10.000000", "客户端低余额提醒阈值（元）", now),
            )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_key = excluded.setting_key
                """,
                ("ai_usage_detail_retention_days", "60", "AI 调用明细保存天数", now),
            )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_key = excluded.setting_key
                """,
                ("ai_cache_enabled", "1", "AI 相同请求缓存开关", now),
            )
            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_key = excluded.setting_key
                """,
                ("ai_cache_ttl_seconds", "120", "AI 相同请求缓存秒数", now),
            )

            connection.execute(
                """
                INSERT INTO system_settings (setting_key, setting_value, remark, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_key = excluded.setting_key
                """,
                ("order_detail_retention_days", "365", "订单明细保存天数", now),
            )

    def _ensure_mysql_ai_cache_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_response_cache (
                    id VARCHAR(64) NOT NULL,
                    cache_key CHAR(64) NOT NULL,
                    endpoint VARCHAR(16) NOT NULL DEFAULT '',
                    provider_id VARCHAR(128) NOT NULL DEFAULT '',
                    model_id VARCHAR(128) NOT NULL DEFAULT '',
                    response_json LONGTEXT NOT NULL,
                    response_preview TEXT NULL,
                    input_tokens BIGINT NOT NULL DEFAULT 0,
                    output_tokens BIGINT NOT NULL DEFAULT 0,
                    total_tokens BIGINT NOT NULL DEFAULT 0,
                    hit_count BIGINT NOT NULL DEFAULT 0,
                    expires_at VARCHAR(40) NOT NULL,
                    last_hit_at VARCHAR(40) NOT NULL DEFAULT '',
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_ai_response_cache_key (cache_key),
                    KEY idx_ai_response_cache_expires (expires_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )

    def _ensure_mysql_auth_tables(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS email_verification_codes (
                    id VARCHAR(64) NOT NULL,
                    email VARCHAR(255) NOT NULL,
                    purpose VARCHAR(32) NOT NULL,
                    code_hash VARCHAR(128) NOT NULL,
                    expires_at VARCHAR(40) NOT NULL,
                    consumed_at VARCHAR(40) NOT NULL DEFAULT '',
                    attempt_count INT NOT NULL DEFAULT 0,
                    created_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    KEY idx_verification_email_purpose (email, purpose, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_sessions (
                    id VARCHAR(64) NOT NULL,
                    user_id BIGINT NOT NULL,
                    token_hash VARCHAR(128) NOT NULL,
                    expires_at VARCHAR(40) NOT NULL,
                    revoked_at VARCHAR(40) NOT NULL DEFAULT '',
                    created_at VARCHAR(40) NOT NULL,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_user_sessions_token_hash (token_hash),
                    KEY idx_user_sessions_user_id (user_id, created_at),
                    CONSTRAINT fk_user_sessions_user FOREIGN KEY (user_id) REFERENCES users(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )

    def _ensure_mysql_decision_columns(self) -> None:
        migrations = {
            "account_login": "ALTER TABLE decisions ADD COLUMN account_login VARCHAR(64) NOT NULL DEFAULT '' AFTER request_id",
            "account_server": "ALTER TABLE decisions ADD COLUMN account_server VARCHAR(128) NOT NULL DEFAULT '' AFTER account_login",
            "symbol": "ALTER TABLE decisions ADD COLUMN symbol VARCHAR(32) NOT NULL DEFAULT '' AFTER account_server",
            "timeframe": "ALTER TABLE decisions ADD COLUMN timeframe VARCHAR(16) NOT NULL DEFAULT '' AFTER symbol",
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'decisions'
                """,
            ).fetchall()
            columns = {str(row["COLUMN_NAME"]) for row in rows}
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
    def _ensure_mysql_official_strategy_columns(self) -> None:
        migrations = {
            "open_ai_endpoint_id": "ALTER TABLE official_ai_strategies ADD COLUMN open_ai_endpoint_id VARCHAR(64) NOT NULL DEFAULT '' AFTER position_model_id",
            "position_ai_endpoint_id": "ALTER TABLE official_ai_strategies ADD COLUMN position_ai_endpoint_id VARCHAR(64) NOT NULL DEFAULT '' AFTER open_ai_endpoint_id",
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'official_ai_strategies'
                """,
            ).fetchall()
            columns = {str(row["COLUMN_NAME"]) for row in rows}
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)

    def _ensure_mysql_history_indexes(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'mt5_history_deals'
                """
            ).fetchall()
            indexes = {str(row["INDEX_NAME"]) for row in rows}
            history_indexes = {
                "idx_mt5_history_deployment_close": "CREATE INDEX idx_mt5_history_deployment_close ON mt5_history_deals(deployment_id, close_time)",
                "idx_mt5_history_deployment_symbol_close": "CREATE INDEX idx_mt5_history_deployment_symbol_close ON mt5_history_deals(deployment_id, symbol, close_time)",
            }
            for index_name, statement in history_indexes.items():
                if index_name not in indexes:
                    connection.execute(statement)

    def _ensure_mysql_ai_usage_columns(self) -> None:
        migrations = {
            "account_login": "ALTER TABLE ai_usage_logs ADD COLUMN account_login VARCHAR(64) NOT NULL DEFAULT '' AFTER model_id",
            "account_server": "ALTER TABLE ai_usage_logs ADD COLUMN account_server VARCHAR(128) NOT NULL DEFAULT '' AFTER account_login",
            "symbol": "ALTER TABLE ai_usage_logs ADD COLUMN symbol VARCHAR(32) NOT NULL DEFAULT '' AFTER account_server",
            "timeframe": "ALTER TABLE ai_usage_logs ADD COLUMN timeframe VARCHAR(16) NOT NULL DEFAULT '' AFTER symbol",
            "response_preview": "ALTER TABLE ai_usage_logs ADD COLUMN response_preview TEXT NULL AFTER error_message",
            "billing_source": "ALTER TABLE ai_usage_logs ADD COLUMN billing_source VARCHAR(16) NOT NULL DEFAULT '' AFTER custom_tokens",
            "input_price_snapshot": "ALTER TABLE ai_usage_logs ADD COLUMN input_price_snapshot DECIMAL(18,6) NOT NULL DEFAULT 0.000000 AFTER billing_source",
            "output_price_snapshot": "ALTER TABLE ai_usage_logs ADD COLUMN output_price_snapshot DECIMAL(18,6) NOT NULL DEFAULT 0.000000 AFTER input_price_snapshot",
            "charged_amount": "ALTER TABLE ai_usage_logs ADD COLUMN charged_amount DECIMAL(18,6) NOT NULL DEFAULT 0.000000 AFTER output_price_snapshot",
            "balance_after": "ALTER TABLE ai_usage_logs ADD COLUMN balance_after DECIMAL(18,6) NULL AFTER charged_amount",
            "provider_called": "ALTER TABLE ai_usage_logs ADD COLUMN provider_called TINYINT(1) NOT NULL DEFAULT 1 AFTER success",
            "response_source": "ALTER TABLE ai_usage_logs ADD COLUMN response_source VARCHAR(16) NOT NULL DEFAULT 'provider' AFTER provider_called",
            "cache_id": "ALTER TABLE ai_usage_logs ADD COLUMN cache_id VARCHAR(64) NULL AFTER response_source",
            "request_snapshot": "ALTER TABLE ai_usage_logs ADD COLUMN request_snapshot MEDIUMTEXT NULL AFTER error_message",
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'ai_usage_logs'
                """,
            ).fetchall()
            columns = {str(row["COLUMN_NAME"]) for row in rows}
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            index_rows = connection.execute(
                """
                SELECT DISTINCT INDEX_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_usage_logs'
                """
            ).fetchall()
            indexes = {str(row["INDEX_NAME"]) for row in index_rows}
            usage_indexes = {
                "idx_ai_usage_user_time": "CREATE INDEX idx_ai_usage_user_time ON ai_usage_logs(user_id, created_at)",
                "idx_ai_usage_user_model_time": "CREATE INDEX idx_ai_usage_user_model_time ON ai_usage_logs(user_id, model_id, created_at)",
                "idx_ai_usage_user_deployment_time": "CREATE INDEX idx_ai_usage_user_deployment_time ON ai_usage_logs(user_id, deployment_id, created_at)",
            }
            for index_name, statement in usage_indexes.items():
                if index_name not in indexes:
                    connection.execute(statement)
            monthly_rows = connection.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'ai_usage_monthly_summaries'
                """,
            ).fetchall()
            monthly_columns = {str(row["COLUMN_NAME"]) for row in monthly_rows}
            monthly_migrations = {
                "provider_calls": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN provider_calls BIGINT NOT NULL DEFAULT 0 AFTER success_calls",
                "cache_hits": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN cache_hits BIGINT NOT NULL DEFAULT 0 AFTER provider_calls",
                "provider_input_tokens": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN provider_input_tokens BIGINT NOT NULL DEFAULT 0 AFTER output_tokens",
                "provider_output_tokens": "ALTER TABLE ai_usage_monthly_summaries ADD COLUMN provider_output_tokens BIGINT NOT NULL DEFAULT 0 AFTER provider_input_tokens",
            }
            for column, statement in monthly_migrations.items():
                if column not in monthly_columns:
                    connection.execute(statement)

    def _ensure_mysql_ai_template_endpoint_seed(self) -> None:
        now = utc_now_iso()
        endpoint_migrations = {
            "owner_type": "ALTER TABLE ai_endpoints ADD COLUMN owner_type VARCHAR(32) NOT NULL DEFAULT 'gl'",
            "user_id": "ALTER TABLE ai_endpoints ADD COLUMN user_id VARCHAR(64) NOT NULL DEFAULT ''",
            "template_code": "ALTER TABLE ai_endpoints ADD COLUMN template_code VARCHAR(64) NOT NULL DEFAULT 'openai_compatible'",
            "name": "ALTER TABLE ai_endpoints ADD COLUMN name VARCHAR(128) NOT NULL DEFAULT ''",
            "base_url": "ALTER TABLE ai_endpoints ADD COLUMN base_url VARCHAR(512) NOT NULL DEFAULT ''",
            "model": "ALTER TABLE ai_endpoints ADD COLUMN model VARCHAR(128) NOT NULL DEFAULT ''",
            "api_key": "ALTER TABLE ai_endpoints ADD COLUMN api_key VARCHAR(512) NOT NULL DEFAULT ''",
            "strict_json": "ALTER TABLE ai_endpoints ADD COLUMN strict_json TINYINT NOT NULL DEFAULT 1",
            "context_window": "ALTER TABLE ai_endpoints ADD COLUMN context_window INT NOT NULL DEFAULT 0",
            "input_token_rate": "ALTER TABLE ai_endpoints ADD COLUMN input_token_rate DOUBLE NOT NULL DEFAULT 1",
            "output_token_rate": "ALTER TABLE ai_endpoints ADD COLUMN output_token_rate DOUBLE NOT NULL DEFAULT 1",
            "billing_multiplier": "ALTER TABLE ai_endpoints ADD COLUMN billing_multiplier DOUBLE NOT NULL DEFAULT 1",
            "input_price_per_million": "ALTER TABLE ai_endpoints ADD COLUMN input_price_per_million DECIMAL(18,6) NOT NULL DEFAULT 0.000000 AFTER billing_multiplier",
            "output_price_per_million": "ALTER TABLE ai_endpoints ADD COLUMN output_price_per_million DECIMAL(18,6) NOT NULL DEFAULT 0.000000 AFTER input_price_per_million",
            "supports_vision": "ALTER TABLE ai_endpoints ADD COLUMN supports_vision TINYINT NOT NULL DEFAULT 0",
            "vision_test_status": "ALTER TABLE ai_endpoints ADD COLUMN vision_test_status VARCHAR(16) NOT NULL DEFAULT 'untested'",
            "vision_tested_at": "ALTER TABLE ai_endpoints ADD COLUMN vision_tested_at VARCHAR(40) NOT NULL DEFAULT ''",
            "vision_test_error": "ALTER TABLE ai_endpoints ADD COLUMN vision_test_error VARCHAR(500) NOT NULL DEFAULT ''",
            "is_default": "ALTER TABLE ai_endpoints ADD COLUMN is_default TINYINT NOT NULL DEFAULT 0",
            "enabled": "ALTER TABLE ai_endpoints ADD COLUMN enabled TINYINT NOT NULL DEFAULT 1",
            "selectable_by_user": "ALTER TABLE ai_endpoints ADD COLUMN selectable_by_user TINYINT NOT NULL DEFAULT 0",
            "sort": "ALTER TABLE ai_endpoints ADD COLUMN sort INT NOT NULL DEFAULT 9999",
            "remark": "ALTER TABLE ai_endpoints ADD COLUMN remark TEXT NULL",
            "created_at": "ALTER TABLE ai_endpoints ADD COLUMN created_at VARCHAR(40) NOT NULL DEFAULT ''",
            "updated_at": "ALTER TABLE ai_endpoints ADD COLUMN updated_at VARCHAR(40) NOT NULL DEFAULT ''",
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'ai_endpoints'
                """,
            ).fetchall()
            columns = {str(row["COLUMN_NAME"]) for row in rows}
            for column, statement in endpoint_migrations.items():
                if column not in columns:
                    connection.execute(statement)

            connection.execute(
                """
                INSERT INTO ai_templates (
                    code, name, request_type, enabled, remark, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    name = excluded.name,
                    request_type = excluded.request_type,
                    enabled = excluded.enabled,
                    remark = excluded.remark,
                    updated_at = excluded.updated_at
                """,
                (
                    "openai_compatible",
                    "OpenAI 兼容接口",
                    "openai_compatible",
                    1,
                    "适用于 OpenAI、DeepSeek、通义千问兼容模式和大多数中转商。",
                    now,
                    now,
                ),
            )

            count_row = connection.execute("SELECT COUNT(*) FROM ai_endpoints").fetchone()
            if int(count_row[0] if count_row else 0) > 0:
                return
            legacy_rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN ('ai_providers', 'ai_models')
                """
            ).fetchall()
            legacy_tables = {str(row["table_name"]) for row in legacy_rows}
            if {"ai_providers", "ai_models"} - legacy_tables:
                return
            rows = connection.execute(
                """
                SELECT
                    m.*,
                    p.name AS provider_name,
                    p.api_key AS provider_api_key,
                    COALESCE(NULLIF(m.base_url, ''), p.base_url) AS endpoint_base_url
                FROM ai_models m
                JOIN ai_providers p ON p.id = m.provider_id
                WHERE m.enabled = 1 AND p.enabled = 1 AND p.api_key <> ''
                ORDER BY m.is_default DESC, p.sort ASC, m.sort ASC, m.updated_at DESC
                """
            ).fetchall()
            for index, row in enumerate(rows):
                connection.execute(
                    """
                    INSERT INTO ai_endpoints (
                        id, owner_type, user_id, template_code, name, base_url, model, api_key,
                        context_window, input_token_rate, output_token_rate, billing_multiplier,
                        is_default, enabled, selectable_by_user, sort, remark, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"aie_{uuid4().hex}",
                        "gl",
                        "",
                        "openai_compatible",
                        f"{row['provider_name']} / {row['display_name'] or row['name']}",
                        str(row["endpoint_base_url"] or ""),
                        str(row["name"] or ""),
                        str(row["provider_api_key"] or ""),
                        int(row["context_window"] or 0),
                        float(row["input_token_rate"] or 1),
                        float(row["output_token_rate"] or 1),
                        float(row["billing_multiplier"] or 1),
                        1 if (index == 0 or row["is_default"]) else 0,
                        1,
                        1,
                        int(row["sort"] or 9999),
                        str(row["remark"] or ""),
                        now,
                        now,
                    ),
                )

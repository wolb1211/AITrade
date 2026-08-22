from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountIdentity(StrictModel):
    model_config = ConfigDict(extra="ignore")

    platform: Literal["MT4", "MT5"] = "MT5"
    login: str = Field(min_length=1, max_length=64)
    provider: str = Field(default="", max_length=128)
    server: str = Field(default="", max_length=128)
    balance: float = Field(default=0, ge=0)
    equity: float = Field(default=0, ge=0)


class Candle(StrictModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class PositionSnapshot(StrictModel):
    ticket: str
    symbol: str
    side: Literal["BUY", "SELL"]
    volume: float = Field(gt=0)
    open_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    profit: float = 0.0
    sl: float | None = None
    tp: float | None = None
    open_time: int | None = None


class ActivateRequest(StrictModel):
    deployment_key: str = Field(min_length=8, max_length=256)
    account: AccountIdentity
    ea_version: str = Field(default="0.1.0", max_length=32)


class ActivateResponse(StrictModel):
    ok: bool = True
    deployment_id: str
    strategy_code: str
    strategy_name: str
    symbol: str
    timeframe: str
    status: str


class HeartbeatRequest(StrictModel):
    deployment_key: str = Field(min_length=8, max_length=256)
    account: AccountIdentity
    terminal_time: datetime | None = None
    auto_trading_enabled: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatResponse(StrictModel):
    ok: bool = True
    server_time: datetime
    deployment_status: str


class BaseEvaluateRequest(StrictModel):
    deployment_key: str = Field(min_length=8, max_length=256)
    request_id: str = Field(min_length=8, max_length=128)
    account: AccountIdentity
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=16)
    bar_time: int
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    spread_points: float = Field(ge=0)
    candles: list[Candle] = Field(default_factory=list, max_length=1000)
    symbol_info: dict[str, Any] = Field(default_factory=dict)


class OpenEvaluateRequest(BaseEvaluateRequest):
    balance: float = Field(ge=0)
    equity: float = Field(ge=0)


class PositionEvaluateRequest(BaseEvaluateRequest):
    balance: float = Field(default=0, ge=0)
    equity: float = Field(default=0, ge=0)
    positions: list[PositionSnapshot] = Field(min_length=1, max_length=100)


DecisionAction = Literal[
    "BUY",
    "SELL",
    "HOLD",
    "CLOSE",
    "MODIFY_SL",
    "MODIFY_TP",
]


class UsageSummary(StrictModel):
    ai_called: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    charged_points: int = 0


class TradeDecision(StrictModel):
    decision_id: str
    request_id: str
    status: Literal["APPROVED", "HOLD", "REJECTED"]
    action: DecisionAction
    symbol: str
    confidence: float = Field(ge=0, le=1)
    reason: str
    expires_at: datetime
    lot: float | None = None
    entry: float | None = None
    sl: float | None = None
    tp: float | None = None
    position_ticket: str | None = None
    usage: UsageSummary = Field(default_factory=UsageSummary)
    idempotent: bool = False


class ExecutionReportRequest(StrictModel):
    deployment_key: str = Field(min_length=8, max_length=256)
    account: AccountIdentity
    decision_id: str = Field(min_length=8, max_length=128)
    success: bool
    order_id: str | None = None
    deal_id: str | None = None
    error_code: str | None = None
    message: str = Field(default="", max_length=1000)
    executed_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutionReportResponse(StrictModel):
    ok: bool = True
    report_id: str


Mt5DataType = Literal["kline", "screenshot", "both"]
Mt5CallMode = Literal["bar", "timer", "tick", "price_step"]
PositionSizeMode = Literal["fixed", "risk"]
RiskBaseMode = Literal["fixed_loss", "balance_percent"]
Mt5DecisionAction = Literal[
    "none",
    "open",
    "hold",
    "close",
    "add",
    "reduce",
    "modify_sl_tp",
]


class Mt5StrategyInfo(StrictModel):
    id: str
    name: str
    summary: str = ""
    status: str
    open_data_type: Mt5DataType = "kline"
    open_kline_count: int = Field(default=100, ge=1, le=1000)
    position_data_type: Mt5DataType = "kline"
    position_kline_count: int = Field(default=100, ge=1, le=1000)
    call_mode: Mt5CallMode = "bar"
    call_val: float = Field(default=1, ge=0)


class Mt5StrategyInitRequest(StrictModel):
    deployment_key: str = Field(min_length=8, max_length=256)
    account: AccountIdentity
    provider: str = Field(default="", max_length=128)
    ea_version: float | None = Field(default=None, ge=0)


class Mt5StrategyInitResponse(StrictModel):
    status: Literal["ok"]
    protocol_version: float = 1.0
    min_ea_version: float = 1.0
    ea_upgrade_required: bool = False
    strategy: Mt5StrategyInfo


class Mt5Bar(StrictModel):
    time: str | int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Mt5Screenshot(StrictModel):
    mime_type: str = ""
    base64: str = ""


class Mt5MarketSnapshot(StrictModel):
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    spread: float = Field(ge=0)
    bars: list[Mt5Bar] = Field(default_factory=list, max_length=1000)
    screenshot: Mt5Screenshot | None = None
    screenshot_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Mt5BaseDecisionRequest(StrictModel):
    deployment_key: str = Field(min_length=8, max_length=256)
    request_id: str | None = Field(default=None, min_length=8, max_length=128)
    account: AccountIdentity = Field(default_factory=lambda: AccountIdentity(login="unknown"))
    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=16)
    data_type: Mt5DataType = "kline"
    market: Mt5MarketSnapshot


class Mt5OpenDecisionRequest(Mt5BaseDecisionRequest):
    balance: float = Field(default=0, ge=0)
    equity: float = Field(default=0, ge=0)


class Mt5Position(StrictModel):
    trade_type: Literal["position", "pending_order"] | None = None
    ticket: str | int
    symbol: str
    direction: Literal["buy", "sell"] | None = None
    mt_type: int | str = 0
    volume: float = Field(gt=0)
    open_price: float = Field(gt=0)
    current_price: float | None = Field(default=None, gt=0)
    sl: float | None = None
    tp: float | None = None
    profit: float = 0.0
    open_time: int | None = None
    comment: str = ""


class Mt5PositionDecisionRequest(Mt5BaseDecisionRequest):
    balance: float = Field(default=0, ge=0)
    equity: float = Field(default=0, ge=0)
    positions: list[Mt5Position] = Field(min_length=1, max_length=100)


class Mt5OpenOrder(StrictModel):
    direction: Literal["buy", "sell"]
    volume: float = Field(gt=0)
    order_type: Literal["market", "limit", "stop"] = "market"
    price: float = 0.0
    tp: float | None = None
    sl: float | None = None
    comment: str = "GainLabAI"


class Mt5OpenDecisionResponse(StrictModel):
    status: Literal["ok"]
    should_open: bool
    description: str
    spread: float = Field(ge=0)
    decision_id: str
    request_id: str
    orders_count: int = Field(ge=0)
    orders: list[Mt5OpenOrder] = Field(default_factory=list, max_length=100)


class Mt5PositionAction(StrictModel):
    action: Literal["close", "add", "modify", "cancel"]
    ticket: str = ""
    mt_type: int | str = 0
    direction: Literal["buy", "sell"] | None = None
    volume: float = 0.0
    order_type: Literal["market", "limit", "stop"] = "market"
    price: float = 0.0
    sl: float | None = None
    tp: float | None = None
    comment: str = "GainLabAI"


class Mt5PositionDecisionResponse(StrictModel):
    status: Literal["ok"]
    has_action: bool
    description: str
    spread: float = Field(ge=0)
    decision_id: str
    request_id: str
    actions_count: int = Field(ge=0)
    actions: list[Mt5PositionAction] = Field(default_factory=list, max_length=100)


class Mt5HistoryOrder(StrictModel):
    model_config = ConfigDict(extra="ignore")

    order_id: int | str | None = None
    deal_id: int | str | None = None
    symbol: str = Field(min_length=1, max_length=32)
    mt_type: int | str = 0
    volume: float = Field(ge=0)
    open_price: float = Field(ge=0)
    close_price: float = Field(ge=0)
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    open_time: int = Field(ge=0)
    close_time: int = Field(ge=0)
    comment: str = ""


class Mt5HistorySyncRequest(StrictModel):
    model_config = ConfigDict(extra="ignore")

    deployment_key: str = Field(min_length=8, max_length=256)
    login: str = Field(default="", max_length=64)
    orders: list[Mt5HistoryOrder] = Field(default_factory=list, max_length=5000)
    account: AccountIdentity | None = None
    from_time: int | None = Field(default=None, ge=0)
    to_time: int | None = Field(default=None, ge=0)


class Mt5HistorySyncResponse(StrictModel):
    status: Literal["ok"]
    received_count: int = Field(ge=0)
    inserted_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    profit_orders_count: int = Field(default=0, ge=0)
    profit_deals_count: int = Field(ge=0)
    net_profit: float = 0.0


class Mt5DecisionResponse(StrictModel):
    status: Literal["ok"]
    action: Mt5DecisionAction
    reason: str
    decision_id: str
    request_id: str
    confidence: float = Field(ge=0, le=1)
    direction: Literal["buy", "sell"] | None = None
    volume: float | None = None
    sl: float | None = None
    tp: float | None = None
    ticket: str | None = None
    comment: str = "GainLabAI"
    expires_at: datetime
    idempotent: bool = False


class WebDeploymentUpsertRequest(StrictModel):
    deployment_key: str = Field(min_length=8, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    symbol: str | None = Field(default=None, min_length=1, max_length=32)
    timeframe: str | None = Field(default=None, min_length=1, max_length=16)
    status: Literal["active", "paused"] = "paused"
    strategy_code: str = "PA_MOCK_V1"
    user_id: str = "web_demo"
    mt_login: str | None = Field(default=None, max_length=64)
    open_data_type: Mt5DataType = "kline"
    open_kline_count: int = Field(default=100, ge=1, le=1000)
    position_data_type: Mt5DataType = "kline"
    position_kline_count: int = Field(default=100, ge=1, le=1000)
    call_mode: Mt5CallMode = "bar"
    call_val: float = Field(default=1, ge=0)
    position_size_mode: PositionSizeMode = "fixed"
    fixed_volume: float = Field(default=0.01, ge=0)
    risk_base_mode: RiskBaseMode = "fixed_loss"
    risk_amount: float = Field(default=10, ge=0)
    risk_percent: float = Field(default=1, ge=0)
    allow_add: bool = False
    max_positions: int = Field(default=1, ge=1)
    summary: str = ""
    open_logic: str = ""
    position_logic: str = ""
    open_ai_mode: Literal["official", "custom"] = "official"
    open_ai_endpoint_id: str = ""
    open_ai_model: str = ""
    open_ai_base_url: str = ""
    open_ai_key: str = ""
    position_ai_mode: Literal["official", "custom"] = "official"
    position_ai_endpoint_id: str = ""
    position_ai_model: str = ""
    position_ai_base_url: str = ""
    position_ai_key: str = ""


class WebDeploymentUpsertResponse(StrictModel):
    ok: bool = True
    deployment_id: str
    deployment_key: str
    status: str


class WebDeploymentItem(StrictModel):
    id: str
    deployment_key: str
    name: str
    status: Literal["active", "paused"]
    strategy_code: str
    user_id: str
    mt_login: str = ""
    summary: str = ""
    open_logic: str = ""
    position_logic: str = ""
    open_ai_mode: Literal["official", "custom"] = "official"
    open_ai_endpoint_id: str = ""
    open_ai_endpoint_name: str = ""
    open_ai_endpoint_model: str = ""
    open_ai_model: str = ""
    open_ai_base_url: str = ""
    open_ai_key: str = ""
    position_ai_mode: Literal["official", "custom"] = "official"
    position_ai_endpoint_id: str = ""
    position_ai_endpoint_name: str = ""
    position_ai_endpoint_model: str = ""
    position_ai_model: str = ""
    position_ai_base_url: str = ""
    position_ai_key: str = ""
    open_data_type: Mt5DataType = "kline"
    open_kline_count: int = 100
    position_data_type: Mt5DataType = "kline"
    position_kline_count: int = 100
    call_mode: Mt5CallMode = "bar"
    call_val: float = 1
    position_size_mode: PositionSizeMode = "fixed"
    fixed_volume: float = 0.01
    risk_base_mode: RiskBaseMode = "fixed_loss"
    risk_amount: float = 10
    risk_percent: float = 1
    allow_add: bool = False
    max_positions: int = 1
    analysis_count: int = 0
    signal_count: int = 0
    order_count: int = 0
    official_tokens_used: int = 0
    custom_tokens_used: int = 0
    pnl: float = 0.0
    updated_at: str


class WebDeploymentListResponse(StrictModel):
    ok: bool = True
    deployments: list[WebDeploymentItem] = Field(default_factory=list)


class WebDeploymentEquityPoint(StrictModel):
    time: int = 0
    pnl: float = 0.0
    cumulative_pnl: float = 0.0


class WebDeploymentStatsSummary(StrictModel):
    analysis_count: int = 0
    signal_count: int = 0
    order_count: int = 0
    official_tokens_used: int = 0
    custom_tokens_used: int = 0
    pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    flat_count: int = 0
    win_rate: float = 0.0
    traded_symbol_count: int = 0


class WebDeploymentStatsResponse(StrictModel):
    ok: bool = True
    summary: WebDeploymentStatsSummary
    curve: list[WebDeploymentEquityPoint] = Field(default_factory=list)


class WebDeploymentHistoryOrderItem(StrictModel):
    order_id: str
    symbol: str = ""
    mt_type: str = ""
    volume: float = 0.0
    open_price: float = 0.0
    close_price: float = 0.0
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    net_profit: float = 0.0
    open_time: int = 0
    close_time: int = 0
    comment: str = ""


class WebDeploymentHistoryOrdersResponse(StrictModel):
    ok: bool = True
    total: int = 0
    orders: list[WebDeploymentHistoryOrderItem] = Field(default_factory=list)

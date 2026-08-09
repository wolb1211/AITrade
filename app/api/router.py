from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from hashlib import sha256

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.models import (
    ActivateRequest,
    ActivateResponse,
    AccountIdentity,
    Candle,
    ExecutionReportRequest,
    ExecutionReportResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    Mt5Bar,
    Mt5DecisionResponse,
    Mt5HistorySyncRequest,
    Mt5HistorySyncResponse,
    Mt5OpenDecisionResponse,
    Mt5OpenOrder,
    Mt5OpenDecisionRequest,
    Mt5Position,
    Mt5PositionAction,
    Mt5PositionDecisionRequest,
    Mt5PositionDecisionResponse,
    Mt5StrategyInfo,
    Mt5StrategyInitRequest,
    Mt5StrategyInitResponse,
    OpenEvaluateRequest,
    PositionEvaluateRequest,
    PositionSnapshot,
    TradeDecision,
    WebDeploymentItem,
    WebDeploymentHistoryOrdersResponse,
    WebDeploymentListResponse,
    WebDeploymentStatsResponse,
    WebDeploymentUpsertRequest,
    WebDeploymentUpsertResponse,
)
from app.services.decision_service import DecisionService
from app.store import SqliteStore


class AsciiJSONResponse(JSONResponse):
    media_type = "application/json; charset=utf-8"

    def render(self, content: object) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")


def create_api_router(
    store: SqliteStore,
    decision_service: DecisionService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.post("/ea/activate", response_model=ActivateResponse)
    def activate(request: ActivateRequest) -> ActivateResponse:
        deployment = store.activate_deployment(
            request.deployment_key,
            platform=request.account.platform,
            login=request.account.login,
            server=request.account.server,
        )
        if deployment is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_deployment_key",
            )
        return ActivateResponse(
            deployment_id=deployment["id"],
            strategy_code=deployment["strategy_code"],
            strategy_name=deployment["strategy_name"],
            symbol=deployment["symbol"],
            timeframe=deployment["timeframe"],
            status=deployment["status"],
        )

    @router.post("/ea/heartbeat", response_model=HeartbeatResponse)
    def heartbeat(request: HeartbeatRequest) -> HeartbeatResponse:
        deployment = decision_service.authenticate(
            request.deployment_key,
            request.account,
        )
        store.save_heartbeat(
            deployment["id"],
            request.model_dump(mode="json", exclude={"deployment_key"}),
        )
        return HeartbeatResponse(
            server_time=datetime.now(timezone.utc),
            deployment_status=deployment["status"],
        )

    @router.post(
        "/trading/open/evaluate",
        response_model=TradeDecision,
    )
    def evaluate_open(request: OpenEvaluateRequest) -> TradeDecision:
        return decision_service.evaluate_open(request)

    @router.post(
        "/trading/position/evaluate",
        response_model=TradeDecision,
    )
    def evaluate_position(request: PositionEvaluateRequest) -> TradeDecision:
        return decision_service.evaluate_position(request)

    @router.post(
        "/executions/report",
        response_model=ExecutionReportResponse,
    )
    def report_execution(
        request: ExecutionReportRequest,
    ) -> ExecutionReportResponse:
        deployment = decision_service.authenticate(
            request.deployment_key,
            request.account,
        )
        report_id = store.save_execution_report(
            deployment["id"],
            request.model_dump(mode="json", exclude={"deployment_key"}),
        )
        return ExecutionReportResponse(report_id=report_id)

    @router.get("/web/ai-model-options")
    def web_ai_model_options() -> dict[str, object]:
        return {
            "ok": True,
            **store.list_public_ai_model_options(),
        }

    @router.post(
        "/web/deployments/upsert",
        response_model=WebDeploymentUpsertResponse,
    )
    def upsert_web_deployment(
        request: WebDeploymentUpsertRequest,
    ) -> WebDeploymentUpsertResponse:
        deployment = store.upsert_web_deployment(
            request.deployment_key,
            user_id=request.user_id,
            strategy_code=request.strategy_code,
            strategy_name=request.name,
            status=request.status,
            symbol=(request.symbol or "*").upper(),
            timeframe=(request.timeframe or "*").upper(),
            config={
                "lot": request.fixed_volume,
                "sl_distance": 5.0,
                "tp_distance": 8.0,
                "max_loss_per_position": 100.0,
                "take_profit_per_position": 150.0,
                "deployment_key": request.deployment_key,
                "open_data_type": request.open_data_type,
                "open_kline_count": request.open_kline_count,
                "position_data_type": request.position_data_type,
                "position_kline_count": request.position_kline_count,
                "call_mode": request.call_mode,
                "call_val": request.call_val,
                "position_size_mode": request.position_size_mode,
                "fixed_volume": request.fixed_volume,
                "risk_base_mode": request.risk_base_mode,
                "risk_amount": request.risk_amount,
                "risk_percent": request.risk_percent,
                "allow_add": request.allow_add,
                "max_positions": request.max_positions,
                "summary": request.summary,
                "open_logic": request.open_logic,
                "position_logic": request.position_logic,
                "open_ai_mode": request.open_ai_mode,
                "open_ai_provider": request.open_ai_provider,
                "open_ai_model": request.open_ai_model,
                "open_ai_key": request.open_ai_key,
                "position_ai_mode": request.position_ai_mode,
                "position_ai_provider": request.position_ai_provider,
                "position_ai_model": request.position_ai_model,
                "position_ai_key": request.position_ai_key,
            },
        )
        return WebDeploymentUpsertResponse(
            deployment_id=deployment["id"],
            deployment_key=request.deployment_key,
            status=deployment["status"],
        )

    @router.get(
        "/web/deployments",
        response_model=WebDeploymentListResponse,
    )
    def list_web_deployments(
        user_id: str = Query(default="web_demo", min_length=1, max_length=128),
    ) -> WebDeploymentListResponse:
        deployments = []
        for deployment in store.list_web_deployments(user_id):
            config = deployment["config"]
            stats = store.deployment_runtime_stats(deployment["id"])
            deployments.append(
                WebDeploymentItem(
                    id=deployment["id"],
                    deployment_key=config["deployment_key"],
                    name=deployment["strategy_name"],
                    status=deployment["status"],
                    strategy_code=deployment["strategy_code"],
                    user_id=deployment["user_id"],
                    summary=config.get("summary", ""),
                    open_logic=config.get("open_logic", ""),
                    position_logic=config.get("position_logic", ""),
                    open_ai_mode=config.get("open_ai_mode", "official"),
                    open_ai_provider=config.get("open_ai_provider", ""),
                    open_ai_model=config.get("open_ai_model", ""),
                    open_ai_key=config.get("open_ai_key", ""),
                    position_ai_mode=config.get("position_ai_mode", "official"),
                    position_ai_provider=config.get("position_ai_provider", ""),
                    position_ai_model=config.get("position_ai_model", ""),
                    position_ai_key=config.get("position_ai_key", ""),
                    open_data_type=config.get("open_data_type", "kline"),
                    open_kline_count=int(config.get("open_kline_count", 100)),
                    position_data_type=config.get("position_data_type", "kline"),
                    position_kline_count=int(config.get("position_kline_count", 100)),
                    call_mode=config.get("call_mode", "bar"),
                    call_val=float(config.get("call_val", 1)),
                    position_size_mode=config.get("position_size_mode", "fixed"),
                    fixed_volume=float(config.get("fixed_volume", config.get("lot", 0.01))),
                    risk_base_mode=config.get("risk_base_mode", "fixed_loss"),
                    risk_amount=float(config.get("risk_amount", 10)),
                    risk_percent=float(config.get("risk_percent", 1)),
                    allow_add=bool(config.get("allow_add", False)),
                    max_positions=int(config.get("max_positions", 1)),
                    analysis_count=stats["analysis_count"],
                    signal_count=stats["signal_count"],
                    order_count=stats["order_count"],
                    official_tokens_used=stats["official_tokens_used"],
                    custom_tokens_used=stats["custom_tokens_used"],
                    pnl=stats["pnl"],
                    updated_at=deployment["updated_at"],
                ),
            )
        return WebDeploymentListResponse(deployments=deployments)

    @router.get(
        "/web/deployments/stats",
        response_model=WebDeploymentStatsResponse,
    )
    def web_deployment_stats(
        deployment_key: str = Query(min_length=8, max_length=256),
        user_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> WebDeploymentStatsResponse:
        deployment = store.find_deployment_by_key(deployment_key)
        if deployment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment_not_found")
        if user_id and deployment["user_id"] != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment_not_found")
        stats = store.deployment_detail_stats(deployment["id"])
        return WebDeploymentStatsResponse(**stats)

    @router.get(
        "/web/deployments/orders",
        response_model=WebDeploymentHistoryOrdersResponse,
    )
    def web_deployment_orders(
        deployment_key: str = Query(min_length=8, max_length=256),
        user_id: str | None = Query(default=None, min_length=1, max_length=128),
        page: int = Query(default=1, ge=1),
        size: int = Query(default=100, ge=1, le=500),
    ) -> WebDeploymentHistoryOrdersResponse:
        deployment = store.find_deployment_by_key(deployment_key)
        if deployment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment_not_found")
        if user_id and deployment["user_id"] != user_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="deployment_not_found")
        result = store.list_deployment_history_orders(deployment["id"], page=page, size=size)
        return WebDeploymentHistoryOrdersResponse(**result)

    return router


def create_mt5_router(
    store: SqliteStore,
    decision_service: DecisionService,
) -> APIRouter:
    router = APIRouter(prefix="/mt5/strategy", default_response_class=AsciiJSONResponse)

    @router.post("/init", response_model=Mt5StrategyInitResponse)
    def init_strategy(request: Mt5StrategyInitRequest) -> Mt5StrategyInitResponse:
        return _init_strategy_by_key(
            request.deployment_key,
            account=request.account,
            provider=request.provider,
        )

    @router.get("/init", response_model=Mt5StrategyInitResponse)
    def init_strategy_get(
        deployment_key: str = Query(min_length=8, max_length=256),
    ) -> Mt5StrategyInitResponse:
        return _init_strategy_by_key(deployment_key)

    def _init_strategy_by_key(
        raw_key: str,
        *,
        account: AccountIdentity | None = None,
        provider: str = "",
    ) -> Mt5StrategyInitResponse:
        deployment = store.find_deployment_by_key(raw_key)
        if deployment is None and raw_key.strip().startswith("gl_"):
            deployment = store.upsert_web_deployment(
                raw_key,
                user_id="mt5_runtime",
                strategy_code="PA_MOCK_V1",
                strategy_name="MT5 Runtime PA Mock",
                status="active",
                symbol="XAUUSD",
                timeframe="M15",
                config={
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
                    "summary": "Auto-created by MT5 init for local development.",
                    "open_logic": "",
                    "position_logic": "",
                },
            )
        if deployment is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_deployment_key",
            )

        store.record_deployment_activity(
            deployment["id"],
            strategy_code=deployment["strategy_code"],
            event_type="init",
        )
        if account is not None:
            store.save_deployment_account(
                deployment,
                login=account.login,
                platform=account.platform,
                provider=account.provider or provider,
                server=account.server,
            )
        config = deployment["config"]
        return Mt5StrategyInitResponse(
            status="ok",
            protocol_version=1.0,
            min_ea_version=1.0,
            ea_upgrade_required=False,
            strategy=Mt5StrategyInfo(
                id=deployment["id"],
                name=deployment["strategy_name"],
                summary=config.get("summary", ""),
                status=deployment["status"],
                open_data_type=config.get("open_data_type", "kline"),
                open_kline_count=int(config.get("open_kline_count", 100)),
                position_data_type=config.get("position_data_type", "kline"),
                position_kline_count=int(config.get("position_kline_count", 100)),
                call_mode=config.get("call_mode", "bar"),
                call_val=float(config.get("call_val", 1)),
            ),
        )

    @router.post("/open-decision", response_model=Mt5OpenDecisionResponse)
    def open_decision(request: Mt5OpenDecisionRequest) -> Mt5OpenDecisionResponse:
        request_id = _request_id("open", request)
        test_response = _mt5_open_test_response(request, request_id)
        if test_response is not None:
            deployment = decision_service.authenticate(request.deployment_key, request.account)
            store.record_deployment_activity(
                deployment["id"],
                strategy_code=deployment["strategy_code"],
                event_type="open",
            )
            return test_response

        evaluate_request = OpenEvaluateRequest(
            deployment_key=request.deployment_key,
            request_id=request_id,
            account=request.account,
            symbol=request.symbol,
            timeframe=request.timeframe,
            bar_time=_bar_time(request.market.bars),
            bid=request.market.bid,
            ask=request.market.ask,
            spread_points=request.market.spread,
            candles=_candles(request.market.bars),
            symbol_info=request.market.metadata,
            balance=request.balance or request.account.balance,
            equity=request.equity or request.account.equity,
        )
        decision = decision_service.evaluate_open(evaluate_request)
        return _mt5_open_response(decision, spread=request.market.spread)

    @router.post("/position-decision", response_model=Mt5PositionDecisionResponse)
    def position_decision(request: Mt5PositionDecisionRequest) -> Mt5PositionDecisionResponse:
        request_id = _request_id("position", request)
        test_response = _mt5_position_test_response(request, request_id)
        if test_response is not None:
            deployment = decision_service.authenticate(request.deployment_key, request.account)
            store.record_deployment_activity(
                deployment["id"],
                strategy_code=deployment["strategy_code"],
                event_type="position",
            )
            return test_response

        evaluate_request = PositionEvaluateRequest(
            deployment_key=request.deployment_key,
            request_id=request_id,
            account=request.account,
            symbol=request.symbol,
            timeframe=request.timeframe,
            bar_time=_bar_time(request.market.bars),
            bid=request.market.bid,
            ask=request.market.ask,
            spread_points=request.market.spread,
            candles=_candles(request.market.bars),
            symbol_info=request.market.metadata,
            balance=request.balance or request.account.balance,
            equity=request.equity or request.account.equity,
            positions=[
                _position_snapshot(item, bid=request.market.bid, ask=request.market.ask)
                for item in request.positions
            ],
        )
        decision = decision_service.evaluate_position(evaluate_request)
        return _mt5_position_response(
            decision,
            spread=request.market.spread,
            positions=request.positions,
        )

    return router


def create_mt5_executions_router(
    store: SqliteStore,
    decision_service: DecisionService,
) -> APIRouter:
    router = APIRouter(prefix="/mt5/executions", default_response_class=AsciiJSONResponse)

    @router.post("/history-sync", response_model=Mt5HistorySyncResponse)
    def history_sync(request: Mt5HistorySyncRequest) -> Mt5HistorySyncResponse:
        source_account = request.account or AccountIdentity(login=request.login or "unknown")
        account = AccountIdentity(
            platform=source_account.platform,
            login=request.login or source_account.login,
            server=source_account.server,
            balance=source_account.balance,
            equity=source_account.equity,
        )
        deployment = decision_service.authenticate(request.deployment_key, account)
        store.save_deployment_account(
            deployment,
            login=account.login,
            platform=account.platform,
            provider=account.provider,
            server=account.server,
        )
        store.record_deployment_activity(
            deployment["id"],
            strategy_code=deployment["strategy_code"],
            event_type="history_sync",
        )
        result = store.sync_mt5_history_deals(
            deployment["id"],
            account_login=account.login,
            account_server=account.server,
            orders=[item.model_dump(mode="json") for item in request.orders],
        )
        return Mt5HistorySyncResponse(status="ok", **result)

    return router


def create_admin_ai_router(store: SqliteStore) -> APIRouter:
    router = APIRouter(prefix="/api/admin/ai", default_response_class=AsciiJSONResponse)

    def ok(data: object = None, message: str = "success") -> dict[str, object]:
        return {
            "code": 0,
            "data": data if data is not None else {},
            "message": message,
        }

    @router.post("/stats/overview")
    def stats_overview() -> dict[str, object]:
        return ok(store.admin_ai_strategy_overview())

    @router.post("/official-strategy/list")
    def official_strategy_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_official_ai_strategies(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
        )
        return ok(data)

    @router.post("/official-strategy/detail")
    def official_strategy_detail(payload: dict[str, object]) -> dict[str, object]:
        strategy_id = str(payload.get("id") or payload.get("code") or "").strip()
        if not strategy_id:
            raise HTTPException(status_code=400, detail="strategy_id_required")
        try:
            return ok(store.admin_official_strategy_detail(
                strategy_id,
                period=str(payload.get("period") or "all").strip(),
            ))
        except RuntimeError as exc:
            if str(exc) == "official_strategy_not_found":
                raise HTTPException(status_code=404, detail="strategy_not_found") from exc
            raise

    @router.post("/official-strategy/deployment-detail")
    def official_strategy_deployment_detail(payload: dict[str, object]) -> dict[str, object]:
        deployment_id = str(payload.get("deployment_id") or "").strip()
        if not deployment_id:
            raise HTTPException(status_code=400, detail="deployment_id_required")
        try:
            return ok(store.admin_deployment_account_symbol_stats(
                deployment_id,
                period=str(payload.get("period") or "all").strip(),
            ))
        except RuntimeError as exc:
            if str(exc) == "deployment_not_found":
                raise HTTPException(status_code=404, detail="deployment_not_found") from exc
            raise

    @router.post("/official-strategy/deployment-orders")
    def official_strategy_deployment_orders(payload: dict[str, object]) -> dict[str, object]:
        deployment_id = str(payload.get("deployment_id") or "").strip()
        if not deployment_id:
            raise HTTPException(status_code=400, detail="deployment_id_required")
        return ok(store.admin_deployment_history_orders(
            deployment_id,
            account_login=str(payload.get("account_login") or "").strip(),
            account_server=str(payload.get("account_server") or "").strip(),
            symbol=str(payload.get("symbol") or "").strip(),
            period=str(payload.get("period") or "all").strip(),
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 50),
        ))

    @router.post("/official-strategy/save")
    def official_strategy_save(payload: dict[str, object]) -> dict[str, object]:
        if not str(payload.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="strategy_name_required")
        if not str(payload.get("code") or "").strip():
            raise HTTPException(status_code=400, detail="strategy_code_required")
        return ok(store.save_official_ai_strategy(payload), "saved")

    @router.post("/provider/list")
    def provider_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_ai_providers(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            official_only=bool(payload.get("official_only", False)),
        )
        return ok(data)

    @router.post("/provider/save")
    def provider_save(payload: dict[str, object]) -> dict[str, object]:
        if not str(payload.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="provider_name_required")
        return ok(store.save_ai_provider(payload), "保存成功")

    @router.post("/provider/delete")
    def provider_delete(payload: dict[str, object]) -> dict[str, object]:
        provider_id = str(payload.get("id") or "").strip()
        if not provider_id:
            raise HTTPException(status_code=400, detail="provider_id_required")
        store.clear_ai_provider_key(provider_id)
        return ok(message="删除成功")

    @router.post("/model/list")
    def model_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_ai_models(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            provider_id=str(payload.get("provider_id") or "").strip(),
        )
        return ok(data)

    @router.post("/model/save")
    def model_save(payload: dict[str, object]) -> dict[str, object]:
        if not str(payload.get("provider_id") or "").strip():
            raise HTTPException(status_code=400, detail="provider_id_required")
        if not str(payload.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="model_name_required")
        if not str(payload.get("base_url") or "").strip():
            raise HTTPException(status_code=400, detail="model_base_url_required")
        if not str(payload.get("display_name") or "").strip():
            payload["display_name"] = payload["name"]
        return ok(store.save_ai_model(payload), "保存成功")

    @router.post("/model/delete")
    def model_delete(payload: dict[str, object]) -> dict[str, object]:
        model_id = str(payload.get("id") or "").strip()
        if not model_id:
            raise HTTPException(status_code=400, detail="model_id_required")
        store.delete_ai_model(model_id)
        return ok(message="删除成功")

    @router.post("/quota/list")
    def quota_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_ai_user_quotas(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
        )
        return ok(data)

    @router.post("/quota/save")
    def quota_save(payload: dict[str, object]) -> dict[str, object]:
        if not str(payload.get("user_id") or "").strip():
            raise HTTPException(status_code=400, detail="user_id_required")
        return ok(store.save_ai_user_quota(payload), "保存成功")

    @router.post("/usage/list")
    def usage_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_ai_usage_logs(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            user_id=str(payload.get("user_id") or "").strip(),
        )
        return ok(data)

    return router


def _request_id(
    endpoint: str,
    request: Mt5OpenDecisionRequest | Mt5PositionDecisionRequest,
) -> str:
    last_bar_time = str(request.market.bars[-1].time) if request.market.bars else "no_bar"
    source_id = request.request_id or "auto"
    raw = "|".join(
        [
            endpoint,
            request.deployment_key,
            source_id,
            request.account.login,
            request.symbol.upper(),
            request.timeframe.upper(),
            request.data_type,
            last_bar_time,
            str(request.market.bid),
            str(request.market.ask),
            str(request.market.spread),
        ],
    )
    return f"{source_id[:48]}_{sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _bar_timestamp(bar: Mt5Bar, fallback: int) -> int:
    if isinstance(bar.time, int):
        return _normalize_epoch_seconds(bar.time)
    try:
        value = datetime.fromisoformat(bar.time.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    except ValueError:
        return fallback


def _bar_time(bars: list[Mt5Bar]) -> int:
    if not bars:
        return int(datetime.now(timezone.utc).timestamp())
    now = int(datetime.now(timezone.utc).timestamp())
    return max(_bar_timestamp(item, now) for item in bars)


def _normalize_epoch_seconds(value: int) -> int:
    timestamp = int(value)
    if timestamp > 10_000_000_000_000:
        return timestamp // 1_000_000
    if timestamp > 10_000_000_000:
        return timestamp // 1_000
    return timestamp


def _candles(bars: list[Mt5Bar]) -> list[Candle]:
    return [
        Candle(
            timestamp=_bar_timestamp(item, index),
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=item.volume,
        )
        for index, item in enumerate(bars)
    ]


def _position_snapshot(position: Mt5Position, *, bid: float, ask: float) -> PositionSnapshot:
    current_price = position.current_price
    direction = _mt5_direction(position)
    if current_price is None:
        current_price = bid if direction == "buy" else ask
    return PositionSnapshot(
        ticket=str(position.ticket),
        symbol=position.symbol,
        side="BUY" if direction == "buy" else "SELL",
        volume=position.volume,
        open_price=position.open_price,
        current_price=current_price,
        profit=position.profit,
        sl=position.sl,
        tp=position.tp,
        open_time=position.open_time,
    )


def _mt5_open_response(decision: TradeDecision, *, spread: float) -> Mt5OpenDecisionResponse:
    orders: list[Mt5OpenOrder] = []
    if decision.action in {"BUY", "SELL"} and decision.lot:
        orders.append(
            Mt5OpenOrder(
                direction="buy" if decision.action == "BUY" else "sell",
                volume=decision.lot,
                order_type="market",
                price=decision.entry or 0.0,
                sl=decision.sl,
                tp=decision.tp,
                comment="GainLabAI",
            ),
        )

    return Mt5OpenDecisionResponse(
        status="ok",
        should_open=len(orders) > 0,
        description=decision.reason,
        spread=spread,
        decision_id=decision.decision_id,
        request_id=decision.request_id,
        orders_count=len(orders),
        orders=orders,
    )


def _mt5_open_test_response(
    request: Mt5OpenDecisionRequest,
    request_id: str,
) -> Mt5OpenDecisionResponse | None:
    source_id = request.request_id or ""
    if source_id.startswith("test_random"):
        return _mt5_open_random_response(request, request_id)

    if not source_id.startswith("test_pending_"):
        return None

    spread_price = _spread_price(request.market.bid, request.market.ask)

    if source_id.startswith("test_pending_buy_limit"):
        price = request.market.bid - spread_price * 3
        order = Mt5OpenOrder(
            direction="buy",
            volume=0.01,
            order_type="limit",
            price=price,
            sl=price - spread_price * 3,
            tp=price + spread_price * 5,
            comment="Test pending buy limit",
        )
        description = "Test pending buy limit"
    elif source_id.startswith("test_pending_sell_limit"):
        price = request.market.ask + spread_price * 3
        order = Mt5OpenOrder(
            direction="sell",
            volume=0.01,
            order_type="limit",
            price=price,
            sl=price + spread_price * 3,
            tp=price - spread_price * 5,
            comment="Test pending sell limit",
        )
        description = "Test pending sell limit"
    else:
        return None

    return Mt5OpenDecisionResponse(
        status="ok",
        should_open=True,
        description=description,
        spread=request.market.spread,
        decision_id=f"dec_test_{sha256(request_id.encode('utf-8')).hexdigest()[:24]}",
        request_id=request_id,
        orders_count=1,
        orders=[order],
    )


def _mt5_open_random_response(
    request: Mt5OpenDecisionRequest,
    request_id: str,
) -> Mt5OpenDecisionResponse:
    spread_price = _spread_price(request.market.bid, request.market.ask)
    pending_distance = spread_price * 10
    sl_distance = spread_price * 30
    tp_distance = spread_price * 50
    mode = random.choice(["hold", "market_buy", "market_sell", "buy_limit", "sell_limit"])
    order: Mt5OpenOrder | None = None

    if mode == "market_buy":
        order = Mt5OpenOrder(
            direction="buy",
            volume=random.choice([0.01, 0.02]),
            order_type="market",
            price=request.market.ask,
            sl=request.market.ask - sl_distance,
            tp=request.market.ask + tp_distance,
            comment="Random market buy",
        )
    elif mode == "market_sell":
        order = Mt5OpenOrder(
            direction="sell",
            volume=random.choice([0.01, 0.02]),
            order_type="market",
            price=request.market.bid,
            sl=request.market.bid + sl_distance,
            tp=request.market.bid - tp_distance,
            comment="Random market sell",
        )
    elif mode == "buy_limit":
        price = request.market.bid - pending_distance
        order = Mt5OpenOrder(
            direction="buy",
            volume=0.01,
            order_type="limit",
            price=price,
            sl=price - sl_distance,
            tp=price + tp_distance,
            comment="Random buy limit",
        )
    elif mode == "sell_limit":
        price = request.market.ask + pending_distance
        order = Mt5OpenOrder(
            direction="sell",
            volume=0.01,
            order_type="limit",
            price=price,
            sl=price + sl_distance,
            tp=price - tp_distance,
            comment="Random sell limit",
        )

    orders = [order] if order is not None else []
    return Mt5OpenDecisionResponse(
        status="ok",
        should_open=len(orders) > 0,
        description=f"Random open test: {mode}",
        spread=request.market.spread,
        decision_id=f"dec_random_{sha256((request_id + mode).encode('utf-8')).hexdigest()[:24]}",
        request_id=request_id,
        orders_count=len(orders),
        orders=orders,
    )


def _mt5_position_response(
    decision: TradeDecision,
    *,
    spread: float,
    positions: list[Mt5Position],
) -> Mt5PositionDecisionResponse:
    actions: list[Mt5PositionAction] = []
    target = _find_mt5_position(positions, decision.position_ticket)

    if decision.action == "CLOSE" and target is not None:
        actions.append(
            Mt5PositionAction(
                action="close",
                ticket=str(target.ticket),
                mt_type=target.mt_type,
                volume=target.volume,
                order_type="market",
                price=0.0,
                comment=decision.reason,
            ),
        )
    elif decision.action in {"MODIFY_SL", "MODIFY_TP"} and target is not None:
        actions.append(
            Mt5PositionAction(
                action="modify",
                ticket=str(target.ticket),
                mt_type=target.mt_type,
                volume=0.0,
                order_type="market",
                price=0.0,
                sl=decision.sl,
                tp=decision.tp,
                comment=decision.reason,
            ),
        )
    elif decision.action in {"BUY", "SELL"} and decision.lot:
        actions.append(
            Mt5PositionAction(
                action="add",
                ticket="",
                mt_type=0 if decision.action == "BUY" else 1,
                direction="buy" if decision.action == "BUY" else "sell",
                volume=decision.lot,
                order_type="market",
                price=decision.entry or 0.0,
                sl=decision.sl,
                tp=decision.tp,
                comment=decision.reason,
            ),
        )

    return Mt5PositionDecisionResponse(
        status="ok",
        has_action=len(actions) > 0,
        description=decision.reason,
        spread=spread,
        decision_id=decision.decision_id,
        request_id=decision.request_id,
        actions_count=len(actions),
        actions=actions,
    )


def _mt5_position_test_response(
    request: Mt5PositionDecisionRequest,
    request_id: str,
) -> Mt5PositionDecisionResponse | None:
    source_id = request.request_id or ""
    if source_id.startswith("test_random"):
        return _mt5_position_random_response(request, request_id)

    if not source_id.startswith("test_"):
        return None

    target = request.positions[0] if request.positions else None
    spread_price = _spread_price(request.market.bid, request.market.ask)

    action: Mt5PositionAction | None = None
    description = "Position action test response"

    if source_id.startswith("test_add_buy"):
        action = Mt5PositionAction(
            action="add",
            ticket="",
            mt_type=0,
            direction="buy",
            volume=0.01,
            order_type="market",
            price=request.market.ask,
            sl=request.market.ask - spread_price * 3,
            tp=request.market.ask + spread_price * 5,
            comment="Test add buy",
        )
        description = "Test add buy"
    elif source_id.startswith("test_add_sell"):
        action = Mt5PositionAction(
            action="add",
            ticket="",
            mt_type=1,
            direction="sell",
            volume=0.01,
            order_type="market",
            price=request.market.bid,
            sl=request.market.bid + spread_price * 3,
            tp=request.market.bid - spread_price * 5,
            comment="Test add sell",
        )
        description = "Test add sell"
    elif source_id.startswith("test_modify") and target is not None:
        direction = _mt5_direction(target)
        base_price = request.market.bid if direction == "buy" else request.market.ask
        action = Mt5PositionAction(
            action="modify",
            ticket=str(target.ticket),
            mt_type=target.mt_type,
            volume=0.0,
            order_type="market",
            price=0.0,
            sl=base_price - spread_price * 3 if direction == "buy" else base_price + spread_price * 3,
            tp=base_price + spread_price * 5 if direction == "buy" else base_price - spread_price * 5,
            comment="Test modify sl tp",
        )
        description = "Test modify sl tp"
    elif source_id.startswith("test_cancel") and target is not None:
        action = Mt5PositionAction(
            action="cancel",
            ticket=str(target.ticket),
            mt_type=target.mt_type,
            volume=0.0,
            order_type=_mt5_order_type(target.mt_type),
            price=0.0,
            comment="Test cancel pending order",
        )
        description = "Test cancel pending order"

    actions = [action] if action is not None else []
    return Mt5PositionDecisionResponse(
        status="ok",
        has_action=len(actions) > 0,
        description=description if actions else "No test action matched",
        spread=request.market.spread,
        decision_id=f"dec_test_{sha256(request_id.encode('utf-8')).hexdigest()[:24]}",
        request_id=request_id,
        actions_count=len(actions),
        actions=actions,
    )


def _mt5_position_random_response(
    request: Mt5PositionDecisionRequest,
    request_id: str,
) -> Mt5PositionDecisionResponse:
    source_id = request.request_id or ""
    if source_id.startswith("test_random_all"):
        modes = _repeat_modes(["close", "modify", "cancel"], len(request.positions))
        if not modes:
            modes = ["add_buy", "add_sell"]
    else:
        modes = ["hold", "add_buy", "add_sell"]
        if request.positions:
            sample_size = random.randint(1, min(len(request.positions), 6))
            modes.extend(random.choices(["close", "modify", "cancel"], k=sample_size))

    spread_price = _spread_price(request.market.bid, request.market.ask)
    sl_distance = spread_price * 30
    tp_distance = spread_price * 50
    actions: list[Mt5PositionAction] = []
    position_index = 0
    for mode in modes:
        target = None
        if mode in {"close", "modify", "cancel"} and request.positions:
            target = request.positions[position_index % len(request.positions)]
            position_index += 1
        action = _mt5_position_random_action(
            mode,
            request=request,
            target=target,
            sl_distance=sl_distance,
            tp_distance=tp_distance,
        )
        if action is not None:
            actions.append(action)

    description_mode = "multi" if len(actions) > 1 else (modes[0] if modes else "hold")
    return Mt5PositionDecisionResponse(
        status="ok",
        has_action=len(actions) > 0,
        description=f"Random position test: {description_mode}",
        spread=request.market.spread,
        decision_id=f"dec_random_{sha256((request_id + description_mode + str(len(actions))).encode('utf-8')).hexdigest()[:24]}",
        request_id=request_id,
        actions_count=len(actions),
        actions=actions,
    )


def _mt5_position_random_action(
    mode: str,
    *,
    request: Mt5PositionDecisionRequest,
    target: Mt5Position | None,
    sl_distance: float,
    tp_distance: float,
) -> Mt5PositionAction | None:
    if mode == "hold":
        return None
    if mode == "close" and target is not None:
        return Mt5PositionAction(
            action="close",
            ticket=str(target.ticket),
            mt_type=target.mt_type,
            volume=target.volume,
            order_type="market",
            price=0.0,
            comment="Random close",
        )
    if mode == "add_buy":
        return Mt5PositionAction(
            action="add",
            ticket="",
            mt_type=0,
            direction="buy",
            volume=0.01,
            order_type="market",
            price=request.market.ask,
            sl=request.market.ask - sl_distance,
            tp=request.market.ask + tp_distance,
            comment="Random add buy",
        )
    if mode == "add_sell":
        return Mt5PositionAction(
            action="add",
            ticket="",
            mt_type=1,
            direction="sell",
            volume=0.01,
            order_type="market",
            price=request.market.bid,
            sl=request.market.bid + sl_distance,
            tp=request.market.bid - tp_distance,
            comment="Random add sell",
        )
    if mode == "modify" and target is not None:
        direction = _mt5_direction(target)
        base_price = request.market.bid if direction == "buy" else request.market.ask
        return Mt5PositionAction(
            action="modify",
            ticket=str(target.ticket),
            mt_type=target.mt_type,
            volume=0.0,
            order_type=_mt5_order_type(target.mt_type),
            price=0.0,
            sl=base_price - sl_distance if direction == "buy" else base_price + sl_distance,
            tp=base_price + tp_distance if direction == "buy" else base_price - tp_distance,
            comment="Random modify",
        )
    if mode == "cancel" and target is not None:
        return Mt5PositionAction(
            action="cancel",
            ticket=str(target.ticket),
            mt_type=target.mt_type,
            volume=0.0,
            order_type=_mt5_order_type(target.mt_type),
            price=0.0,
            comment="Random cancel",
        )
    return None


def _repeat_modes(modes: list[str], count: int) -> list[str]:
    return [modes[index % len(modes)] for index in range(count)]


def _find_mt5_position(
    positions: list[Mt5Position],
    ticket: str | None,
) -> Mt5Position | None:
    if ticket is None:
        return positions[0] if positions else None
    for position in positions:
        if str(position.ticket) == ticket:
            return position
    return positions[0] if positions else None


def _mt5_order_type(mt_type: int | str) -> str:
    mt_type_number = _mt5_type_number(mt_type)
    if mt_type_number in {2, 3}:
        return "limit"
    if mt_type_number in {4, 5, 6, 7}:
        return "stop"
    return "market"


def _spread_price(bid: float, ask: float) -> float:
    spread_price = abs(ask - bid)
    if spread_price > 0:
        return spread_price
    return max(abs(bid) * 0.001, 1.0)


def _mt5_trade_type(position: Mt5Position) -> str:
    if position.trade_type:
        return position.trade_type
    mt_type = _mt5_type_number(position.mt_type)
    return "pending_order" if mt_type >= 2 else "position"


def _mt5_direction(position: Mt5Position) -> str:
    if position.direction:
        return position.direction
    mt_type = _mt5_type_number(position.mt_type)
    return "buy" if mt_type % 2 == 0 else "sell"


def _mt5_type_number(mt_type: int | str) -> int:
    if isinstance(mt_type, int):
        return mt_type
    try:
        return int(mt_type)
    except ValueError:
        upper = mt_type.upper()
        if "SELL" in upper:
            return 1
        return 0


def _mt5_response(decision: TradeDecision) -> Mt5DecisionResponse:
    action = "hold"
    direction = None
    if decision.action == "BUY":
        action = "open"
        direction = "buy"
    elif decision.action == "SELL":
        action = "open"
        direction = "sell"
    elif decision.action == "CLOSE":
        action = "close"
    elif decision.action in {"MODIFY_SL", "MODIFY_TP"}:
        action = "modify_sl_tp"
    elif decision.action == "HOLD":
        action = "hold"

    return Mt5DecisionResponse(
        status="ok",
        action=action,
        direction=direction,
        volume=decision.lot,
        sl=decision.sl,
        tp=decision.tp,
        ticket=decision.position_ticket,
        reason=decision.reason,
        decision_id=decision.decision_id,
        request_id=decision.request_id,
        confidence=decision.confidence,
        expires_at=decision.expires_at,
        idempotent=decision.idempotent,
    )

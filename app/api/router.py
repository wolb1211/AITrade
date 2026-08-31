from __future__ import annotations

import json
import base64
from decimal import Decimal, InvalidOperation
import hmac
import random
import time
from datetime import datetime, timezone
from hashlib import sha256

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
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
from app.services.auth_service import AuthError, UserAuthService
from app.services.ai_service import AiDecisionClient
from app.services.custom_indicators import public_indicator_catalog
from app.services.custom_workflow import workflow_catalog, workflow_json_schema, workflow_validation_result
from app.services.screenshot_preview import ScreenshotError, load_preview, prepare_screenshot
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


def _decode_admin_jwt(token: str, secret: str) -> dict[str, object]:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".")
        signed = f"{encoded_header}.{encoded_payload}".encode("ascii")
        expected = hmac.new(secret.encode("utf-8"), signed, sha256).digest()
        signature_padding = "=" * (-len(encoded_signature) % 4)
        signature = base64.urlsafe_b64decode(encoded_signature + signature_padding)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        payload_padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + payload_padding))
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
        expires_at = int(payload.get("exp") or 0)
        if expires_at and expires_at <= int(time.time()):
            raise ValueError("expired")
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=401, detail="invalid_admin_token") from exc


def create_api_router(
    store: SqliteStore,
    decision_service: DecisionService,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    def public_ok(data: object = None, message: str = "success") -> dict[str, object]:
        return {"code": 0, "message": message, "data": data}

    @router.get("/guides")
    def public_guide_list() -> dict[str, object]:
        return public_ok(store.list_guide_articles())

    @router.get("/guides/{article_id}")
    def public_guide_detail(article_id: str) -> dict[str, object]:
        article = store.get_guide_article(article_id)
        if article is None:
            raise HTTPException(status_code=404, detail="guide_not_found")
        return public_ok(article)

    @router.post("/ea/activate", response_model=ActivateResponse)
    def activate(request: ActivateRequest) -> ActivateResponse:
        deployment = store.find_deployment_by_key(request.deployment_key)
        if deployment is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_deployment_key",
            )
        decision_service.ensure_deployment_access(deployment)
        deployment = store.activate_deployment(
            request.deployment_key,
            platform=request.account.platform,
            login=request.account.login,
            server=request.account.server,
        ) or deployment
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

    @router.get("/web/official-strategies")
    def web_official_strategies() -> dict[str, object]:
        return {
            "ok": True,
            **store.list_public_official_ai_strategies(),
        }

    @router.post(
        "/web/deployments/upsert",
        response_model=WebDeploymentUpsertResponse,
    )
    def upsert_web_deployment(
        request: WebDeploymentUpsertRequest,
    ) -> WebDeploymentUpsertResponse:
        existing = store.find_deployment_by_key(request.deployment_key)
        user = store.get_user(request.user_id)
        if existing is None:
            if user is None or not bool(user.get("vip_active")):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="vip_required",
                )
            if int(user.get("strategy_count") or 0) >= int(user.get("max_strategy_keys") or 0):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="strategy_key_limit_reached",
                )
        elif str(existing.get("user_id")) != str(request.user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="deployment_not_found",
            )
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
                "open_ai_endpoint_id": request.open_ai_endpoint_id,
                "open_ai_model": request.open_ai_model,
                "open_ai_base_url": request.open_ai_base_url,
                "open_ai_key": request.open_ai_key,
                "position_ai_mode": request.position_ai_mode,
                "position_ai_endpoint_id": request.position_ai_endpoint_id,
                "position_ai_model": request.position_ai_model,
                "position_ai_base_url": request.position_ai_base_url,
                "position_ai_key": request.position_ai_key,
            },
        )
        if "mt_login" in request.model_fields_set:
            deployment = store.set_deployment_login(
                request.deployment_key,
                request.mt_login or "",
            ) or deployment
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
            open_ai_endpoint_id = str(config.get("open_ai_endpoint_id") or "")
            position_ai_endpoint_id = str(config.get("position_ai_endpoint_id") or "")
            open_ai_endpoint_name = ""
            open_ai_endpoint_model = ""
            position_ai_endpoint_name = ""
            position_ai_endpoint_model = ""
            official_strategy = store.get_official_ai_strategy(deployment["strategy_code"])
            if official_strategy is not None:
                open_ai_endpoint_id = open_ai_endpoint_id or str(official_strategy.get("open_ai_endpoint_id") or "")
                position_ai_endpoint_id = position_ai_endpoint_id or str(official_strategy.get("position_ai_endpoint_id") or "")
                open_endpoint = store.get_ai_endpoint(open_ai_endpoint_id)
                position_endpoint = store.get_ai_endpoint(position_ai_endpoint_id)
                if open_endpoint is not None:
                    open_ai_endpoint_name = str(open_endpoint["name"] or "")
                    open_ai_endpoint_model = str(open_endpoint["model"] or "")
                if position_endpoint is not None:
                    position_ai_endpoint_name = str(position_endpoint["name"] or "")
                    position_ai_endpoint_model = str(position_endpoint["model"] or "")
            deployments.append(
                WebDeploymentItem(
                    id=deployment["id"],
                    deployment_key=config["deployment_key"],
                    name=deployment["strategy_name"],
                    status=deployment["status"],
                    strategy_code=deployment["strategy_code"],
                    user_id=deployment["user_id"],
                    mt_login=str(deployment.get("mt_login") or ""),
                    summary=config.get("summary", ""),
                    open_logic=config.get("open_logic", ""),
                    position_logic=config.get("position_logic", ""),
                    open_ai_mode=config.get("open_ai_mode", "official"),
                    open_ai_endpoint_id=open_ai_endpoint_id,
                    open_ai_endpoint_name=open_ai_endpoint_name,
                    open_ai_endpoint_model=open_ai_endpoint_model,
                    open_ai_model=config.get("open_ai_model", ""),
                    open_ai_base_url=config.get("open_ai_base_url", ""),
                    open_ai_key=config.get("open_ai_key", ""),
                    position_ai_mode=config.get("position_ai_mode", "official"),
                    position_ai_endpoint_id=position_ai_endpoint_id,
                    position_ai_endpoint_name=position_ai_endpoint_name,
                    position_ai_endpoint_model=position_ai_endpoint_model,
                    position_ai_model=config.get("position_ai_model", ""),
                    position_ai_base_url=config.get("position_ai_base_url", ""),
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
        if deployment is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_deployment_key",
            )

        access_error = decision_service.deployment_access_error(deployment)
        if access_error == "invalid_deployment_key":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=access_error,
            )

        if access_error is None and account is not None:
            try:
                deployment = store.bind_deployment_login(
                    raw_key,
                    login=account.login,
                    platform=account.platform,
                    server=account.server,
                ) or deployment
            except RuntimeError as exc:
                if str(exc) == "deployment_account_mismatch":
                    access_error = "deployment_account_mismatch"
                elif str(exc) == "invalid_deployment_account":
                    access_error = "invalid_deployment_account"
                else:
                    raise
            if access_error is None:
                store.save_deployment_account(
                    deployment,
                    login=account.login,
                    platform=account.platform,
                    provider=account.provider or provider,
                    server=account.server,
                )
        if access_error is None:
            store.record_deployment_activity(
                deployment["id"],
                strategy_code=deployment["strategy_code"],
                event_type="init",
            )
        config = deployment["config"]
        official_strategy = store.get_official_ai_strategy(deployment["strategy_code"])
        strategy_name = deployment["strategy_name"]
        strategy_summary = config.get("ea_description") or config.get("summary", "")
        if official_strategy is not None:
            strategy_summary = official_strategy["summary"]
        return Mt5StrategyInitResponse(
            status="ok",
            protocol_version=1.0,
            min_ea_version=1.0,
            ea_upgrade_required=False,
            strategy=Mt5StrategyInfo(
                id=deployment["id"],
                name=strategy_name,
                summary=strategy_summary,
                status=(
                    _mt5_business_error_description(access_error, include_code=False)
                    if access_error is not None
                    else deployment["status"]
                ),
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
        deployment = store.find_deployment_by_key(request.deployment_key)
        access_error = "invalid_deployment_key" if deployment is None else decision_service.deployment_access_error(deployment)
        if access_error is None:
            deployment, access_error = _mt5_validate_deployment_account(
                store, deployment, request.deployment_key, request.account,
            )
        if access_error is not None:
            return _mt5_open_error_response(request, request_id, access_error)
        test_response = _mt5_open_test_response(request, request_id)
        if test_response is not None:
            store.record_deployment_activity(
                deployment["id"],
                strategy_code=deployment["strategy_code"],
                event_type="open",
            )
            return test_response

        try:
            screenshot_data_url, screenshot_metadata = _request_screenshot(request.data_type, request.market.screenshot)
        except ScreenshotError as exc:
            return _mt5_open_error_response(request, request_id, str(exc))

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
            data_type=request.data_type,
            screenshot_data_url=screenshot_data_url,
            screenshot_metadata=screenshot_metadata,
            balance=request.balance or request.account.balance,
            equity=request.equity or request.account.equity,
        )
        decision = decision_service.evaluate_open(evaluate_request)
        return _mt5_open_response(decision, spread=request.market.spread)

    @router.post("/position-decision", response_model=Mt5PositionDecisionResponse)
    def position_decision(request: Mt5PositionDecisionRequest) -> Mt5PositionDecisionResponse:
        request_id = _request_id("position", request)
        deployment = store.find_deployment_by_key(request.deployment_key)
        access_error = "invalid_deployment_key" if deployment is None else decision_service.deployment_access_error(deployment)
        if access_error is None:
            deployment, access_error = _mt5_validate_deployment_account(
                store, deployment, request.deployment_key, request.account,
            )
        if access_error is not None:
            return _mt5_position_error_response(request, request_id, access_error)
        test_response = _mt5_position_test_response(request, request_id)
        if test_response is not None:
            store.record_deployment_activity(
                deployment["id"],
                strategy_code=deployment["strategy_code"],
                event_type="position",
            )
            return test_response

        try:
            screenshot_data_url, screenshot_metadata = _request_screenshot(request.data_type, request.market.screenshot)
        except ScreenshotError as exc:
            return _mt5_position_error_response(request, request_id, str(exc))

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
            data_type=request.data_type,
            screenshot_data_url=screenshot_data_url,
            screenshot_metadata=screenshot_metadata,
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


def create_admin_ai_router(
    store: SqliteStore,
    *,
    ai_client: AiDecisionClient | None = None,
    admin_jwt_secret: str = "",
    require_admin_auth: bool = False,
) -> APIRouter:
    endpoint_test_client = ai_client or AiDecisionClient(store)
    def require_admin(authorization: str = Header(default="")) -> None:
        if not require_admin_auth:
            return
        if not admin_jwt_secret:
            raise HTTPException(status_code=503, detail="admin_auth_not_configured")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="admin_token_required")
        payload = _decode_admin_jwt(token.strip(), admin_jwt_secret)
        roles = payload.get("roles") or []
        if not isinstance(roles, list) or "admin" not in roles:
            raise HTTPException(status_code=403, detail="admin_role_required")

    router = APIRouter(
        prefix="/api/admin/ai",
        default_response_class=AsciiJSONResponse,
        dependencies=[Depends(require_admin)],
    )

    def ok(data: object = None, message: str = "success") -> dict[str, object]:
        return {
            "code": 0,
            "data": data if data is not None else {},
            "message": message,
        }

    @router.post("/stats/overview")
    def stats_overview() -> dict[str, object]:
        return ok(store.admin_ai_strategy_overview())

    @router.post("/user/list")
    def user_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        raw_vip_level = payload.get("vip_level")
        vip_level = None if raw_vip_level in (None, "") else int(raw_vip_level)
        raw_agent_level = payload.get("agent_level")
        agent_level = None if raw_agent_level in (None, "") else int(raw_agent_level)
        return ok(store.list_users(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            status=str(payload.get("status") or "").strip(),
            vip_level=vip_level,
            agent_level=agent_level,
        ))

    @router.post("/user/referrals")
    def user_referrals(payload: dict[str, object]) -> dict[str, object]:
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id.isdigit():
            raise HTTPException(status_code=400, detail="invalid_user_id")
        try:
            return ok(store.get_agent_dashboard(
                int(user_id),
                page=int(payload.get("page") or 1),
                size=int(payload.get("size") or 20),
            ))
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/user/save")
    def user_save(payload: dict[str, object]) -> dict[str, object]:
        try:
            return ok(store.save_user(payload), "saved")
        except RuntimeError as exc:
            if str(exc) == "user_email_exists":
                raise HTTPException(status_code=409, detail="user_email_exists") from exc
            if str(exc) == "invalid_user_status":
                raise HTTPException(status_code=400, detail="invalid_user_status") from exc
            if str(exc) in {"invalid_user_id", "user_email_required"}:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise

    @router.post("/user/strategies")
    def user_strategies(payload: dict[str, object]) -> dict[str, object]:
        user_id = str(payload.get("user_id") or "").strip()
        if not user_id.isdigit():
            raise HTTPException(status_code=400, detail="invalid_user_id")
        rows = []
        for deployment in store.list_web_deployments(user_id):
            config = deployment.get("config", {})
            rows.append({
                "id": deployment["id"],
                "name": deployment["strategy_name"],
                "strategy_code": deployment["strategy_code"],
                "deployment_key": str(config.get("deployment_key") or ""),
                "mt_login": str(deployment.get("mt_login") or ""),
                "status": deployment["status"],
                "open_ai_mode": str(config.get("open_ai_mode") or "official"),
                "position_ai_mode": str(config.get("position_ai_mode") or "official"),
                "updated_at": deployment["updated_at"],
            })
        return ok({"list": rows})

    @router.post("/user/strategy/status")
    def user_strategy_status(payload: dict[str, object]) -> dict[str, object]:
        try:
            deployment = store.update_user_deployment_status(
                user_id=int(str(payload.get("user_id") or "0")),
                deployment_id=str(payload.get("deployment_id") or ""),
                status=str(payload.get("status") or ""),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ok({"id": deployment["id"], "status": deployment["status"]}, "saved")

    @router.post("/user/strategy/delete")
    def user_strategy_delete(payload: dict[str, object]) -> dict[str, object]:
        try:
            store.delete_user_deployment(
                user_id=int(str(payload.get("user_id") or "0")),
                deployment_id=str(payload.get("deployment_id") or ""),
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ok(message="deleted")

    @router.post("/wallet/settings")
    def wallet_settings() -> dict[str, object]:
        return ok(store.get_ai_billing_settings())

    @router.post("/wallet/settings/save")
    def wallet_settings_save(payload: dict[str, object]) -> dict[str, object]:
        try:
            credit_limit = Decimal(str(payload.get("credit_limit") or 0))
            warning_threshold = Decimal(str(payload.get("low_balance_threshold") or 0))
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=400, detail="wallet_setting_invalid") from None
        if credit_limit < 0 or warning_threshold < 0:
            raise HTTPException(status_code=400, detail="wallet_setting_invalid")
        return ok(
            store.save_ai_billing_settings(
                credit_limit=credit_limit,
                low_balance_threshold=warning_threshold,
            ),
            "saved",
        )

    @router.post("/cache/settings")
    def cache_settings() -> dict[str, object]:
        return ok(store.get_ai_cache_settings())

    @router.post("/cache/settings/save")
    def cache_settings_save(payload: dict[str, object]) -> dict[str, object]:
        raw_enabled = payload.get("enabled", True)
        enabled = raw_enabled if isinstance(raw_enabled, bool) else str(raw_enabled).strip().lower() in {"1", "true", "yes", "on"}
        try:
            ttl_seconds = int(payload.get("ttl_seconds") or 120)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="cache_ttl_invalid") from None
        if ttl_seconds < 10 or ttl_seconds > 3600:
            raise HTTPException(status_code=400, detail="cache_ttl_invalid")
        return ok(store.save_ai_cache_settings(enabled=enabled, ttl_seconds=ttl_seconds), "saved")

    @router.post("/wallet/adjust")
    def wallet_adjust(payload: dict[str, object]) -> dict[str, object]:
        raw_user_id = str(payload.get("user_id") or "").strip()
        if not raw_user_id.isdigit():
            raise HTTPException(status_code=400, detail="invalid_user_id")
        operation = str(payload.get("operation") or "").strip()
        if operation not in {"recharge", "deduction"}:
            raise HTTPException(status_code=400, detail="wallet_operation_invalid")
        try:
            amount = Decimal(str(payload.get("amount") or 0))
        except (InvalidOperation, ValueError):
            raise HTTPException(status_code=400, detail="wallet_amount_invalid") from None
        if amount <= 0:
            raise HTTPException(status_code=400, detail="wallet_amount_invalid")
        signed_amount = amount if operation == "recharge" else -amount
        try:
            data = store.adjust_ai_balance(
                user_id=int(raw_user_id),
                amount=signed_amount,
                entry_type=f"admin_{operation}",
                remark=str(payload.get("remark") or "").strip(),
                operator_id=str(payload.get("operator_id") or "admin").strip() or "admin",
            )
        except RuntimeError as exc:
            if str(exc) == "user_not_found":
                raise HTTPException(status_code=404, detail="user_not_found") from exc
            raise
        return ok(data, "saved")

    @router.post("/wallet/ledger")
    def wallet_ledger(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        raw_user_id = str(payload.get("user_id") or "").strip()
        user_id = int(raw_user_id) if raw_user_id.isdigit() else None
        return ok(store.list_ai_balance_ledger(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            user_id=user_id,
            entry_type=str(payload.get("entry_type") or "").strip(),
            exclude_entry_type=str(payload.get("exclude_entry_type") or "").strip(),
        ))

    @router.post("/official-strategy/list")
    def official_strategy_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_official_ai_strategies(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
        )
        return ok(data)

    @router.post("/custom-strategy/list")
    def custom_strategy_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        return ok(store.list_admin_custom_strategies(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            unsupported_only=bool(payload.get("unsupported_only", False)),
        ))

    @router.post("/screenshot-preview")
    def screenshot_preview(payload: dict[str, object]) -> dict[str, object]:
        preview_id = str(payload.get("preview_id") or "").strip()
        try:
            return ok(load_preview(preview_id))
        except ScreenshotError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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
        try:
            return ok(store.admin_deployment_order_overview(
                deployment_id,
                period=str(payload.get("period") or "all").strip(),
                page=int(payload.get("page") or 1),
                size=int(payload.get("size") or 50),
            ))
        except RuntimeError as exc:
            if str(exc) == "deployment_not_found":
                raise HTTPException(status_code=404, detail="deployment_not_found") from exc
            raise

    @router.post("/official-strategy/save")
    def official_strategy_save(payload: dict[str, object]) -> dict[str, object]:
        if not str(payload.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="strategy_name_required")
        if not str(payload.get("code") or "").strip():
            raise HTTPException(status_code=400, detail="strategy_code_required")
        return ok(store.save_official_ai_strategy(payload), "saved")

    @router.post("/ea-download/list")
    def ea_download_list() -> dict[str, object]:
        return ok(store.list_ea_downloads(include_disabled=True))

    @router.post("/ea-download/save")
    def ea_download_save(payload: dict[str, object]) -> dict[str, object]:
        try:
            return ok(store.save_ea_download(payload), "saved")
        except RuntimeError as exc:
            if str(exc) in {"ea_download_name_required", "ea_download_url_required"}:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise

    @router.post("/ea-download/delete")
    def ea_download_delete(payload: dict[str, object]) -> dict[str, object]:
        download_id = str(payload.get("id") or "").strip()
        if not download_id:
            raise HTTPException(status_code=400, detail="ea_download_id_required")
        store.delete_ea_download(download_id)
        return ok(message="deleted")

    @router.post("/guide/list")
    def guide_list() -> dict[str, object]:
        return ok(store.list_guide_articles(include_disabled=True, include_content=True))

    @router.post("/guide/save")
    def guide_save(payload: dict[str, object]) -> dict[str, object]:
        try:
            return ok(store.save_guide_article(payload), "saved")
        except RuntimeError as exc:
            if str(exc) == "guide_title_required":
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise

    @router.post("/guide/delete")
    def guide_delete(payload: dict[str, object]) -> dict[str, object]:
        article_id = str(payload.get("id") or "").strip()
        if not article_id:
            raise HTTPException(status_code=400, detail="guide_id_required")
        store.delete_guide_article(article_id)
        return ok(message="deleted")

    @router.post("/endpoint/list")
    def endpoint_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_ai_endpoints(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            owner_type=str(payload.get("owner_type") or "").strip(),
            user_id=str(payload.get("user_id") or "").strip(),
            selectable_only=bool(payload.get("selectable_only", False)),
            enabled_only=bool(payload.get("enabled_only", False)),
        )
        return ok(data)

    @router.post("/endpoint/save")
    def endpoint_save(payload: dict[str, object]) -> dict[str, object]:
        if not str(payload.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="endpoint_name_required")
        if not str(payload.get("base_url") or "").strip():
            raise HTTPException(status_code=400, detail="endpoint_base_url_required")
        if not str(payload.get("model") or "").strip():
            raise HTTPException(status_code=400, detail="endpoint_model_required")
        for field in ("input_price_per_million", "output_price_per_million"):
            if field not in payload:
                continue
            try:
                price = Decimal(str(payload.get(field) or 0))
            except (InvalidOperation, ValueError):
                raise HTTPException(status_code=400, detail="endpoint_price_invalid") from None
            if price < 0:
                raise HTTPException(status_code=400, detail="endpoint_price_invalid")
            payload[field] = format(price, "f")
        return ok(store.save_ai_endpoint(payload), "saved")

    @router.post("/endpoint/test")
    def endpoint_test(payload: dict[str, object]) -> dict[str, object]:
        endpoint_id = str(payload.get("id") or "").strip()
        if not endpoint_id:
            raise HTTPException(status_code=400, detail="endpoint_id_required")
        try:
            return ok(endpoint_test_client.test_endpoint(endpoint_id), "connection_successful")
        except (RuntimeError, ValueError, TimeoutError) as exc:
            return ok({"success": False, "error": str(exc)}, "connection_failed")

    @router.post("/endpoint/test-vision")
    def endpoint_test_vision(payload: dict[str, object]) -> dict[str, object]:
        endpoint_id = str(payload.get("id") or "").strip()
        if not endpoint_id:
            raise HTTPException(status_code=400, detail="endpoint_id_required")
        try:
            return ok(endpoint_test_client.test_vision_endpoint(endpoint_id), "vision_test_successful")
        except (RuntimeError, ValueError, TimeoutError) as exc:
            return ok({"success": False, "supports_vision": False, "vision_test_status": "failed", "error": str(exc)}, "vision_test_failed")

    @router.post("/endpoint/delete")
    def endpoint_delete(payload: dict[str, object]) -> dict[str, object]:
        endpoint_id = str(payload.get("id") or "").strip()
        if not endpoint_id:
            raise HTTPException(status_code=400, detail="endpoint_id_required")
        try:
            store.delete_ai_endpoint(endpoint_id)
        except RuntimeError as exc:
            if str(exc) == "ai_endpoint_in_use":
                raise HTTPException(status_code=409, detail="ai_endpoint_in_use") from exc
            raise
        return ok(message="deleted")

    @router.post("/template/list")
    def template_list(payload: dict[str, object] | None = None) -> dict[str, object]:
        payload = payload or {}
        data = store.list_ai_templates(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
        )
        return ok(data)

    @router.post("/template/save")
    def template_save(payload: dict[str, object]) -> dict[str, object]:
        if not str(payload.get("code") or "").strip():
            raise HTTPException(status_code=400, detail="template_code_required")
        if not str(payload.get("name") or "").strip():
            raise HTTPException(status_code=400, detail="template_name_required")
        return ok(store.save_ai_template(payload), "saved")

    @router.post("/template/delete")
    def template_delete(payload: dict[str, object]) -> dict[str, object]:
        code = str(payload.get("code") or "").strip()
        if not code:
            raise HTTPException(status_code=400, detail="template_code_required")
        store.delete_ai_template(code)
        return ok(message="deleted")

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
        raw_success = payload.get("success")
        success = None if raw_success in (None, "") else str(raw_success).lower() in {"1", "true", "yes"}
        data = store.list_ai_usage_logs(
            page=int(payload.get("page") or 1),
            size=int(payload.get("size") or 20),
            keyword=str(payload.get("keyword") or "").strip(),
            user_id=str(payload.get("user_id") or "").strip(),
            model_id=str(payload.get("model_id") or "").strip(),
            deployment_id=str(payload.get("deployment_id") or "").strip(),
            deployment_key=str(payload.get("deployment_key") or "").strip(),
            endpoint=str(payload.get("endpoint") or "").strip(),
            billing_source=str(payload.get("billing_source") or "").strip(),
            response_source=str(payload.get("response_source") or "").strip(),
            success=success,
            start_at=str(payload.get("start_at") or "").strip(),
            end_at=str(payload.get("end_at") or "").strip(),
        )
        return ok(data)

    return router


def create_auth_router(
    auth_service: UserAuthService,
    *,
    ai_client: AiDecisionClient | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/auth", default_response_class=AsciiJSONResponse)
    custom_ai_test_client = ai_client or AiDecisionClient(auth_service.store)

    def ok(data: object = None, message: str = "success") -> dict[str, object]:
        return {"code": 0, "data": data if data is not None else {}, "message": message}

    def execute(action: object) -> dict[str, object]:
        try:
            return ok(action())  # type: ignore[operator]
        except AuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc

    def bearer_token(authorization: str) -> str:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="invalid_session")
        return token.strip()

    @router.post("/register")
    def register(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.register(
            email=str(payload.get("email") or ""),
            password=str(payload.get("password") or ""),
            invite_code=str(payload.get("invite_code") or ""),
        ))

    @router.post("/register/verify")
    def verify_register(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.verify_registration(
            email=str(payload.get("email") or ""),
            code=str(payload.get("code") or ""),
        ))

    @router.post("/login/password")
    def password_login(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.password_login(
            email=str(payload.get("email") or ""),
            password=str(payload.get("password") or ""),
        ))

    @router.post("/login/code/send")
    def send_login_code(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.send_login_code(
            email=str(payload.get("email") or ""),
        ))

    @router.post("/login/code")
    def code_login(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.code_login(
            email=str(payload.get("email") or ""),
            code=str(payload.get("code") or ""),
        ))

    @router.post("/password/forgot")
    def forgot_password(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.forgot_password(
            email=str(payload.get("email") or ""),
        ))

    @router.post("/password/reset")
    def reset_password(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.reset_password(
            email=str(payload.get("email") or ""),
            code=str(payload.get("code") or ""),
            password=str(payload.get("password") or ""),
        ))

    @router.post("/code/resend")
    def resend_code(payload: dict[str, object]) -> dict[str, object]:
        return execute(lambda: auth_service.resend_code(
            email=str(payload.get("email") or ""),
            purpose=str(payload.get("purpose") or ""),
        ))

    @router.get("/me")
    def current_user(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.me(token))

    @router.get("/portal")
    def user_portal(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.portal(token))

    @router.get("/official-strategies")
    def user_official_strategies(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.official_strategies(token))

    @router.get("/ai-model-options")
    def user_ai_model_options(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.ai_model_options(token))

    @router.post("/custom-ai/test")
    def test_custom_ai(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        execute(lambda: auth_service.me(token))
        try:
            result = custom_ai_test_client.test_configuration(
                base_url=str(payload.get("base_url") or ""),
                model=str(payload.get("model") or ""),
                api_key=str(payload.get("api_key") or ""),
                strict_json=True,
            )
            return ok(result, "connection_successful")
        except (RuntimeError, ValueError, TimeoutError) as exc:
            return ok({"success": False, "error": str(exc)}, "connection_failed")

    @router.post("/custom-ai/test-vision")
    def test_custom_ai_vision(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        execute(lambda: auth_service.me(token))
        try:
            result = custom_ai_test_client.test_vision_configuration(
                base_url=str(payload.get("base_url") or ""),
                model=str(payload.get("model") or ""),
                api_key=str(payload.get("api_key") or ""),
            )
            return ok(result, "vision_test_successful")
        except (RuntimeError, ValueError, TimeoutError) as exc:
            return ok({"success": False, "supports_vision": False, "vision_test_status": "failed", "error": str(exc)}, "vision_test_failed")

    @router.get("/custom-strategy/indicators")
    def custom_strategy_indicators(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        execute(lambda: auth_service.me(token))
        return ok({"list": public_indicator_catalog(), "default_output_count": 100})

    @router.get("/custom-strategy/workflow/catalog")
    def custom_strategy_workflow_catalog(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        execute(lambda: auth_service.me(token))
        return ok(workflow_catalog())

    @router.get("/custom-strategy/workflow/schema")
    def custom_strategy_workflow_schema(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        execute(lambda: auth_service.me(token))
        return ok(workflow_json_schema())

    @router.post("/custom-strategy/workflow/validate")
    def validate_custom_strategy_workflow(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        execute(lambda: auth_service.me(token))
        return ok(workflow_validation_result(payload.get("workflow")))

    @router.post("/custom-strategy/workflow/generate")
    def generate_custom_strategy_workflow_stage(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.generate_custom_workflow_stage(token, payload=payload))

    @router.post("/custom-strategy/preview")
    def preview_custom_strategy(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.preview_custom_strategy(token, payload=payload))

    @router.get("/ea-downloads")
    def user_ea_downloads(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.ea_downloads(token))

    @router.get("/agent")
    def agent_dashboard(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=20, ge=1, le=100),
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.agent_dashboard(token, page=page, size=size))

    @router.post("/strategies")
    def create_user_strategy(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.create_strategy(token, payload=payload))

    @router.get("/usage")
    def user_usage(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=10, ge=1, le=100),
        model_id: str = Query(default="", max_length=128),
        deployment_id: str = Query(default="", max_length=128),
        start_at: str = Query(default="", max_length=64),
        end_at: str = Query(default="", max_length=64),
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.usage(
            token,
            page=page,
            size=size,
            model_id=model_id,
            deployment_id=deployment_id,
            start_at=start_at,
            end_at=end_at,
        ))

    @router.post("/usage/screenshot-preview")
    def user_usage_screenshot_preview(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.usage_screenshot_preview(
            token,
            usage_id=str(payload.get("usage_id") or ""),
        ))

    @router.get("/orders")
    def user_orders(
        page: int = Query(default=1, ge=1),
        size: int = Query(default=10, ge=1, le=100),
        deployment_id: str = Query(default="", max_length=128),
        symbol: str = Query(default="", max_length=64),
        start_at: str = Query(default="", max_length=64),
        end_at: str = Query(default="", max_length=64),
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.orders(
            token,
            page=page,
            size=size,
            deployment_id=deployment_id,
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
        ))

    @router.patch("/strategies/{deployment_id}/status")
    def update_user_strategy_status(
        deployment_id: str,
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.update_strategy_status(
            token,
            deployment_id=deployment_id,
            status=str(payload.get("status") or ""),
        ))

    @router.patch("/strategies/{deployment_id}/ai")
    def update_user_strategy_ai(
        deployment_id: str,
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.update_strategy_ai(token, deployment_id=deployment_id, payload=payload))

    @router.patch("/strategies/{deployment_id}")
    def update_user_strategy_settings(
        deployment_id: str,
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.update_strategy_settings(token, deployment_id=deployment_id, payload=payload))

    @router.delete("/strategies/{deployment_id}")
    def delete_user_strategy(
        deployment_id: str,
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.delete_strategy(token, deployment_id=deployment_id))

    @router.post("/profile")
    def update_profile(
        payload: dict[str, object],
        authorization: str = Header(default=""),
    ) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.update_profile(
            token,
            nickname=str(payload.get("nickname") or ""),
        ))

    @router.post("/password/change")
    def change_password(payload: dict[str, object], authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.change_password(
            token,
            current_password=str(payload.get("current_password") or ""),
            new_password=str(payload.get("new_password") or ""),
        ))

    @router.post("/email/change/current/send")
    def send_current_email_code(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.send_current_email_code(token))

    @router.post("/email/change/send")
    def send_change_email_code(payload: dict[str, object], authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.send_change_email_code(
            token,
            email=str(payload.get("email") or ""),
        ))

    @router.post("/email/change/verify")
    def verify_change_email(payload: dict[str, object], authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.verify_change_email(
            token,
            email=str(payload.get("email") or ""),
            current_email_code=str(payload.get("current_email_code") or ""),
            new_email_code=str(payload.get("new_email_code") or ""),
        ))

    @router.get("/sessions")
    def list_sessions(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.list_sessions(token))

    @router.post("/sessions/revoke")
    def revoke_session(payload: dict[str, object], authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.revoke_session_by_id(
            token,
            session_id=str(payload.get("session_id") or ""),
        ))

    @router.post("/logout-all")
    def logout_all(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.logout_all(token))

    @router.post("/logout")
    def logout(authorization: str = Header(default="")) -> dict[str, object]:
        token = bearer_token(authorization)
        return execute(lambda: auth_service.logout(token))

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
            _screenshot_request_fingerprint(request),
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


def _request_screenshot(data_type: str, screenshot: object) -> tuple[str, dict[str, object]]:
    if data_type not in {"screenshot", "both"}:
        return "", {}
    if screenshot is None:
        raise ScreenshotError("screenshot_required")
    mime_type = str(getattr(screenshot, "mime_type", "") or "")
    encoded = str(getattr(screenshot, "base64", "") or "")
    return prepare_screenshot(mime_type, encoded)


def _screenshot_request_fingerprint(request: Mt5OpenDecisionRequest | Mt5PositionDecisionRequest) -> str:
    if request.market.screenshot_id:
        return str(request.market.screenshot_id)
    screenshot = request.market.screenshot
    if screenshot is None or not screenshot.base64:
        return "no_screenshot"
    return sha256(str(screenshot.base64).encode("ascii", errors="ignore")).hexdigest()[:16]


def _normalize_epoch_seconds(value: int) -> int:
    timestamp = int(value)
    if timestamp > 10_000_000_000_000:
        return timestamp // 1_000_000
    if timestamp > 10_000_000_000:
        return timestamp // 1_000
    return timestamp


def _candles(bars: list[Mt5Bar]) -> list[Candle]:
    candles = [
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
    # MT arrays may arrive newest-first, while strategy engines consistently
    # treat [-1] as the latest closed candle.
    return sorted(candles, key=lambda item: item.timestamp)


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
                comment=_strategy_order_comment(decision),
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


def _strategy_order_comment(decision: TradeDecision) -> str:
    setup_code = str(decision.metadata.get("setup_code") or "")
    setup_version = int(decision.metadata.get("setup_version") or 0)
    abbreviations = {
        "breakout_long": "BOL",
        "breakout_short": "BOS",
        "breakout_retest_long": "BRL",
        "breakout_retest_short": "BRS",
        "trend_continuation_long": "TCL",
        "trend_continuation_short": "TCS",
        "pullback_h1_long": "PH1",
        "pullback_h2_long": "PH2",
        "pullback_l1_short": "PL1",
        "pullback_l2_short": "PL2",
    }
    short_code = abbreviations.get(setup_code)
    if setup_version <= 0 or not short_code:
        return "GainLabAI"
    decision_token = decision.decision_id.rsplit("_", 1)[-1][-8:]
    return f"GL{setup_version}-{short_code}-{decision_token}"[:31]


def _mt5_validate_deployment_account(
    store: SqliteStore,
    deployment: dict,
    deployment_key: str,
    account: AccountIdentity,
) -> tuple[dict, str | None]:
    try:
        bound_deployment = store.bind_deployment_login(
            deployment_key,
            login=account.login,
            platform=account.platform,
            server=account.server,
        )
        return bound_deployment or deployment, None
    except RuntimeError as exc:
        error_code = str(exc)
        if error_code in {"deployment_account_mismatch", "invalid_deployment_account"}:
            return deployment, error_code
        raise


def _mt5_business_error_description(
    error_code: str,
    *,
    include_code: bool = True,
) -> str:
    messages = {
        "invalid_deployment_key": "策略 Key 无效，请检查 EA 配置",
        "deployment_not_active": "策略未启用，请在用户中心检查策略状态",
        "deployment_owner_unavailable": "策略所属用户不存在，请联系管理员",
        "account_not_active": "用户账号不可用，请联系管理员",
        "vip_required": "VIP 未开通，策略分析已停止",
        "vip_expired": "VIP 已到期，策略分析已停止",
        "deployment_account_mismatch": "当前 MT 账号与策略绑定账号不一致，请在用户中心修改绑定账号",
        "invalid_deployment_account": "MT 账号无效，请检查账号后重试",
        "insufficient_balance": "AI 余额不足，策略分析已停止，请充值后继续使用",
        "screenshot_required": "EA未提供策略所需截图，本次分析已停止",
        "unsupported_screenshot_type": "截图格式不支持，请使用PNG、JPEG或WebP",
        "invalid_screenshot_base64": "截图Base64数据无效，本次分析已停止",
        "invalid_screenshot_image": "截图文件内容无效，本次分析已停止",
        "empty_screenshot": "截图内容为空，本次分析已停止",
        "screenshot_too_large": "截图超过5MB，本次分析已停止",
        "screenshot_type_mismatch": "截图格式与声明类型不一致，本次分析已停止",
    }
    message = messages.get(error_code, "策略暂时不可用")
    return f"{message}（{error_code}）" if include_code else message


def _mt5_open_error_response(
    request: Mt5OpenDecisionRequest,
    request_id: str,
    error_code: str,
) -> Mt5OpenDecisionResponse:
    return Mt5OpenDecisionResponse(
        status="ok",
        should_open=False,
        description=_mt5_business_error_description(error_code),
        spread=request.market.spread,
        decision_id=f"dec_rejected_{sha256((request_id + error_code).encode('utf-8')).hexdigest()[:24]}",
        request_id=request_id,
        orders_count=0,
        orders=[],
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
                volume=min(decision.volume, target.volume) if decision.volume else target.volume,
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


def _mt5_position_error_response(
    request: Mt5PositionDecisionRequest,
    request_id: str,
    error_code: str,
) -> Mt5PositionDecisionResponse:
    return Mt5PositionDecisionResponse(
        status="ok",
        has_action=False,
        description=_mt5_business_error_description(error_code),
        spread=request.market.spread,
        decision_id=f"dec_rejected_{sha256((request_id + error_code).encode('utf-8')).hexdigest()[:24]}",
        request_id=request_id,
        actions_count=0,
        actions=[],
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

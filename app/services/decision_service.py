from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.models import (
    AccountIdentity,
    OpenEvaluateRequest,
    PositionEvaluateRequest,
    TradeDecision,
)
from app.store import SqliteStore
from app.strategies.base import StrategyEngine


class DecisionService:
    def __init__(
        self,
        store: SqliteStore,
        strategies: dict[str, StrategyEngine],
    ) -> None:
        self.store = store
        self.strategies = strategies

    def authenticate(
        self,
        deployment_key: str,
        account: AccountIdentity,
    ) -> dict[str, Any]:
        deployment = self.store.find_deployment_by_key(deployment_key)
        if deployment is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_deployment_key",
            )
        self.ensure_deployment_access(deployment)
        return deployment

    def ensure_deployment_access(self, deployment: dict[str, Any]) -> None:
        access_error = self.deployment_access_error(deployment)
        if access_error is not None:
            raise HTTPException(
                status_code=(
                    status.HTTP_401_UNAUTHORIZED
                    if access_error == "invalid_deployment_key"
                    else status.HTTP_403_FORBIDDEN
                ),
                detail=access_error,
            )

    def deployment_access_error(self, deployment: dict[str, Any]) -> str | None:
        # Older builds auto-created an active PA mock deployment for every unknown
        # gl_* key received by MT init. Never allow those development-only records
        # to authenticate or reach a trading decision, even if they remain stored.
        if (
            str(deployment.get("user_id") or "").strip() == "mt5_runtime"
            and str(deployment.get("strategy_code") or "").strip() == "PA_MOCK_V1"
        ):
            return "invalid_deployment_key"
        if deployment["status"] != "active":
            return "deployment_not_active"
        raw_user_id = str(deployment.get("user_id") or "").strip()
        # Local demonstration deployments use non-numeric owners. Real user deployments
        # always use the numeric users.id primary key and must pass account/VIP checks.
        if not raw_user_id.isdigit():
            return None
        user = self.store.get_user(int(raw_user_id))
        if user is None:
            return "deployment_owner_unavailable"
        if str(user.get("status") or "") != "active":
            return "account_not_active"
        if int(user.get("vip_level") or 0) <= 0:
            return "vip_required"
        if not bool(user.get("vip_active")):
            return "vip_expired"
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        uses_official_ai = any(
            str(config.get(f"{prefix}_ai_mode") or "official").strip().lower() != "custom"
            for prefix in ("open", "position")
        )
        if uses_official_ai and bool(user.get("credit_exhausted")):
            return "insufficient_balance"
        return None

    def evaluate_open(self, request: OpenEvaluateRequest) -> TradeDecision:
        deployment = self.authenticate(request.deployment_key, request.account)
        return self._evaluate(
            endpoint="open",
            deployment=deployment,
            request=request,
        )

    def evaluate_position(
        self,
        request: PositionEvaluateRequest,
    ) -> TradeDecision:
        deployment = self.authenticate(request.deployment_key, request.account)
        return self._evaluate(
            endpoint="position",
            deployment=deployment,
            request=request,
        )

    def _evaluate(
        self,
        *,
        endpoint: str,
        deployment: dict[str, Any],
        request: OpenEvaluateRequest | PositionEvaluateRequest,
    ) -> TradeDecision:
        self.store.record_deployment_activity(
            deployment["id"],
            strategy_code=deployment["strategy_code"],
            event_type=endpoint,
        )
        existing = self.store.get_decision(
            deployment["id"],
            endpoint,
            request.request_id,
        )
        if existing is not None:
            existing["idempotent"] = True
            return TradeDecision.model_validate(existing)

        strategy = self.strategies.get(deployment["strategy_code"])
        if strategy is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="strategy_engine_unavailable",
            )

        if endpoint == "open":
            assert isinstance(request, OpenEvaluateRequest)
            decision = strategy.evaluate_open(request, deployment)
        else:
            assert isinstance(request, PositionEvaluateRequest)
            decision = strategy.evaluate_position(request, deployment)

        payload = decision.model_dump(mode="json")
        saved = self.store.save_decision(
            deployment["id"],
            endpoint,
            request.request_id,
            payload,
            account_login=request.account.login,
            account_server=request.account.server,
            symbol=request.symbol.upper(),
            timeframe=request.timeframe.upper(),
        )
        return TradeDecision.model_validate(saved)

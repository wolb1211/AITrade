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
        if deployment["status"] != "active":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="deployment_not_active",
            )
        return deployment

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

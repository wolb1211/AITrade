"""The public AI interface: an OpenAI-compatible surface for user API keys.

It is deliberately separate from the strategy routers. A strategy key describes a
symbol, a timeframe and a strategy and is pasted into an EA; these keys identify a
user only, so a leaked strategy key cannot reach this surface and revoking one does
not disturb the other.

The provider, its base url and our key never leave the server: a caller sends the
model name it read from /v1/models, and the mapping happens here.
"""

from __future__ import annotations

import json
from collections import deque
from decimal import Decimal
from threading import Lock
from time import monotonic
from typing import Any
from urllib.request import Request, urlopen

from fastapi import APIRouter, Header, HTTPException, status

from app.store import decimal_string


def _bearer(authorization: str) -> str:
    text = str(authorization or "").strip()
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text


class _RateLimiter:
    """A per-key requests-per-minute window, counted in this process."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, key_id: str, limit: int) -> bool:
        if limit <= 0:
            return True
        now = monotonic()
        with self._lock:
            window = self._hits.setdefault(key_id, deque())
            while window and now - window[0] > 60.0:
                window.popleft()
            if len(window) >= limit:
                return False
            window.append(now)
            return True


def create_public_api_router(store: Any) -> APIRouter:
    router = APIRouter(prefix="/v1")
    limiter = _RateLimiter()

    def authenticate(authorization: str) -> dict[str, Any]:
        record = store.find_user_api_key(_bearer(authorization))
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
            )
        if str(record.get("status") or "") != "active":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": {"message": "API key revoked", "type": "invalid_request_error"}},
            )
        if not _rate_limiter_allows(limiter, record):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
            )
        return record

    @router.get("/models")
    def list_models(authorization: str = Header(default="")) -> dict[str, Any]:
        """The models a user can call, with the prices they are billed at."""
        authenticate(authorization)
        options = store.list_public_ai_model_options().get("list") or []
        return {
            "object": "list",
            "data": [
                {
                    "id": str(item.get("model") or ""),
                    "object": "model",
                    "owned_by": str(item.get("provider_name") or ""),
                    "display_name": str(item.get("display_name") or ""),
                    "supports_vision": bool(item.get("supports_vision")),
                    "pricing": {
                        "currency": "CNY",
                        "unit": "per_million_tokens",
                        "input": str(item.get("input_price_per_million") or "0"),
                        "output": str(item.get("output_price_per_million") or "0"),
                    },
                }
                for item in options
                if str(item.get("model") or "").strip()
            ],
        }

    @router.post("/chat/completions")
    def chat_completions(
        payload: dict[str, Any],
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        """Relay one chat completion, billed against the caller's AI balance.

        The provider and its key stay here. The caller sends the model name it read
        from /v1/models and gets the provider's answer back, with the token usage
        it was charged for.
        """
        record = authenticate(authorization)
        if bool(payload.get("stream")):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"message": "streaming is not supported", "type": "invalid_request_error"}},
            )
        model_name = str(payload.get("model") or "").strip()
        endpoint = store.get_public_ai_endpoint(model_name)
        if endpoint is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": {"message": f"model {model_name or '?'} not found", "type": "invalid_request_error"}},
            )

        user_id = str(record.get("user_id") or "")
        user = store.get_user(int(user_id)) if user_id.isdigit() else None
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": {"message": "account not found", "type": "invalid_request_error"}},
            )
        # No overdraft: an empty balance stops here rather than growing a debt.
        if Decimal(str(user.get("ai_balance") or 0)) <= 0:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={"error": {"message": "AI balance is empty", "type": "insufficient_quota"}},
            )

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"message": "messages is required", "type": "invalid_request_error"}},
            )

        body: dict[str, Any] = {
            "model": str(endpoint.get("model") or model_name),
            "messages": messages,
            "max_tokens": _max_tokens(payload.get("max_tokens")),
        }
        for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "stop"):
            if payload.get(key) is not None:
                body[key] = payload[key]

        started = monotonic()
        try:
            answer = _forward(endpoint, body)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - the provider is outside our control
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"error": {"message": f"upstream failure: {type(exc).__name__}", "type": "upstream_error"}},
            ) from exc

        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        store.touch_user_api_key(str(record.get("id") or ""))
        store.save_ai_usage_log({
            "user_id": user_id,
            "deployment_id": "",
            "strategy_code": "",
            "endpoint": "api",
            "provider_id": str(endpoint.get("id") or ""),
            "model_id": str(endpoint.get("model") or ""),
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "billing_source": "official",
            "input_price_snapshot": decimal_string(endpoint.get("input_price_per_million") or 0),
            "output_price_snapshot": decimal_string(endpoint.get("output_price_per_million") or 0),
            "cache_input_price_snapshot": decimal_string(endpoint.get("cache_input_price_per_million") or 0),
            "provider_called": True,
            "response_source": "provider",
            "success": True,
            "elapsed_ms": int((monotonic() - started) * 1000),
        })
        return answer

    return router


def _max_tokens(value: Any, default: int = 4096, ceiling: int = 16384) -> int:
    """A bounded completion length, so one call cannot run away with a balance."""
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(wanted, ceiling))


def _forward(endpoint: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Send the request to the provider with our key and return its answer."""
    base_url = str(endpoint.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("endpoint_missing_base_url")
    url = base_url if base_url.lower().endswith("/chat/completions") else f"{base_url}/chat/completions"
    request_obj = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {str(endpoint.get('api_key') or '')}",
        },
        method="POST",
    )
    with urlopen(request_obj, timeout=60.0) as response:
        return json.loads(response.read().decode("utf-8"))


def _rate_limiter_allows(limiter: _RateLimiter, record: dict[str, Any]) -> bool:
    try:
        limit = int(record.get("rpm_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    return limiter.allow(str(record.get("id") or ""), limit)

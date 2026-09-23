"""The public AI interface: an OpenAI-compatible surface for user API keys.

It is deliberately separate from the strategy routers. A strategy key describes a
symbol, a timeframe and a strategy and is pasted into an EA; these keys identify a
user only, so a leaked strategy key cannot reach this surface and revoking one does
not disturb the other.

The provider, its base url and our key never leave the server: a caller sends the
model name it read from /v1/models, and the mapping happens here.
"""

from __future__ import annotations

from collections import deque
from threading import Lock
from time import monotonic
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status

from app.security import api_key_prefix  # noqa: F401 - kept for callers importing from here


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

    return router


def _rate_limiter_allows(limiter: _RateLimiter, record: dict[str, Any]) -> bool:
    try:
        limit = int(record.get("rpm_limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    return limiter.allow(str(record.get("id") or ""), limit)

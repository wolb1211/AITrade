"""A user's own AI key is called every time; the platform's models may be cached.

Someone who brings their own provider key pays that provider and can audit the
calls there, so a cached answer would just look like a missing call. The platform's
own models are the ones the cache is for.
"""

from __future__ import annotations

from typing import Any

from app.services.ai_service import AiCallResult, AiDecisionClient


class _Client(AiDecisionClient):
    """Counts the underlying calls and never touches a store."""

    def __init__(self, *, is_custom: bool) -> None:
        self.is_custom = is_custom
        self.calls = 0

    def _select_model(self, deployment: dict[str, Any], endpoint: str) -> dict[str, Any]:
        return {
            "is_custom": self.is_custom,
            "provider_id": "p",
            "provider_base_url": "https://example.test/v1",
            "provider_api_key": "k",
            "model": "m",
            "strict_json": True,
        }

    def _chat_json_uncached(self, **kwargs: Any) -> Any:
        self.calls += 1
        return AiCallResult(content={"reason": "ok"}, usage=None)

    # The cache plumbing asks the store; a custom key must never get that far.
    class _Store:
        def get_ai_cache_settings(self) -> dict[str, Any]:
            return {"enabled": True, "ttl_seconds": 120}

        def get_ai_response_cache(self, key: str) -> Any:
            raise AssertionError("custom AI must not read the response cache")

        def save_ai_response_cache(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("custom AI must not write the response cache")

    @property
    def store(self) -> Any:  # type: ignore[override]
        return _Client._Store()


def _call(client: _Client) -> Any:
    return client._chat_json(
        deployment={"config": {}},
        endpoint="open",
        system_prompt="s",
        user_payload={"symbol": "XAUUSD"},
    )


def test_a_custom_ai_key_is_called_every_time() -> None:
    client = _Client(is_custom=True)

    _call(client)
    _call(client)
    _call(client)

    assert client.calls == 3

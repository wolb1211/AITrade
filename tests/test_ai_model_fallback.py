"""A provider failure retries once on the backup model, and says so on the panel.

The models behind the platform fail in different ways: one gets rate limited, one
times out. Without a backup the whole risk check falls back to "conservative,
no trade" and the strategy simply stops taking entries. The retry is limited to
technical failures - a model that answers "no" is a verdict, and re-asking another
model would let the risk gate be talked past by switching provider.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.ai_service import AiCallResult, AiDecisionClient


class _Client(AiDecisionClient):
    """An AiDecisionClient whose underlying call is replaced per model."""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []

    def _chat_json_uncached(self, **kwargs: Any) -> Any:
        model = str(kwargs.get("model") or "")
        self.calls.append(model)
        outcome = self.outcomes.get(model)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _result(analysis: str = "") -> AiCallResult:
    return AiCallResult(content={"reason": "观望", "analysis": analysis}, usage=None)


def _call(client: _Client, model: str, config: dict[str, Any] | None = None) -> Any:
    return client._chat_with_model_fallback(
        deployment={"config": config or {}},
        endpoint="open",
        system_prompt="s",
        user_payload={},
        model=model,
    )


def test_a_rate_limited_model_is_retried_on_the_backup() -> None:
    client = _Client({
        "gemini-3.8-flash": RuntimeError("AI provider HTTP 429: Resource has been exhausted"),
        "qwen-plus": _result("原有分析"),
    })

    result = _call(client, "gemini-3.8-flash")

    assert client.calls == ["gemini-3.8-flash", "qwen-plus"]
    assert result is not None
    # The operator has to see which model failed and which one answered.
    assert "模型 gemini-3.8-flash 出错" in result.content["analysis"]
    assert "qwen-plus" in result.content["analysis"]
    assert "更换模型" in result.content["analysis"]
    assert result.content["analysis"].endswith("原有分析")


def test_a_timeout_is_retried_too() -> None:
    client = _Client({"a-model": TimeoutError("slow"), "qwen-plus": _result()})

    assert _call(client, "a-model") is not None
    assert client.calls == ["a-model", "qwen-plus"]


def test_the_backup_model_can_be_chosen_or_switched_off() -> None:
    client = _Client({"a-model": RuntimeError("boom"), "gpt-5.5": _result()})

    assert _call(client, "a-model", {"fallback_model": "gpt-5.5"}) is not None
    assert client.calls == ["a-model", "gpt-5.5"]

    # An empty setting disables the fallback and the failure propagates.
    failing = _Client({"a-model": RuntimeError("boom"), "qwen-plus": _result()})
    with pytest.raises(RuntimeError):
        _call(failing, "a-model", {"fallback_model": ""})
    assert failing.calls == ["a-model"]


def test_the_model_in_use_is_never_retried_against_itself() -> None:
    client = _Client({"qwen-plus": RuntimeError("boom")})

    with pytest.raises(RuntimeError):
        _call(client, "qwen-plus")

    assert client.calls == ["qwen-plus"]


def test_a_verdict_is_never_re_asked_elsewhere() -> None:
    """A "no" is an answer: retrying it on another model would bypass the gate."""
    refusal = _result("不符合条件")
    client = _Client({"a-model": refusal, "qwen-plus": _result("换我说")})

    result = _call(client, "a-model")

    assert client.calls == ["a-model"]
    assert result is refusal

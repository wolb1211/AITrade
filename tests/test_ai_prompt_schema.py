"""The wrapper prompt must demand the keys each strategy actually reads.

_json_api_system_prompt supplies a generic shape per endpoint, and the model
follows that system prompt rather than the payload hint. When the two disagree
the strategy silently reads defaults: the GL entry gate asked for allow_open and
risk_level while the wrapper demanded should_open, so every verdict looked
unreadable - entries opened regardless before the gate was made strict, and none
opened at all afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib import request

from app.services.ai_service import (
    _TURTLE_OPEN_RISK_SCHEMA,
    _TURTLE_POSITION_REVIEW_SCHEMA,
    AiDecisionClient,
    _json_api_system_prompt,
)
from app.store import SqliteStore


def test_caller_schema_replaces_the_generic_shape() -> None:
    opened = _json_api_system_prompt("open", "task", schema_override=_TURTLE_OPEN_RISK_SCHEMA)
    assert "allow_open" in opened
    assert "risk_level" in opened
    assert "should_open" not in opened

    position = _json_api_system_prompt(
        "position", "task", schema_override=_TURTLE_POSITION_REVIEW_SCHEMA
    )
    assert "close_now" in position
    assert "allow_add" in position
    assert "close_scope" not in position


def test_the_generic_shape_is_unchanged_without_an_override() -> None:
    """Other callers keep the shape they have always been given."""
    prompt = _json_api_system_prompt("open", "task")
    assert "should_open" in prompt
    assert "allow_open" not in prompt


def test_gl_schemas_carry_every_key_the_strategy_reads() -> None:
    """Coupling guard: these are the exact keys read out of the AI content."""
    for key in ("allow_open", "risk_level", "reason", "analysis"):
        assert key in _TURTLE_OPEN_RISK_SCHEMA
    for key in ("close_now", "allow_add", "risk_level", "reason", "analysis"):
        assert key in _TURTLE_POSITION_REVIEW_SCHEMA


def test_a_stray_question_mark_is_removed_from_chinese_prose() -> None:
    """The models drop a half-width "?" where they are unsure.

    It reached the customer panel as noise - "多头排列，?格局突破前高" - which reads
    as a defect. Written Chinese uses the full-width form, so the half-width mark
    touching Chinese text is an artefact.
    """
    from app.services.ai_service import _strip_placeholder_marks

    assert (
        _strip_placeholder_marks("市场结构为多头排列，?格局突破前高")
        == "市场结构为多头排列，格局突破前高"
    )
    assert (
        _strip_placeholder_marks("形态评分中等偏高，?能动能持续?")
        == "形态评分中等偏高，能动能持续"
    )
    # A mark that belongs to Latin text, a figure or an identifier is left alone,
    # so a genuine question or a value is never altered.
    assert _strip_placeholder_marks("Why?") == "Why?"
    assert _strip_placeholder_marks("评分 72? ") == "评分 72? "
    assert _strip_placeholder_marks("RSI?超买") == "RSI?超买"


def test_the_parser_cleans_the_reason_it_returns() -> None:
    """The scrub runs where every decision's text is finalised."""
    from app.services.ai_service import _extract_json_object

    parsed = _extract_json_object(
        '{"close_now":false,"allow_add":true,"reason":"?趋势仍在修复上行",'
        '"analysis":"市场结构为多头排列，?格局突破前高，浮盈不足以提前止盈。"}',
        endpoint="position",
    )

    assert parsed["reason"] == "趋势仍在修复上行"
    assert parsed["analysis"] == "市场结构为多头排列，格局突破前高，浮盈不足以提前止盈。"


def test_a_chat_call_carries_the_caller_schema_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """The whole call chain must accept the schema and act on it.

    The override travels from the caller through the chat helpers into the
    request builder; a stray keyword anywhere in that chain breaks every live AI
    call, which is exactly what shipped once because only the prompt text was
    covered and not the call itself.
    """
    store = SqliteStore(tmp_path / "schema-passthrough.db")
    store.initialize()
    client = AiDecisionClient(store)
    bodies: list[dict] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"allow_open\\": true}"}}]}'

    def fake_urlopen(req: request.Request, timeout: float):
        bodies.append(json.loads((req.data or b"{}").decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr(request, "urlopen", fake_urlopen)
    result = client._chat_json_uncached(
        deployment={},
        endpoint="open",
        system_prompt="entry risk gate",
        user_payload={"task": "turtle_open_risk_filter"},
        model={
            "provider_id": "prov_schema_test",
            "provider_base_url": "https://api.example.com/v1",
            "provider_api_key": "sk-schema-test",
            "model": "example-model",
            "strict_json": True,
        },
        response_schema=_TURTLE_OPEN_RISK_SCHEMA,
    )

    assert result is not None
    system_message = str(bodies[0]["messages"][0]["content"])
    assert "allow_open" in system_message
    assert "should_open" not in system_message


"""The wrapper prompt must demand the keys each strategy actually reads.

_json_api_system_prompt supplies a generic shape per endpoint, and the model
follows that system prompt rather than the payload hint. When the two disagree
the strategy silently reads defaults: the GL entry gate asked for allow_open and
risk_level while the wrapper demanded should_open, so every verdict looked
unreadable - entries opened regardless before the gate was made strict, and none
opened at all afterwards.
"""

from __future__ import annotations

from app.services.ai_service import (
    _TURTLE_OPEN_RISK_SCHEMA,
    _TURTLE_POSITION_REVIEW_SCHEMA,
    _json_api_system_prompt,
)


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

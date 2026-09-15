"""Guard tests for the PA stage-1 diagnosis gates.

A production regression showed what happens when the gate semantics drift: the
diagnosis prompt asked the model for a boolean ``passed`` per gate without
defining what passing meant, and the model read ``gate3_extreme_location``
literally. It answered ``passed: false`` whenever price sat mid-range, the
strategy turned that into a veto, and no trade was opened for two days even
though the same conditions used to produce orders every day.

These tests pin the intended semantics:

* only gate1, gate2 and gate4 may block;
* gate3 is a warning that steers the order type and never blocks;
* the prompts state the meaning and the default of every gate;
* the verdicts reach the persisted decision.
"""

from __future__ import annotations

from app.services.ai_service import _pa_diagnosis_system_prompt, _pa_system_prompt
from app.strategies.pa_agent_lite import (
    ALL_GATES,
    BLOCKING_GATES,
    _blocking_gate_reason,
    _diagnosis_metadata,
    _gate_verdicts,
)


def _diagnosis(**gates: bool) -> dict[str, object]:
    return {
        "cycle": "normal_channel",
        "direction": "bullish",
        "gates": {key: {"passed": value, "reason": "测试理由"} for key, value in gates.items()},
    }


def test_gate3_never_blocks_an_entry() -> None:
    """The exact production failure: gate3 false plus everything else true."""
    diagnosis = _diagnosis(
        gate1_no_trade_environment=True,
        gate2_direction_clear=True,
        gate3_extreme_location=False,
        gate4_stop_definable=True,
    )
    assert _blocking_gate_reason(diagnosis) == ""


def test_gate3_is_not_a_blocking_gate() -> None:
    assert "gate3_extreme_location" not in BLOCKING_GATES
    assert set(BLOCKING_GATES) | {"gate3_extreme_location"} == set(ALL_GATES)


def test_every_blocking_gate_still_blocks() -> None:
    for key in BLOCKING_GATES:
        reason = _blocking_gate_reason(
            {"cycle": "normal_channel", "gates": {key: {"passed": False, "reason": "具体证据"}}}
        )
        assert "阶段一闸门未通过" in reason
        assert "具体证据" in reason


def test_extreme_range_still_blocks() -> None:
    assert "极端震荡" in _blocking_gate_reason({"cycle": "extreme_tr"})


def test_malformed_gates_never_block() -> None:
    """A chatty or broken model must not be able to silently disable trading."""
    assert _blocking_gate_reason({}) == ""
    assert _blocking_gate_reason({"gates": "nonsense"}) == ""
    assert _blocking_gate_reason({"gates": {"gate1_no_trade_environment": {"passed": "no"}}}) == ""
    assert _blocking_gate_reason({"gates": {"gate1_no_trade_environment": {}}}) == ""


def test_gate_verdicts_record_only_explicit_booleans() -> None:
    diagnosis = _diagnosis(
        gate1_no_trade_environment=True,
        gate2_direction_clear=True,
        gate3_extreme_location=False,
        gate4_stop_definable=True,
    )
    assert _gate_verdicts(diagnosis) == {
        "gate1_no_trade_environment": True,
        "gate2_direction_clear": True,
        "gate3_extreme_location": False,
        "gate4_stop_definable": True,
    }
    assert _gate_verdicts({"gates": {"gate1_no_trade_environment": {"passed": "true"}}}) == {}
    assert _gate_verdicts(None) == {}


def test_diagnosis_metadata_is_persistable() -> None:
    metadata = _diagnosis_metadata(
        _diagnosis(
            gate1_no_trade_environment=True,
            gate2_direction_clear=True,
            gate3_extreme_location=False,
            gate4_stop_definable=True,
        )
    )
    assert metadata["stage1_cycle"] == "normal_channel"
    assert metadata["stage1_direction"] == "bullish"
    assert metadata["stage1_gate3_advisory"] is True
    assert metadata["stage1_gate_passed"]["gate3_extreme_location"] is False
    # A passing gate3 is not an advisory.
    assert _diagnosis_metadata(_diagnosis(gate3_extreme_location=True))["stage1_gate3_advisory"] is False
    assert _diagnosis_metadata(None) == {}


def test_diagnosis_prompt_defines_every_gate() -> None:
    prompt = _pa_diagnosis_system_prompt()
    for key in ALL_GATES:
        assert key in prompt
    # The definition that was missing before the fix.
    assert "only a warning about chasing" in prompt
    assert "middle of a range" in prompt
    assert "default every gate to true" in prompt


def test_order_prompt_switches_to_a_pending_order_when_gate3_fails() -> None:
    prompt = _pa_system_prompt()
    assert "gate3_extreme_location" in prompt
    assert "never answer order_type market" in prompt
    assert "limit or stop order" in prompt

"""The panel and the call log must show the AI's own words in plain Chinese.

The customer reads this text as proof that the analysis is real, so a payload
field name echoed back in English ("favorable_atr仅0.91") undermines exactly the
thing the log exists to demonstrate.
"""

from __future__ import annotations

import json

import pytest

from app.services.ai_service import (
    _PLAIN_CHINESE_RULE,
    _extract_json_object,
    _pa_diagnosis_system_prompt,
    _pa_system_prompt,
    _scrub_internal_field_names,
    _turtle_open_risk_system_prompt,
    _turtle_position_review_prompt,
)


# ── the scrub itself ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Taken from a real GL position review: the model echoed both keys.
        (
            "两空仓均浮亏，favorable_atr分别为+0.215和-0.318",
            "两空仓均浮亏，浮盈ATR倍数分别为+0.215和-0.318",
        ),
        (
            "add_candidate为sell，入场价77258.64与现价重合",
            "加仓候选为sell，入场价77258.64与现价重合",
        ),
        (
            "close_now 为 false，risk_level 为 low",
            "是否立即平仓 为 false，风险等级 为 low",
        ),
        # The longer key must win over its own prefix.
        (
            "bar_by_bar_summary 显示重叠",
            "逐棒分析 显示重叠",
        ),
        ("allow_add和allow_open均已给出", "加仓许可和开仓许可均已给出"),
    ],
)
def test_echoed_field_names_are_rendered_in_chinese(raw: str, expected: str) -> None:
    assert _scrub_internal_field_names(raw) == expected


def test_scrub_leaves_normal_prose_alone() -> None:
    text = "当前处于震荡区间，价格位于区间中部，止损仍远于市价，风险可控。"
    assert _scrub_internal_field_names(text) == text
    assert _scrub_internal_field_names("") == ""


def test_scrub_does_not_touch_a_longer_word_that_merely_contains_a_key() -> None:
    """`favorable_atmosphere` is not the payload key `favorable_atr`."""
    text = "a favorable_atmosphere is not a field"
    assert _scrub_internal_field_names(text) == text


# ── the normalizer applies it everywhere the model writes prose ───────────────


def _position_content(**overrides):
    payload = {
        "action": "hold",
        "ticket": None,
        "direction": None,
        "confidence": 0.4,
        "reason": "未达止盈或反转信号",
        "analysis": "两空仓均浮亏，favorable_atr分别为+0.215和-0.318，风险可控。",
    }
    payload.update(overrides)
    return payload


def test_analysis_is_scrubbed_before_it_can_reach_the_panel() -> None:
    parsed = _extract_json_object(
        json.dumps(_position_content(), ensure_ascii=False), endpoint="position"
    )

    assert "favorable_atr" not in parsed["analysis"]
    assert "浮盈ATR倍数" in parsed["analysis"]


def test_reason_is_scrubbed_too() -> None:
    parsed = _extract_json_object(
        json.dumps(
            _position_content(reason="risk_level 偏低，继续持有"),
            ensure_ascii=False,
        ),
        endpoint="position",
    )

    assert "risk_level" not in parsed["reason"]
    assert "风险等级" in parsed["reason"]


def test_open_endpoint_is_scrubbed_too() -> None:
    parsed = _extract_json_object(
        json.dumps(
            {
                "should_open": False,
                "direction": None,
                "confidence": 0.3,
                "reason": "setup_score 不足",
                "analysis": "cycle_position 为震荡区间，follow_through 失败。",
            },
            ensure_ascii=False,
        ),
        endpoint="open",
    )

    assert "setup_score" not in parsed["reason"]
    assert "形态评分" in parsed["reason"]
    assert "cycle_position" not in parsed["analysis"]
    assert "跟随情况" in parsed["analysis"]


def test_diagnosis_reasoning_is_scrubbed_without_gaining_placeholders() -> None:
    parsed = _extract_json_object(
        json.dumps(
            {
                "cycle": "trading_range",
                "direction": "neutral",
                "gates": {},
                "reasoning": "cycle_position 判定为交易区间，trend_relationship 为中性背景。",
            },
            ensure_ascii=False,
        ),
        endpoint="pa_diag",
    )

    assert "cycle_position" not in parsed["reasoning"]
    assert "周期位置" in parsed["reasoning"]
    assert "reason" not in parsed
    assert "analysis" not in parsed


# ── the prompts ask for it in the first place ─────────────────────────────────


@pytest.mark.parametrize(
    "prompt_factory",
    [
        _pa_system_prompt,
        _pa_diagnosis_system_prompt,
        _turtle_position_review_prompt,
        _turtle_open_risk_system_prompt,
    ],
)
def test_every_strategy_prompt_forbids_english_field_names(prompt_factory) -> None:
    prompt = prompt_factory()

    assert _PLAIN_CHINESE_RULE in prompt
    assert "Never output internal field names" in prompt

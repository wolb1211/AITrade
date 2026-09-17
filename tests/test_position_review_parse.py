"""Reproduce the reported "not standard JSON" verdict on a valid object.

The admin preview showed a perfectly ordinary position review being rejected:

    {"close_now":false,"allow_add":true,"risk_level":"low","confidence":0.72,
     "reason":"趋势仍在修复上行，浮盈不足以提前止盈"}

If these pass, the parser is fine and the live content differed from what the
preview displayed; if they fail, the parser is the bug.
"""

from __future__ import annotations

from app.services.ai_service import _extract_json_object

_SHORT = (
    '{"close_now":false,"allow_add":true,"risk_level":"low","confidence":0.72,'
    '"reason":"趋势仍在修复上行，浮盈不足以提前止盈"}'
)

_LONG = (
    '{"close_now":false,"allow_add":true,"risk_level":"low","confidence":0.84,'
    '"reason":"上行结构仍在，回落未形成反转，不宜提前止盈",'
    '"analysis":"市场结构表现为低点抬高后的上行趋势，价格虽从近期高点回撤，但仍处于突破后的高位整理，尚未出现明确的反转形态或衰竭信号。当前一笔持仓浮盈约0.79倍波动幅度，另一笔略有浮亏，整体利润很小，尚未达到应立即兑现的程度。"}'
)


def test_a_plain_review_object_parses() -> None:
    parsed = _extract_json_object(_SHORT, endpoint="position")
    assert parsed["close_now"] is False
    assert parsed["allow_add"] is True
    assert parsed["risk_level"] == "low"


def test_a_review_object_with_analysis_parses() -> None:
    parsed = _extract_json_object(_LONG, endpoint="position")
    assert parsed["risk_level"] == "low"
    assert "上行趋势" in parsed["analysis"]


def test_a_review_object_wrapped_in_a_code_fence_parses() -> None:
    parsed = _extract_json_object(f"```json\n{_SHORT}\n```", endpoint="position")
    assert parsed["allow_add"] is True


def test_a_review_object_with_a_leading_newline_parses() -> None:
    parsed = _extract_json_object(f"\n\n  {_LONG}  \n", endpoint="position")
    assert parsed["close_now"] is False

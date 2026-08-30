from __future__ import annotations

import pytest

from app.services.custom_workflow import (
    WorkflowError,
    compile_workflow,
    validate_workflow,
    workflow_catalog,
    workflow_validation_result,
)


def _entry(node_id: str, stage: str) -> dict:
    return {"id": node_id, "type": "entry", "stage": stage, "label": "入口"}


def _action(node_id: str, kind: str, **action: object) -> dict:
    return {"id": node_id, "type": "action", "label": kind, "action": {"kind": kind, **action}}


def _stage(stage: str, *, vision: bool = False) -> dict:
    entry_id = f"{stage}_entry"
    condition_id = f"{stage}_condition"
    yes_id = f"{stage}_yes"
    no_id = f"{stage}_no"
    if vision:
        condition = {
            "id": condition_id,
            "type": "vision_condition",
            "label": "识别SuperTrend变色",
            "instruction": "检查最近三根已收盘K线的SuperTrend是否由紫色变为蓝色",
            "expected_result": "bullish",
            "result_options": ["bullish", "bearish", "none"],
            "lookback": 3,
        }
    else:
        condition = {
            "id": condition_id,
            "type": "condition",
            "label": "EMA5上穿EMA30",
            "condition": {
                "kind": "cross",
                "description": "最近3根EMA5上穿EMA30",
                "left": {"kind": "indicator", "indicator": "ema", "alias": "ema5", "params": {"length": 5}},
                "right": {"kind": "indicator", "indicator": "ema", "alias": "ema30", "params": {"length": 30}},
                "direction": "above",
                "lookback": 3,
            },
        }
    if stage == "open":
        yes_action = _action(yes_id, "open_buy")
        no_action = _action(no_id, "no_action")
    else:
        yes_action = _action(yes_id, "close_all")
        no_action = _action(no_id, "hold")
    return {
        "entry_node_id": entry_id,
        "data_requirements": {"data_type": "both" if vision else "kline", "kline_count": 20},
        "nodes": [_entry(entry_id, stage), condition, yes_action, no_action],
        "edges": [
            {"id": f"{stage}_e1", "source": entry_id, "target": condition_id, "source_handle": "next"},
            {"id": f"{stage}_e2", "source": condition_id, "target": yes_id, "source_handle": "yes"},
            {"id": f"{stage}_e3", "source": condition_id, "target": no_id, "source_handle": "no"},
        ],
    }


def _workflow() -> dict:
    return {
        "schema_version": 1,
        "workflow_version": 1,
        "source_mode": "visual",
        "source_text": {"open": "", "position": ""},
        "open": _stage("open"),
        "position": _stage("position", vision=True),
    }


def test_validate_and_compile_visual_workflow() -> None:
    workflow = validate_workflow(_workflow())
    assert workflow.open.entry_node_id == "open_entry"

    compiled = compile_workflow(workflow)
    assert compiled["schema_version"] == 1
    assert compiled["open"]["data_requirements"]["kline_count"] >= 33
    assert {item["alias"] for item in compiled["open"]["indicators"]} == {"ema5", "ema30"}
    assert compiled["open"]["uses_ai"] is False
    assert compiled["position"]["uses_ai"] is True
    assert compiled["position"]["data_requirements"]["data_type"] == "both"


def test_condition_requires_both_yes_and_no_branches() -> None:
    value = _workflow()
    value["open"]["edges"].pop()
    with pytest.raises(WorkflowError, match="condition_branches_incomplete"):
        validate_workflow(value)


def test_action_node_cannot_continue() -> None:
    value = _workflow()
    value["open"]["nodes"].append(_action("extra_action", "no_action"))
    value["open"]["edges"].append({
        "id": "cycle_edge", "source": "open_yes", "target": "extra_action", "source_handle": "next",
    })
    with pytest.raises(WorkflowError, match="action_node_cannot_have_outgoing_edge"):
        validate_workflow(value)


def test_cycle_is_rejected() -> None:
    value = _workflow()
    second = {
        "id": "open_condition_2",
        "type": "condition",
        "label": "第二个条件",
        "condition": {
            "kind": "comparison",
            "left": {"kind": "market_price", "name": "bid"},
            "operator": "gt",
            "right": {"kind": "constant", "value": 0},
        },
    }
    value["open"]["nodes"].append(second)
    value["open"]["edges"][2]["target"] = "open_condition_2"
    value["open"]["edges"].extend([
        {"id": "open_cycle", "source": "open_condition_2", "target": "open_condition", "source_handle": "yes"},
        {"id": "open_condition_2_no", "source": "open_condition_2", "target": "open_no", "source_handle": "no"},
    ])
    with pytest.raises(WorkflowError, match="workflow_cycle_not_allowed"):
        validate_workflow(value)


def test_open_stage_rejects_position_action() -> None:
    value = _workflow()
    value["open"]["nodes"][2]["action"]["kind"] = "close_all"
    with pytest.raises(WorkflowError, match="workflow_action_not_allowed_in_stage"):
        validate_workflow(value)


def test_open_stage_rejects_position_operand() -> None:
    value = _workflow()
    value["open"]["nodes"][1]["condition"] = {
        "kind": "comparison",
        "left": {"kind": "position", "name": "profit"},
        "operator": "gt",
        "right": {"kind": "constant", "value": 0},
    }
    with pytest.raises(WorkflowError, match="position_condition_not_allowed_in_open_stage"):
        validate_workflow(value)


def test_ai_condition_is_reported_as_unstructured() -> None:
    value = _workflow()
    value["position"]["nodes"][1] = {
        "id": "position_condition",
        "type": "ai_condition",
        "label": "主观趋势判断",
        "instruction": "判断当前趋势是否已经明显衰竭",
        "data_type": "both",
    }
    compiled = compile_workflow(value)
    assert compiled["position"]["unsupported_conditions"] == [{
        "node_id": "position_condition",
        "text": "判断当前趋势是否已经明显衰竭",
        "code": "ai_condition",
    }]


def test_catalog_exposes_v1_node_dictionary() -> None:
    catalog = workflow_catalog()
    assert catalog["schema_version"] == 1
    assert any(item["kind"] == "cross" for item in catalog["condition_nodes"])
    assert any(item["type"] == "vision_condition" for item in catalog["ai_nodes"])
    assert any(item["kind"] == "close_partial" for item in catalog["action_nodes"])


def test_validation_result_is_editor_friendly() -> None:
    value = _workflow()
    value["open"]["edges"].pop()
    result = workflow_validation_result(value)
    assert result["valid"] is False
    assert result["compiled"] is None
    assert result["errors"][0]["code"] == "condition_branches_incomplete"


def test_consecutive_condition_requires_complete_comparison() -> None:
    value = _workflow()
    value["open"]["nodes"][1]["condition"] = {
        "kind": "consecutive",
        "left": {"kind": "candle", "name": "close"},
        "count": 3,
    }
    result = workflow_validation_result(value)
    assert result["valid"] is False
    assert any("consecutive_condition_requires_comparison" in item["detail"] for item in result["errors"])


def test_multi_output_indicator_component_is_preserved() -> None:
    value = _workflow()
    value["open"]["nodes"][1]["condition"] = {
        "kind": "comparison",
        "left": {
            "kind": "indicator", "indicator": "macd", "component": "histogram",
            "params": {"fast": 12, "slow": 26, "signal": 9},
        },
        "operator": "gt",
        "right": {"kind": "constant", "value": 0},
    }
    compiled = compile_workflow(value)
    indicator = compiled["open"]["indicators"][0]
    assert indicator["component"] == "histogram"
    assert indicator["alias"].endswith("_histogram")

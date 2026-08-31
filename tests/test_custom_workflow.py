from __future__ import annotations

from copy import deepcopy

import pytest

from app.services.ai_service import (
    AiDecisionClient,
    _classify_workflow_source_rule,
    _normalize_generated_workflow_stage,
    _workflow_source_rules,
    _workflow_stage_from_classified_rules,
)
from app.services.custom_workflow import (
    WorkflowError,
    compile_workflow,
    validate_workflow,
    validate_workflow_stage,
    workflow_catalog,
    workflow_validation_result,
)
from app.store import SqliteStore


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
        extract_id = f"{stage}_vision"
        extract = {
            "id": extract_id,
            "type": "vision_extract",
            "label": "提取SuperTrend信号",
            "instruction": "检查最近三根已收盘K线的SuperTrend颜色变化并输出固定信号",
            "output": {
                "key": "supertrend_signal",
                "label": "SuperTrend信号",
                "options": [
                    {"value": "long", "label": "多头信号"},
                    {"value": "short", "label": "空头信号"},
                    {"value": "none", "label": "无信号"},
                    {"value": "uncertain", "label": "无法确认"},
                ],
                "fallback": "uncertain",
            },
            "lookback": 3,
        }
        condition = {
            "id": condition_id,
            "type": "condition",
            "label": "SuperTrend信号等于多头",
            "condition": {
                "kind": "vision_result",
                "left": {
                    "kind": "vision_result", "source_node_id": extract_id,
                    "output_key": "supertrend_signal",
                },
                "operator": "eq",
                "right": {"kind": "constant", "value": "long"},
            },
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
    nodes = [_entry(entry_id, stage), condition, yes_action, no_action]
    edges = [
        {"id": f"{stage}_e1", "source": entry_id, "target": condition_id, "source_handle": "next"},
        {"id": f"{stage}_e2", "source": condition_id, "target": yes_id, "source_handle": "yes"},
        {"id": f"{stage}_e3", "source": condition_id, "target": no_id, "source_handle": "no"},
    ]
    if vision:
        nodes.insert(1, extract)
        edges[0]["target"] = extract_id
        edges.insert(1, {"id": f"{stage}_vision_next", "source": extract_id, "target": condition_id, "source_handle": "next"})
    return {
        "entry_node_id": entry_id,
        "data_requirements": {"data_type": "both" if vision else "kline", "kline_count": 20},
        "nodes": nodes,
        "edges": edges,
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


def test_validate_single_generated_stage() -> None:
    stage = validate_workflow_stage(_stage("open"), "open")
    assert stage.entry_node_id == "open_entry"
    with pytest.raises(WorkflowError, match="invalid_workflow_entry"):
        validate_workflow_stage(_stage("open"), "position")


def test_generated_cross_operator_and_natural_language_stop_rule_are_supported() -> None:
    value = _stage("position")
    condition = next(node for node in value["nodes"] if node["type"] == "condition")["condition"]
    condition.pop("direction", None)
    condition["operator"] = "lt"
    action = next(node for node in value["nodes"] if node["id"] == "position_yes")
    action["action"] = {"kind": "modify_sl", "stop_loss_rule": "current_price + 0.5 * atr14"}

    normalized = _normalize_generated_workflow_stage(
        value,
        stage="position",
        data_requirements=value["data_requirements"],
    )
    validated = validate_workflow_stage(normalized, "position")
    validated_condition = next(node for node in validated.nodes if node.type == "condition")
    validated_action = next(node for node in validated.nodes if node.id == "position_yes")
    assert validated_condition.condition.direction == "below"
    assert validated_action.action.stop_loss_rule == "current_price + 0.5 * atr14"


def test_complex_position_rules_can_fall_back_to_exact_source_decision_chain() -> None:
    source = (
        "多单出现EMA5下穿EMA10时，平仓。\n"
        "空单出现EMA5上穿EMA10时，平仓。\n"
        "当多单盈利达到0.5 ATR时，将止损移动到开仓价+0.2。"
    )
    rules = _workflow_source_rules(source)
    stage = _workflow_stage_from_classified_rules(
        {
            "rules": [
                {"rule_index": 1, "label": "多单反向交叉平仓", "action_kind": "close_all"},
                {"rule_index": 2, "label": "空单反向交叉平仓", "action_kind": "close_all"},
                {"rule_index": 3, "label": "多单移动止损", "action_kind": "modify_sl"},
            ],
        },
        stage="position",
        data_requirements={"data_type": "kline", "kline_count": 100, "call_mode": "bar", "call_value": 1},
        source_rules=rules,
    )
    validated = validate_workflow_stage(stage, "position")
    decisions = [node for node in validated.nodes if node.type == "ai_condition"]
    actions = [node for node in validated.nodes if node.type == "action"]
    assert [node.instruction for node in decisions] == rules
    assert [node.action.kind for node in actions] == ["close_all", "close_all", "modify_sl", "hold"]
    assert actions[2].action.stop_loss_rule == rules[2]


def test_position_action_classification_uses_explicit_action_not_position_direction() -> None:
    assert _classify_workflow_source_rule("多单出现EMA5下穿EMA10时，平仓", stage="position") == "close_all"
    assert _classify_workflow_source_rule("空单盈利后将止损移动到开仓价-0.2", stage="position") == "modify_sl"
    assert _classify_workflow_source_rule("多单盈利后加仓", stage="position") == "add_buy"
    assert _classify_workflow_source_rule("空单盈利后加仓", stage="position") == "add_sell"


def test_confirmed_workflow_compiles_without_runtime_model_call(tmp_path) -> None:
    store = SqliteStore(tmp_path / "workflow-compile.db")
    store.initialize()
    client = AiDecisionClient(store)
    result = client.compile_custom_workflow(
        _workflow(),
        open_logic="EMA5上穿EMA30开多",
        position_logic="EMA5下穿EMA30时平仓",
    )
    assert result["compile_status"] == "generated"
    assert result["compiled_workflow"]["open"]["entry_node_id"] == "open_entry"
    assert result["workflow"]["schema_version"] == 1
    assert "entry_node_id" in result["open_prompt_template"]


def test_generated_flat_cross_rules_are_normalized_into_a_complete_chain() -> None:
    value = _stage("open")
    first = next(node for node in value["nodes"] if node["id"] == "open_condition")
    first["condition"]["direction"] = "bullish"
    first["condition"]["lookback"] = 1
    first["condition"]["left"]["indicator"] = ""
    first["condition"]["left"]["name"] = "ema"
    first["condition"]["left"]["alias"] = ""
    second = deepcopy(first)
    second["id"] = "open_condition_short"
    second["label"] = "EMA5下穿EMA30"
    second["condition"]["direction"] = "bearish"
    short_action = _action("open_short", "open_sell", stop_loss_rule="recent_low(5)", description="止损设在最近5根最高价")
    value["nodes"].extend([second, short_action])
    value["edges"].extend([
        {"id": "short_yes", "source": second["id"], "target": short_action["id"], "source_handle": "yes"},
        {"id": "short_no", "source": second["id"], "target": "open_no", "source_handle": "no"},
    ])
    value["nodes"].append({
        "id": "latest_cross_meta", "type": "condition", "label": "时间最近的一次交叉",
        "condition": {
            "kind": "consecutive", "description": "同时出现时以最近交叉为准",
            "left": {"kind": "condition", "name": "open_condition"}, "operator": "eq",
            "right": {"kind": "condition", "name": "open_condition_short"}, "count": 1,
        },
    })
    value["edges"].extend([
        {"id": "meta_yes", "source": "latest_cross_meta", "target": "open_yes", "source_handle": "yes"},
        {"id": "meta_no", "source": "latest_cross_meta", "target": "open_short", "source_handle": "no"},
    ])
    normalized = _normalize_generated_workflow_stage(
        value,
        stage="open",
        data_requirements=value["data_requirements"],
        user_logic=(
            "最近3根K线内EMA5上穿EMA30开多，止损设在最近5根最低价；"
            "最近3根K线内EMA5下穿EMA30开空，止损设在最近5根最高价；"
            "如果同时出现上穿和下穿，以时间最近的一次交叉为准。"
        ),
    )
    validated = validate_workflow_stage(normalized, "open")
    conditions = [node for node in validated.nodes if node.type == "condition"]
    assert all(node.condition.lookback == 3 for node in conditions)
    assert all(node.condition.cross_mode == "latest" for node in conditions)
    assert conditions[0].condition.left.alias == "ema5"
    assert all(node.id != "latest_cross_meta" for node in validated.nodes)
    assert any(edge.source == "open_condition" and edge.target == "open_condition_short" for edge in validated.edges)
    short = next(node for node in validated.nodes if node.id == "open_short")
    assert short.action.stop_loss_rule == "recent_high(5)"


def test_open_action_keeps_market_and_risk_rules() -> None:
    value = _workflow()
    open_action = next(node for node in value["open"]["nodes"] if node["id"] == "open_yes")
    open_action["action"].update({
        "entry_mode": "pending",
        "entry_price_rule": "在最近一次回调低点上方 0.2 ATR 挂多单",
        "stop_loss_rule": "最近 5 根已收盘 K 线最低价",
        "take_profit_rule": "止损距离的 2 倍",
    })

    compiled = compile_workflow(validate_workflow(value))
    action = compiled["open"]["nodes"]["open_yes"]["action"]
    assert action["entry_mode"] == "pending"
    assert action["entry_price_rule"] == "在最近一次回调低点上方 0.2 ATR 挂多单"
    assert action["stop_loss_rule"] == "最近 5 根已收盘 K 线最低价"
    assert action["take_profit_rule"] == "止损距离的 2 倍"


def test_pending_open_action_requires_price_rule() -> None:
    value = _workflow()
    open_action = next(node for node in value["open"]["nodes"] if node["id"] == "open_yes")
    open_action["action"]["entry_mode"] = "pending"

    with pytest.raises(Exception, match="pending_order_requires_entry_price_rule"):
        validate_workflow(value)


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
    value["position"] = _stage("position")
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
    assert len(catalog["indicators"]) == 49
    assert all(item["outputs"] for item in catalog["indicators"])
    assert any(item["kind"] == "cross" for item in catalog["condition_nodes"])
    assert any(item["type"] == "vision_extract" for item in catalog["ai_nodes"])
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


def test_vision_extract_has_one_next_branch_and_fixed_enum_output() -> None:
    value = _workflow()
    compiled = compile_workflow(value)
    vision = compiled["position"]["nodes"]["position_vision"]
    assert compiled["position"]["transitions"]["position_vision"] == {"next": "position_condition"}
    assert [item["value"] for item in vision["output"]["options"]] == ["long", "short", "none", "uncertain"]


def test_vision_result_must_use_an_allowed_upstream_enum_value() -> None:
    value = _workflow()
    value["position"]["nodes"][2]["condition"]["right"]["value"] = "outside_enum"
    with pytest.raises(WorkflowError, match="vision_result_value_not_allowed"):
        validate_workflow(value)


def test_vision_output_requires_unique_options_and_existing_fallback() -> None:
    value = _workflow()
    output = value["position"]["nodes"][1]["output"]
    output["options"][1]["value"] = "long"
    result = workflow_validation_result(value)
    assert result["valid"] is False
    assert any("vision_output_options_must_be_unique" in item["detail"] for item in result["errors"])

    value = _workflow()
    value["position"]["nodes"][1]["output"]["fallback"] = "missing"
    result = workflow_validation_result(value)
    assert result["valid"] is False
    assert any("vision_output_fallback_not_found" in item["detail"] for item in result["errors"])


def test_vision_result_rejects_a_source_that_can_be_bypassed() -> None:
    value = _workflow()
    stage = value["position"]
    stage["nodes"].append({
        "id": "position_gate", "type": "condition", "label": "前置条件",
        "condition": {
            "kind": "comparison", "left": {"kind": "market_price", "name": "bid"},
            "operator": "gt", "right": {"kind": "constant", "value": 0},
        },
    })
    stage["edges"][0]["target"] = "position_gate"
    stage["edges"].extend([
        {"id": "position_gate_yes", "source": "position_gate", "target": "position_vision", "source_handle": "yes"},
        {"id": "position_gate_no", "source": "position_gate", "target": "position_condition", "source_handle": "no"},
    ])
    with pytest.raises(WorkflowError, match="vision_result_source_can_be_bypassed"):
        validate_workflow(value)


def test_price_indicator_can_compare_with_price_or_compatible_price_line() -> None:
    value = _workflow()
    condition = value["open"]["nodes"][1]["condition"]
    condition["left"] = {
        "kind": "indicator", "indicator": "bbands", "component": "upper",
        "params": {"length": 20, "std": 2},
    }
    condition["right"] = {"kind": "market_price", "name": "bid"}
    validate_workflow(value)

    condition["right"] = {
        "kind": "indicator", "indicator": "ema", "component": "value", "params": {"length": 20},
    }
    validate_workflow(value)


def test_numeric_indicator_rejects_market_price_comparison() -> None:
    value = _workflow()
    value["open"]["nodes"][1]["condition"] = {
        "kind": "cross",
        "left": {
            "kind": "indicator", "indicator": "macd", "component": "histogram",
            "params": {"fast": 12, "slow": 26, "signal": 9},
        },
        "right": {"kind": "market_price", "name": "bid"},
        "direction": "above",
    }
    with pytest.raises(WorkflowError, match="indicator_right_operand_not_supported"):
        validate_workflow(value)

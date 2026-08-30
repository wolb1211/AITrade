from __future__ import annotations

from collections import deque
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.custom_indicators import public_indicator_catalog


WORKFLOW_SCHEMA_VERSION = 1
MAX_WORKFLOW_NODES = 120
MAX_CONDITION_GROUP_DEPTH = 4


class WorkflowError(ValueError):
    """Raised when a visual strategy workflow cannot be published."""

    def __init__(self, code: str, *, node_id: str = "", detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.node_id = node_id
        self.detail = detail


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowSourceText(WorkflowModel):
    open: str = Field(default="", max_length=12000)
    position: str = Field(default="", max_length=12000)


class WorkflowDataRequirements(WorkflowModel):
    data_type: Literal["kline", "screenshot", "both"] = "kline"
    kline_count: int = Field(default=100, ge=1, le=1000)
    call_mode: Literal["bar", "timer", "tick", "price_step"] = "bar"
    call_value: float = Field(default=1, ge=0)


class WorkflowUiPosition(WorkflowModel):
    x: float = 0
    y: float = 0


class WorkflowOperand(WorkflowModel):
    kind: Literal[
        "indicator", "market_price", "candle", "position", "constant", "derived",
    ]
    name: str = Field(default="", max_length=80)
    value: float | str | bool | None = None
    indicator: str = Field(default="", max_length=40)
    component: str = Field(default="", max_length=40)
    alias: str = Field(default="", max_length=64)
    source: str = Field(default="close", max_length=32)
    params: dict[str, int | float | str] = Field(default_factory=dict)
    multiplier: float = Field(default=1, ge=-1000000, le=1000000)
    addend: float = Field(default=0, ge=-1000000000, le=1000000000)
    offset: int = Field(default=-1, ge=-1000, le=0)
    lookback: int = Field(default=1, ge=1, le=1000)

    @model_validator(mode="after")
    def validate_operand(self) -> "WorkflowOperand":
        if self.kind == "indicator" and not self.indicator:
            raise ValueError("indicator_operand_requires_indicator")
        if self.kind == "constant" and self.value is None:
            raise ValueError("constant_operand_requires_value")
        if self.kind in {"market_price", "candle", "position", "derived"} and not self.name:
            raise ValueError(f"{self.kind}_operand_requires_name")
        return self


ConditionKind = Literal[
    "comparison",
    "cross",
    "consecutive",
    "indicator_trend",
    "candle_pattern",
    "market_structure",
    "breakout",
    "atr_distance",
    "position_state",
    "group",
]


class WorkflowCondition(WorkflowModel):
    kind: ConditionKind
    description: str = Field(default="", max_length=500)
    left: WorkflowOperand | None = None
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq"] | None = None
    right: WorkflowOperand | None = None
    direction: Literal["above", "below", "up", "down", "bullish", "bearish"] | None = None
    lookback: int = Field(default=1, ge=1, le=1000)
    count: int = Field(default=1, ge=1, le=1000)
    pattern: str = Field(default="", max_length=80)
    group_operator: Literal["all", "any"] | None = None
    conditions: list["WorkflowCondition"] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def validate_condition(self) -> "WorkflowCondition":
        if self.kind == "comparison" and (self.left is None or self.operator is None or self.right is None):
            raise ValueError("comparison_condition_incomplete")
        if self.kind == "cross":
            if self.left is None or self.right is None or self.direction not in {"above", "below"}:
                raise ValueError("cross_condition_incomplete")
        if self.kind in {"consecutive", "indicator_trend"} and self.left is None:
            raise ValueError(f"{self.kind}_condition_requires_left")
        if self.kind == "consecutive" and (self.operator is None or self.right is None):
            raise ValueError("consecutive_condition_requires_comparison")
        if self.kind in {"candle_pattern", "market_structure"} and not self.pattern:
            raise ValueError(f"{self.kind}_condition_requires_pattern")
        if self.kind in {"breakout", "atr_distance", "position_state"} and self.left is None:
            raise ValueError(f"{self.kind}_condition_requires_left")
        if self.kind == "group":
            if self.group_operator is None or len(self.conditions) < 2:
                raise ValueError("condition_group_requires_two_conditions")
        elif self.conditions:
            raise ValueError("nested_conditions_only_allowed_for_group")
        return self


class WorkflowPriceTarget(WorkflowModel):
    kind: Literal[
        "fixed", "entry_price", "current_price", "indicator", "recent_high", "recent_low", "atr_offset",
    ]
    value: float | None = None
    indicator: str = Field(default="", max_length=64)
    lookback: int = Field(default=1, ge=1, le=1000)
    operation: Literal["none", "add", "subtract"] = "none"
    offset_value: float = 0
    atr_multiplier: float = Field(default=0, ge=0, le=1000)


class WorkflowVolume(WorkflowModel):
    mode: Literal["open_sizing", "fixed", "current_ratio", "previous_multiple"] = "open_sizing"
    value: float = Field(default=1, gt=0, le=100000)


ActionKind = Literal[
    "open_buy", "open_sell", "no_action", "close_all", "close_partial",
    "add_buy", "add_sell", "modify_sl", "modify_tp", "hold",
]


class WorkflowAction(WorkflowModel):
    kind: ActionKind
    volume: WorkflowVolume | None = None
    target: WorkflowPriceTarget | None = None
    stop_loss: WorkflowPriceTarget | None = None
    take_profit: WorkflowPriceTarget | None = None
    description: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_action(self) -> "WorkflowAction":
        if self.kind == "close_partial" and self.volume is None:
            raise ValueError("partial_close_requires_volume")
        if self.kind in {"add_buy", "add_sell"} and self.volume is None:
            raise ValueError("add_action_requires_volume")
        if self.kind in {"modify_sl", "modify_tp"} and self.target is None:
            raise ValueError("modify_action_requires_target")
        return self


class WorkflowEntryNode(WorkflowModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    type: Literal["entry"] = "entry"
    label: str = Field(default="", max_length=100)
    stage: Literal["open", "position"]
    position: WorkflowUiPosition | None = None


class WorkflowConditionNode(WorkflowModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    type: Literal["condition"] = "condition"
    label: str = Field(default="", max_length=100)
    condition: WorkflowCondition
    position: WorkflowUiPosition | None = None


class WorkflowVisionConditionNode(WorkflowModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    type: Literal["vision_condition"] = "vision_condition"
    label: str = Field(default="", max_length=100)
    instruction: str = Field(min_length=5, max_length=4000)
    expected_result: str = Field(min_length=1, max_length=100)
    result_options: list[str] = Field(default_factory=lambda: ["matched", "not_matched"], min_length=2, max_length=12)
    lookback: int = Field(default=3, ge=1, le=100)
    minimum_confidence: float = Field(default=0, ge=0, le=1)
    position: WorkflowUiPosition | None = None


class WorkflowAiConditionNode(WorkflowModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    type: Literal["ai_condition"] = "ai_condition"
    label: str = Field(default="", max_length=100)
    instruction: str = Field(min_length=5, max_length=4000)
    data_type: Literal["kline", "screenshot", "both"] = "kline"
    position: WorkflowUiPosition | None = None


class WorkflowActionNode(WorkflowModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    type: Literal["action"] = "action"
    label: str = Field(default="", max_length=100)
    action: WorkflowAction
    position: WorkflowUiPosition | None = None


WorkflowNode = (
    WorkflowEntryNode | WorkflowConditionNode | WorkflowVisionConditionNode |
    WorkflowAiConditionNode | WorkflowActionNode
)


class WorkflowEdge(WorkflowModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    source_handle: Literal["next", "yes", "no"]


class WorkflowStage(WorkflowModel):
    entry_node_id: str = Field(min_length=1, max_length=64)
    data_requirements: WorkflowDataRequirements = Field(default_factory=WorkflowDataRequirements)
    nodes: list[WorkflowNode] = Field(min_length=2, max_length=MAX_WORKFLOW_NODES)
    edges: list[WorkflowEdge] = Field(min_length=1, max_length=MAX_WORKFLOW_NODES * 2)


class CustomStrategyWorkflow(WorkflowModel):
    schema_version: Literal[1] = WORKFLOW_SCHEMA_VERSION
    workflow_version: int = Field(default=1, ge=1)
    source_mode: Literal["visual", "ai_generated", "legacy_import"] = "visual"
    source_text: WorkflowSourceText = Field(default_factory=WorkflowSourceText)
    open: WorkflowStage
    position: WorkflowStage


def workflow_json_schema() -> dict[str, Any]:
    return CustomStrategyWorkflow.model_json_schema()


def validate_workflow(value: Any) -> CustomStrategyWorkflow:
    workflow = CustomStrategyWorkflow.model_validate(value)
    _validate_stage(workflow.open, "open")
    _validate_stage(workflow.position, "position")
    return workflow


def compile_workflow(value: Any) -> dict[str, Any]:
    workflow = validate_workflow(value)
    return {
        "schema_version": workflow.schema_version,
        "workflow_version": workflow.workflow_version,
        "engine_version": 1,
        "open": _compile_stage(workflow.open, "open"),
        "position": _compile_stage(workflow.position, "position"),
    }


def workflow_validation_result(value: Any) -> dict[str, Any]:
    try:
        return {"valid": True, "errors": [], "compiled": compile_workflow(value)}
    except WorkflowError as exc:
        return {
            "valid": False,
            "errors": [{"code": exc.code, "node_id": exc.node_id, "detail": exc.detail}],
            "compiled": None,
        }
    except ValidationError as exc:
        return {
            "valid": False,
            "errors": [
                {
                    "code": str(item.get("type") or "invalid_workflow_value"),
                    "path": ".".join(str(part) for part in item.get("loc") or ()),
                    "detail": str(item.get("msg") or ""),
                }
                for item in exc.errors(include_url=False, include_context=False, include_input=False)
            ],
            "compiled": None,
        }


def workflow_catalog() -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "limits": {"max_nodes_per_stage": MAX_WORKFLOW_NODES, "max_condition_group_depth": MAX_CONDITION_GROUP_DEPTH},
        "indicators": public_indicator_catalog(),
        "entry_nodes": [
            {"type": "open", "title": "开仓分析", "description": "没有持仓时判断是否开多、开空或不操作"},
            {"type": "position", "title": "持仓风控", "description": "持仓后判断平仓、加仓、修改止损止盈或继续持有"},
        ],
        "condition_nodes": [
            {"kind": "comparison", "title": "数值比较", "description": "比较指标、价格、K线、持仓数据或固定数值"},
            {"kind": "cross", "title": "上穿 / 下破", "description": "判断两个指标或数据序列在最近范围内发生交叉"},
            {"kind": "consecutive", "title": "连续满足", "description": "判断一个条件是否连续满足指定K线数量"},
            {"kind": "indicator_trend", "title": "指标方向", "description": "判断指标连续上升或下降"},
            {"kind": "candle_pattern", "title": "K线形态", "description": "吞没、Pin Bar、十字星等标准K线形态"},
            {"kind": "market_structure", "title": "市场结构", "description": "HH、HL、LH、LL等高低点结构"},
            {"kind": "breakout", "title": "突破 / 回踩", "description": "判断价格或指标是否突破、回踩指定参考值"},
            {"kind": "atr_distance", "title": "ATR距离", "description": "判断价格、开仓价或止损价之间的ATR倍数距离"},
            {"kind": "position_state", "title": "持仓状态", "description": "判断方向、盈亏、持仓数量、开仓价和止损止盈"},
            {"kind": "group", "title": "条件组合", "description": "使用全部满足（AND）或任一满足（OR）组合条件"},
        ],
        "ai_nodes": [
            {"type": "vision_condition", "title": "截图识别规则", "description": "识别自定义指标、图形、文字或其他视觉信号"},
            {"type": "ai_condition", "title": "AI判断规则", "description": "处理第一版暂时无法精确结构化的开放条件"},
        ],
        "action_nodes": [
            {"kind": "open_buy", "title": "开多", "stages": ["open"]},
            {"kind": "open_sell", "title": "开空", "stages": ["open"]},
            {"kind": "no_action", "title": "不操作", "stages": ["open"]},
            {"kind": "close_all", "title": "全部平仓", "stages": ["position"]},
            {"kind": "close_partial", "title": "部分平仓", "stages": ["position"]},
            {"kind": "add_buy", "title": "加多仓", "stages": ["position"]},
            {"kind": "add_sell", "title": "加空仓", "stages": ["position"]},
            {"kind": "modify_sl", "title": "修改止损", "stages": ["position"]},
            {"kind": "modify_tp", "title": "修改止盈", "stages": ["position"]},
            {"kind": "hold", "title": "保持持仓", "stages": ["position"]},
        ],
        "operators": [
            {"value": "gt", "title": "大于"}, {"value": "gte", "title": "大于等于"},
            {"value": "lt", "title": "小于"}, {"value": "lte", "title": "小于等于"},
            {"value": "eq", "title": "等于"}, {"value": "neq", "title": "不等于"},
        ],
    }


def _validate_stage(stage: WorkflowStage, expected_stage: Literal["open", "position"]) -> None:
    nodes = {node.id: node for node in stage.nodes}
    if len(nodes) != len(stage.nodes):
        raise WorkflowError("duplicate_workflow_node_id")
    edges = {edge.id: edge for edge in stage.edges}
    if len(edges) != len(stage.edges):
        raise WorkflowError("duplicate_workflow_edge_id")
    entry = nodes.get(stage.entry_node_id)
    if not isinstance(entry, WorkflowEntryNode) or entry.stage != expected_stage:
        raise WorkflowError("invalid_workflow_entry", node_id=stage.entry_node_id)
    if sum(isinstance(node, WorkflowEntryNode) for node in stage.nodes) != 1:
        raise WorkflowError("workflow_requires_single_entry", node_id=stage.entry_node_id)

    outgoing: dict[str, dict[str, str]] = {node_id: {} for node_id in nodes}
    incoming: dict[str, int] = {node_id: 0 for node_id in nodes}
    for edge in stage.edges:
        source = nodes.get(edge.source)
        if source is None or edge.target not in nodes:
            raise WorkflowError("workflow_edge_node_not_found", detail=edge.id)
        if edge.source_handle in outgoing[edge.source]:
            raise WorkflowError("duplicate_workflow_branch", node_id=edge.source, detail=edge.source_handle)
        if isinstance(source, WorkflowEntryNode) and edge.source_handle != "next":
            raise WorkflowError("entry_requires_next_branch", node_id=source.id)
        if isinstance(source, (WorkflowConditionNode, WorkflowVisionConditionNode, WorkflowAiConditionNode)) and edge.source_handle not in {"yes", "no"}:
            raise WorkflowError("condition_requires_yes_no_branch", node_id=source.id)
        if isinstance(source, WorkflowActionNode):
            raise WorkflowError("action_node_cannot_have_outgoing_edge", node_id=source.id)
        outgoing[edge.source][edge.source_handle] = edge.target
        incoming[edge.target] += 1

    for node in stage.nodes:
        handles = set(outgoing[node.id])
        if isinstance(node, WorkflowEntryNode) and handles != {"next"}:
            raise WorkflowError("entry_branch_incomplete", node_id=node.id)
        if isinstance(node, (WorkflowConditionNode, WorkflowVisionConditionNode, WorkflowAiConditionNode)) and handles != {"yes", "no"}:
            raise WorkflowError("condition_branches_incomplete", node_id=node.id)
        if isinstance(node, WorkflowActionNode):
            _validate_stage_action(node, expected_stage)
        if isinstance(node, WorkflowConditionNode):
            _validate_condition_depth(node.condition)
            if expected_stage == "open" and _condition_uses_position(node.condition):
                raise WorkflowError("position_condition_not_allowed_in_open_stage", node_id=node.id)
    if incoming[stage.entry_node_id] != 0:
        raise WorkflowError("workflow_entry_cannot_have_incoming_edge", node_id=stage.entry_node_id)

    reachable: set[str] = set()
    queue: deque[str] = deque([stage.entry_node_id])
    while queue:
        node_id = queue.popleft()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        queue.extend(outgoing[node_id].values())
    if len(reachable) != len(nodes):
        missing = sorted(set(nodes) - reachable)
        raise WorkflowError("workflow_contains_unreachable_nodes", detail=",".join(missing))

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise WorkflowError("workflow_cycle_not_allowed", node_id=node_id)
        if node_id in visited:
            return
        visiting.add(node_id)
        for target in outgoing[node_id].values():
            visit(target)
        visiting.remove(node_id)
        visited.add(node_id)

    visit(stage.entry_node_id)


def _validate_stage_action(node: WorkflowActionNode, stage: Literal["open", "position"]) -> None:
    open_actions = {"open_buy", "open_sell", "no_action"}
    position_actions = {"close_all", "close_partial", "add_buy", "add_sell", "modify_sl", "modify_tp", "hold"}
    allowed = open_actions if stage == "open" else position_actions
    if node.action.kind not in allowed:
        raise WorkflowError("workflow_action_not_allowed_in_stage", node_id=node.id, detail=node.action.kind)


def _validate_condition_depth(condition: WorkflowCondition, depth: int = 1) -> None:
    if depth > MAX_CONDITION_GROUP_DEPTH:
        raise WorkflowError("condition_group_too_deep")
    for child in condition.conditions:
        _validate_condition_depth(child, depth + 1)


def _condition_uses_position(condition: WorkflowCondition) -> bool:
    if condition.kind == "position_state":
        return True
    operands = [condition.left, condition.right]
    if any(item is not None and item.kind == "position" for item in operands):
        return True
    return any(_condition_uses_position(item) for item in condition.conditions)


def _compile_stage(stage: WorkflowStage, stage_name: Literal["open", "position"]) -> dict[str, Any]:
    node_map = {node.id: node for node in stage.nodes}
    transitions: dict[str, dict[str, str]] = {node_id: {} for node_id in node_map}
    for edge in stage.edges:
        transitions[edge.source][edge.source_handle] = edge.target
    indicators: dict[str, dict[str, Any]] = {}
    required_kline_count = stage.data_requirements.kline_count
    requires_screenshot = stage.data_requirements.data_type in {"screenshot", "both"}
    uses_ai = False
    unsupported_conditions: list[dict[str, str]] = []
    for node in stage.nodes:
        if isinstance(node, WorkflowConditionNode):
            required_kline_count = max(required_kline_count, _collect_condition_requirements(node.condition, indicators))
        elif isinstance(node, WorkflowVisionConditionNode):
            requires_screenshot = True
            uses_ai = True
            required_kline_count = max(required_kline_count, node.lookback)
        elif isinstance(node, WorkflowAiConditionNode):
            uses_ai = True
            requires_screenshot = requires_screenshot or node.data_type in {"screenshot", "both"}
            unsupported_conditions.append({"node_id": node.id, "text": node.instruction, "code": "ai_condition"})
    requires_kline = stage.data_requirements.data_type in {"kline", "both"} or any(
        isinstance(node, WorkflowConditionNode) for node in stage.nodes
    )
    data_type = "both" if requires_kline and requires_screenshot else "screenshot" if requires_screenshot else "kline"
    return {
        "stage": stage_name,
        "entry_node_id": stage.entry_node_id,
        "nodes": {node_id: node.model_dump(mode="json", exclude={"position"}) for node_id, node in node_map.items()},
        "transitions": transitions,
        "data_requirements": {
            **stage.data_requirements.model_dump(mode="json"),
            "data_type": data_type,
            "kline_count": min(required_kline_count, 1000) if requires_kline else 1,
        },
        "indicators": list(indicators.values()),
        "uses_ai": uses_ai,
        "unsupported_conditions": unsupported_conditions,
    }


def _collect_condition_requirements(condition: WorkflowCondition, indicators: dict[str, dict[str, Any]]) -> int:
    required = max(condition.lookback, condition.count)
    for operand in (condition.left, condition.right):
        if operand is None:
            continue
        required = max(required, operand.lookback, abs(operand.offset) + 1)
        if operand.kind == "indicator":
            alias = operand.alias or _indicator_alias(operand)
            indicators[alias] = {
                "name": operand.indicator,
                "alias": alias,
                "component": operand.component,
                "source": operand.source,
                "params": operand.params,
            }
            periods = [int(value) for value in operand.params.values() if isinstance(value, int) and value > 0]
            if periods:
                # Indicator warm-up plus the condition inspection window. A
                # crossover across three closed bars needs more than merely
                # the indicator period itself.
                required = max(
                    required,
                    max(periods) + max(condition.lookback, condition.count, operand.lookback) + abs(operand.offset),
                )
    for child in condition.conditions:
        required = max(required, _collect_condition_requirements(child, indicators))
    return required


def _indicator_alias(operand: WorkflowOperand) -> str:
    params = "_".join(str(value) for _, value in sorted(operand.params.items()))
    base = f"{operand.indicator}_{params}" if params else operand.indicator
    if operand.component:
        base = f"{base}_{operand.component}"
    return base.lower().replace(".", "_")[:64]


WorkflowCondition.model_rebuild()

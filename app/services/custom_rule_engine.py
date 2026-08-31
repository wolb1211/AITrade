from __future__ import annotations

import ast
from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from app.models import Candle, OpenEvaluateRequest, PositionEvaluateRequest, PositionSnapshot


RULE_ENGINE_VERSION = 1
MAX_RULES = 24
MAX_EXPRESSION_LENGTH = 500


class RulePlanError(ValueError):
    pass


@dataclass
class RuleEngineResult:
    action: str = "hold"
    direction: str | None = None
    ticket: str | None = None
    sl: float | None = None
    tp: float | None = None
    lot: float | None = None
    volume: float | None = None
    close_scope: str | None = None
    matched_rule: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.action != "hold"

    def explanation_payload(self, *, stage: str) -> dict[str, Any]:
        return {
            "stage": stage,
            "authoritative_action": self.action,
            "direction": self.direction,
            "sl": self.sl,
            "tp": self.tp,
            "lot": self.lot,
            "volume": self.volume,
            "close_scope": self.close_scope,
            "matched_rule": self.matched_rule,
            "condition_results": self.checks,
            "instruction": (
                "The values and action above were calculated by the deterministic rule engine from the user's confirmed "
                "strategy. Do not change, recalculate or contradict them. Return only a short natural Chinese explanation."
            ),
        }


def normalize_rule_plan(
    value: Any,
    *,
    stage: str,
    indicator_aliases: set[str] | None = None,
) -> dict[str, Any]:
    if stage not in {"open", "position"}:
        raise RulePlanError("invalid_rule_plan_stage")
    if not isinstance(value, dict):
        return {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []}
    mode = str(value.get("mode") or "ai").strip().lower()
    if mode != "deterministic":
        return {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []}
    raw_rules = value.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules or len(raw_rules) > MAX_RULES:
        raise RulePlanError("invalid_rule_plan_rules")
    rules: list[dict[str, Any]] = []
    stage_names = _OPEN_NAMES if stage == "open" else _POSITION_NAMES
    allowed_names = stage_names | {str(item).lower() for item in (indicator_aliases or set())}
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise RulePlanError("invalid_rule_plan_rule")
        when = str(raw_rule.get("when") or "").strip()
        _validate_expression(when, allowed_names=allowed_names)
        action = _normalize_action(raw_rule.get("action"), stage=stage, allowed_names=allowed_names)
        description = str(raw_rule.get("description") or f"规则 {index + 1}").strip()[:300]
        if stage == "position":
            if "多单" in description and not _has_side_guard(when, "BUY"):
                raise RulePlanError("buy_position_rule_requires_side_guard")
            if "空单" in description and not _has_side_guard(when, "SELL"):
                raise RulePlanError("sell_position_rule_requires_side_guard")
        rules.append({"when": when, "action": action, "description": description})
    return {"version": RULE_ENGINE_VERSION, "mode": "deterministic", "rules": rules}


def evaluate_open_rule_plan(
    plan: dict[str, Any],
    *,
    request: OpenEvaluateRequest,
    indicators: dict[str, Any],
) -> RuleEngineResult:
    context = _RuleContext(candles=request.candles, indicators=indicators, request=request)
    return _evaluate_plan(plan, context=context, stage="open")


def evaluate_position_rule_plan(
    plan: dict[str, Any],
    *,
    request: PositionEvaluateRequest,
    indicators: dict[str, Any],
    position: PositionSnapshot,
) -> RuleEngineResult:
    context = _RuleContext(candles=request.candles, indicators=indicators, request=request, position=position)
    return _evaluate_plan(plan, context=context, stage="position")


def _evaluate_plan(plan: dict[str, Any], *, context: "_RuleContext", stage: str) -> RuleEngineResult:
    checks: list[dict[str, Any]] = []
    for rule in list(plan.get("rules") or []):
        expression = str(rule.get("when") or "")
        try:
            passed = bool(context.evaluate(expression))
        except (RulePlanError, TypeError, ValueError, ZeroDivisionError):
            passed = False
        description = str(rule.get("description") or "策略条件")
        checks.append({"rule": description, "passed": passed})
        if not passed:
            continue
        action = dict(rule.get("action") or {})
        result = RuleEngineResult(
            action=str(action.get("type") or "hold"),
            direction=str(action.get("direction") or "").lower() or None,
            ticket=context.position.ticket if context.position is not None else None,
            close_scope=str(action.get("close_scope") or "").lower() or None,
            matched_rule=description,
            checks=checks,
        )
        for key in ("sl", "tp", "lot", "volume"):
            expression_value = action.get(key)
            if expression_value in (None, ""):
                continue
            calculated = context.evaluate(str(expression_value))
            number = float(calculated)
            if not isfinite(number) or number <= 0:
                raise RulePlanError(f"invalid_rule_action_{key}")
            setattr(result, key, number)
        constraint = str(action.get("sl_constraint") or "")
        if result.sl is not None and context.position is not None and context.position.sl is not None:
            if constraint == "not_below_current":
                result.sl = max(result.sl, context.position.sl)
            elif constraint == "not_above_current":
                result.sl = min(result.sl, context.position.sl)
        if (
            result.action == "modify"
            and context.position is not None
            and (result.sl is None or _same_price(result.sl, context.position.sl))
            and (result.tp is None or _same_price(result.tp, context.position.tp))
        ):
            continue
        return result
    return RuleEngineResult(action="hold", matched_rule="没有规则满足", checks=checks)


def _normalize_action(value: Any, *, stage: str, allowed_names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RulePlanError("invalid_rule_plan_action")
    action_type = str(value.get("type") or "").strip().lower()
    allowed = {"open"} if stage == "open" else {"close", "modify", "add"}
    if action_type not in allowed:
        raise RulePlanError("unsupported_rule_plan_action")
    action: dict[str, Any] = {"type": action_type}
    direction = str(value.get("direction") or "").strip().lower()
    if action_type in {"open", "add"}:
        if direction not in {"buy", "sell"}:
            raise RulePlanError("invalid_rule_plan_direction")
        action["direction"] = direction
    if action_type == "close":
        scope = str(value.get("close_scope") or "full").strip().lower()
        if scope not in {"full", "partial"}:
            raise RulePlanError("invalid_rule_plan_close_scope")
        action["close_scope"] = scope
    constraint = str(value.get("sl_constraint") or "").strip().lower()
    # JSON-producing models occasionally serialize an optional null as the
    # literal string "null"/"none". Treat those as an omitted constraint.
    if constraint in {"null", "none"}:
        constraint = ""
    if constraint not in {"", "not_below_current", "not_above_current"}:
        raise RulePlanError("invalid_stop_constraint")
    action["sl_constraint"] = constraint or None
    for key in ("sl", "tp", "lot", "volume"):
        expression = value.get(key)
        if expression in (None, "") or str(expression).strip().lower() in {"null", "none"}:
            action[key] = None
            continue
        text = str(expression).strip()
        _validate_expression(text, allowed_names=allowed_names)
        action[key] = text
    if action_type == "modify" and not action.get("sl") and not action.get("tp"):
        raise RulePlanError("modify_rule_requires_price")
    if action_type == "close" and action.get("close_scope") == "partial" and not action.get("volume"):
        raise RulePlanError("partial_close_rule_requires_volume")
    return action


def _validate_expression(expression: str, *, allowed_names: set[str] | None = None) -> None:
    if not expression or len(expression) > MAX_EXPRESSION_LENGTH:
        raise RulePlanError("invalid_rule_expression")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise RulePlanError("invalid_rule_expression") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise RulePlanError(f"unsupported_rule_expression:{type(node).__name__}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS:
                raise RulePlanError("unsupported_rule_function")
            if node.keywords:
                raise RulePlanError("rule_function_keywords_not_allowed")
            _validate_function_arity(node.func.id, len(node.args))
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise RulePlanError("invalid_rule_name")
        if (
            allowed_names is not None
            and isinstance(node, ast.Name)
            and node.id not in _ALLOWED_FUNCTIONS
            and node.id.lower() not in allowed_names
        ):
            raise RulePlanError(f"unknown_rule_name:{node.id}")


_ALLOWED_AST_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Call, ast.Name,
    ast.Load, ast.Constant, ast.And, ast.Or, ast.Not, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.USub, ast.UAdd, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq, ast.Is, ast.IsNot,
)
_ALLOWED_FUNCTIONS = {
    "latest_cross", "cross_above", "cross_below", "lowest_low", "highest_high",
    "indicator", "consecutive", "pattern", "abs", "min", "max",
}
_OPEN_NAMES = {"bid", "ask", "true", "false"}
_POSITION_NAMES = _OPEN_NAMES | {
    "side", "open_price", "current_price", "sl", "tp", "volume", "profit", "favorable_move", "stop_distance",
}


def _validate_function_arity(name: str, count: int) -> None:
    allowed_counts: dict[str, set[int]] = {
        "latest_cross": {3}, "cross_above": {3}, "cross_below": {3},
        "lowest_low": {1}, "highest_high": {1},
        "indicator": {1, 2}, "consecutive": {2}, "pattern": {1, 2},
        "abs": {1},
    }
    if name in allowed_counts and count not in allowed_counts[name]:
        raise RulePlanError(f"invalid_rule_function_arity:{name}")
    if name in {"min", "max"} and count < 1:
        raise RulePlanError(f"invalid_rule_function_arity:{name}")


def _has_side_guard(expression: str, expected: str) -> bool:
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
            continue
        left = node.left
        right = node.comparators[0]
        if isinstance(left, ast.Name) and left.id.lower() == "side" and isinstance(right, ast.Constant):
            if str(right.value).upper() == expected:
                return True
        if isinstance(right, ast.Name) and right.id.lower() == "side" and isinstance(left, ast.Constant):
            if str(left.value).upper() == expected:
                return True
    return False


class _RuleContext:
    def __init__(
        self,
        *,
        candles: list[Candle],
        indicators: dict[str, Any],
        request: OpenEvaluateRequest | PositionEvaluateRequest,
        position: PositionSnapshot | None = None,
    ) -> None:
        # MT4/MT5 clients may send bars newest-first while indicator
        # calculation already normalizes them oldest-first. Keep every rule
        # primitive on the same time convention before slicing recent bars.
        self.candles = sorted(candles, key=lambda item: item.timestamp)
        self.request = request
        self.position = position
        raw_values = indicators.get("values") if isinstance(indicators, dict) else {}
        self.indicators = {
            str(key).lower(): [float(item) for item in value if item is not None]
            for key, value in (raw_values.items() if isinstance(raw_values, dict) else [])
            if isinstance(value, list)
        }

    def evaluate(self, expression: str) -> Any:
        _validate_expression(expression)
        return self._eval(ast.parse(expression, mode="eval").body)

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self._name(node.id)
        if isinstance(node, ast.BoolOp):
            values = [bool(self._eval(item)) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.UnaryOp):
            value = self._eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not bool(value)
            return -float(value) if isinstance(node.op, ast.USub) else float(value)
        if isinstance(node, ast.BinOp):
            left, right = self._eval(node.left), self._eval(node.right)
            if isinstance(node.op, ast.Add): return float(left) + float(right)
            if isinstance(node.op, ast.Sub): return float(left) - float(right)
            if isinstance(node.op, ast.Mult): return float(left) * float(right)
            if isinstance(node.op, ast.Div): return float(left) / float(right)
        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for operator, comparator in zip(node.ops, node.comparators, strict=True):
                right = self._eval(comparator)
                if not _compare(left, right, operator):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return self._call(node.func.id, [self._eval(item) for item in node.args])
        raise RulePlanError("unsupported_rule_expression")

    def _name(self, name: str) -> Any:
        normalized = name.lower()
        if normalized in self.indicators:
            values = self.indicators[normalized]
            return values[-1] if values else None
        if normalized == "bid": return self.request.bid
        if normalized == "ask": return self.request.ask
        if normalized in {"true", "false"}: return normalized == "true"
        if self.position is None:
            raise RulePlanError(f"unknown_rule_name:{name}")
        mapping = {
            "side": self.position.side,
            "open_price": self.position.open_price,
            "current_price": self.position.current_price,
            "sl": self.position.sl,
            "tp": self.position.tp,
            "volume": self.position.volume,
            "profit": self.position.profit,
            "favorable_move": (
                self.position.current_price - self.position.open_price
                if self.position.side == "BUY"
                else self.position.open_price - self.position.current_price
            ),
            "stop_distance": abs(self.position.current_price - self.position.sl) if self.position.sl else 0,
        }
        if normalized not in mapping:
            raise RulePlanError(f"unknown_rule_name:{name}")
        return mapping[normalized]

    def _call(self, name: str, args: list[Any]) -> Any:
        if name in {"abs", "min", "max"}:
            fn = {"abs": abs, "min": min, "max": max}[name]
            return fn(*args)
        if name == "indicator":
            alias = str(args[0]).lower()
            offset = int(args[1]) if len(args) > 1 else 0
            values = self.indicators.get(alias) or []
            index = len(values) - 1 + offset
            if not 0 <= index < len(values):
                raise RulePlanError("indicator_value_missing")
            return values[index]
        if name in {"latest_cross", "cross_above", "cross_below"}:
            left, right, bars = str(args[0]).lower(), str(args[1]).lower(), int(args[2])
            event = self._latest_cross(left, right, bars)
            return event if name == "latest_cross" else event == (1 if name == "cross_above" else -1)
        if name in {"lowest_low", "highest_high"}:
            bars = max(1, int(args[0]))
            selected = self.candles[-bars:]
            if len(selected) < bars:
                raise RulePlanError("candle_data_missing")
            return min(item.low for item in selected) if name == "lowest_low" else max(item.high for item in selected)
        if name == "consecutive":
            direction, count = str(args[0]).lower(), max(1, int(args[1]))
            selected = self.candles[-count:]
            if len(selected) < count:
                return False
            return all(item.close > item.open for item in selected) if direction == "up" else all(item.close < item.open for item in selected)
        if name == "pattern":
            pattern_name = str(args[0]).lower()
            within = max(1, int(args[1])) if len(args) > 1 else 1
            return any(_candle_pattern(self.candles, pattern_name, offset) for offset in range(within))
        raise RulePlanError("unsupported_rule_function")

    def _latest_cross(self, left: str, right: str, bars: int) -> int:
        left_values, right_values = self.indicators.get(left) or [], self.indicators.get(right) or []
        size = min(len(left_values), len(right_values))
        bars = max(2, min(int(bars), size))
        start = size - bars + 1
        latest = 0
        for index in range(max(1, start), size):
            if left_values[index - 1] <= right_values[index - 1] and left_values[index] > right_values[index]:
                latest = 1
            elif left_values[index - 1] >= right_values[index - 1] and left_values[index] < right_values[index]:
                latest = -1
        return latest


def _compare(left: Any, right: Any, operator: ast.cmpop) -> bool:
    if isinstance(operator, ast.Is): return left is right
    if isinstance(operator, ast.IsNot): return left is not right
    if isinstance(operator, ast.Eq): return left == right
    if isinstance(operator, ast.NotEq): return left != right
    if left is None or right is None:
        return False
    if isinstance(operator, ast.Gt): return left > right
    if isinstance(operator, ast.GtE): return left >= right
    if isinstance(operator, ast.Lt): return left < right
    if isinstance(operator, ast.LtE): return left <= right
    raise RulePlanError("unsupported_rule_comparison")


def _same_price(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs(float(left) - float(right)) <= max(1e-9, abs(float(right)) * 1e-10)


def _candle_pattern(candles: list[Candle], name: str, offset: int) -> bool:
    index = len(candles) - 1 - offset
    if index < 0:
        return False
    current = candles[index]
    body = abs(current.close - current.open)
    total = max(current.high - current.low, 1e-12)
    upper = current.high - max(current.open, current.close)
    lower = min(current.open, current.close) - current.low
    if name == "doji":
        return body <= total * 0.1
    if name == "bullish_pinbar":
        return current.close >= current.open and lower >= max(body * 2, total * 0.5) and upper <= total * 0.25
    if name == "bearish_pinbar":
        return current.close <= current.open and upper >= max(body * 2, total * 0.5) and lower <= total * 0.25
    if index < 1:
        return False
    previous = candles[index - 1]
    if name == "bullish_engulfing":
        return previous.close < previous.open and current.close > current.open and current.open <= previous.close and current.close >= previous.open
    if name == "bearish_engulfing":
        return previous.close > previous.open and current.close < current.open and current.open >= previous.close and current.close <= previous.open
    return False

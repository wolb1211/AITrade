from __future__ import annotations

import json
import re
from copy import deepcopy
from hashlib import sha256
import logging
from dataclasses import dataclass
from socket import timeout as SocketTimeout
from threading import Lock
from time import perf_counter
from typing import Any
from urllib import error, request

from app.models import Candle, OpenEvaluateRequest, PositionEvaluateRequest, UsageSummary
from app.services.custom_indicators import (
    INDICATOR_DEFINITIONS,
    calculate_indicator_payload,
    normalize_indicator_specs,
    public_indicator_catalog,
    required_candle_count,
)
from app.services.custom_rule_engine import RULE_ENGINE_VERSION, RulePlanError, normalize_rule_plan
from app.services.custom_workflow import (
    compile_workflow,
    workflow_stage_json_schema,
    workflow_stage_validation_result,
)
from app.store import SqliteStore

logger = logging.getLogger(__name__)

_VISION_TEST_IMAGE_PNG_DATA_URL = (
    "data:image/png;base64,"
    "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"
)

# JPEG is accepted more consistently than PNG data URLs by OpenAI-compatible gateways.
_VISION_TEST_IMAGE_DATA_URL = (
    "data:image/jpeg;base64,"
    "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"
)


@dataclass(frozen=True)
class AiCallResult:
    content: dict[str, Any]
    usage: UsageSummary


class AiDecisionClient:
    def __init__(self, store: SqliteStore, *, timeout: float = 20.0) -> None:
        self.store = store
        self.timeout = timeout
        self._cache_locks = tuple(Lock() for _ in range(64))

    def pa_open_decision(
        self,
        *,
        deployment: dict[str, Any],
        request_payload: OpenEvaluateRequest,
        features: dict[str, Any],
    ) -> AiCallResult | None:
        return self._chat_json(
            deployment=deployment,
            endpoint="open",
            system_prompt=_pa_system_prompt(),
            user_payload={
                "task": "open_decision",
                "strategy_name": deployment["strategy_name"],
                "strategy_summary": deployment["config"].get("summary", ""),
                "open_logic": deployment["config"].get("open_logic", ""),
                "symbol": request_payload.symbol,
                "timeframe": request_payload.timeframe,
                "account": request_payload.account.model_dump(mode="json"),
                "bid": request_payload.bid,
                "ask": request_payload.ask,
                "spread_points": request_payload.spread_points,
                "balance": request_payload.balance,
                "equity": request_payload.equity,
                "features": features,
                "stage1_market_diagnosis": {
                    "market_regime": {
                        "cycle_position": features.get("cycle_position"),
                        "background_direction": features.get("background_direction"),
                        "recent_direction": features.get("recent_direction"),
                        "trend_relationship": features.get("trend_relationship"),
                        "recent_spike": features.get("recent_spike"),
                        "market_phase": features.get("market_phase"),
                        "transition_risk": features.get("transition_risk"),
                        "climax_risk": features.get("climax_risk"),
                        "always_in": features.get("always_in"),
                    },
                    "patterns": features.get("detected_patterns"),
                    "pattern_details": {
                        "wedge_type": features.get("wedge_type"),
                        "triangle_type": features.get("triangle_type"),
                        "double_structure": features.get("double_structure"),
                        "mtr_candidate": features.get("mtr_candidate"),
                        "final_flag_candidate": features.get("final_flag_candidate"),
                    },
                    "signal_bar": {
                        "type": features.get("signal_bar_type"),
                        "quality": features.get("signal_bar_quality"),
                        "follow_through": features.get("follow_through"),
                    },
                    "bar_by_bar_summary": features.get("bar_by_bar"),
                    "program_recommendation": {
                        "bias": features.get("setup_bias"),
                        "score": features.get("setup_score"),
                        "setup": features.get("setup_name"),
                        "setup_code": features.get("setup_code"),
                        "score_components": features.get("setup_components"),
                        "long_score": features.get("long_score"),
                        "short_score": features.get("short_score"),
                        "score_margin": features.get("score_margin"),
                        "candidate_direction": features.get("server_candidate_direction"),
                        "structure_stop": features.get("server_structure_sl"),
                        "minimum_risk_reward": features.get("server_min_risk_reward"),
                        "rule": (
                            "The server candidate direction is fixed. AI may approve that direction or reject the trade, "
                            "but must never reverse it. The final server also preserves the structure stop and minimum risk-reward."
                        ),
                    },
                    "risk_notes": [
                        "Avoid trading when barbwire_candidate is true unless breakout/retest or failed-breakout reversal is clear.",
                        "If opening, use sl_distance_price and tp_distance_price; keep stops outside structure and spread noise.",
                        "For testing this strategy, do not require perfect breakout only; high-quality pullback or failed-breakout reversal is acceptable.",
                    ],
                },
                "recent_candles": _compact_candles(request_payload.candles, limit=40),
                "required_json_schema": {
                    "should_open": "boolean",
                    "direction": "buy|sell|null",
                    "confidence": "0..1 number",
                    "lot": "number, default 0.01",
                    "sl_distance_price": "positive price distance from entry",
                    "tp_distance_price": "positive price distance from entry",
                    "reason": "short Chinese reason",
                    "analysis": "detailed final Chinese market explanation",
                },
            },
        )

    def compile_custom_strategy(self, deployment: dict[str, Any]) -> dict[str, Any]:
        """Turn the user's natural-language rules into a reusable runtime definition."""
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        open_logic = str(config.get("open_logic") or "").strip()
        position_logic = str(config.get("position_logic") or "").strip()
        open_result = self._chat_json(
            deployment=deployment,
            endpoint="compile_open",
            system_prompt=_custom_strategy_stage_compile_prompt("open"),
            user_payload={
                "task": "compile_custom_open_strategy",
                "stage": "open",
                "user_logic": open_logic,
                "available_indicators": public_indicator_catalog(),
                "rules": {
                    "candlestick_patterns": "Do not create indicators for candlestick patterns; runtime supplies OHLCV arrays.",
                    "unsupported_indicator": "Put indicators outside available_indicators into unsupported_indicators.",
                    "indicators": "Include every explicitly referenced built-in indicator and every period, such as EMA5 and EMA30.",
                    "data_type": "Use kline unless the rule explicitly requires a chart image or an unsupported custom indicator.",
                    "execution": "Preserve entry direction, trigger, stop-loss and take-profit rules without inventing conditions.",
                },
            },
        )
        position_result = self._chat_json(
            deployment=deployment,
            endpoint="compile_position",
            system_prompt=_custom_strategy_stage_compile_prompt("position"),
            user_payload={
                "task": "compile_custom_position_strategy",
                "stage": "position",
                "user_logic": position_logic,
                "available_indicators": public_indicator_catalog(),
                "runtime_data_contract": {
                    "positions": {
                        "fields": ["ticket", "side", "volume", "open_price", "current_price", "profit", "sl", "tp"],
                        "profit": "Account-currency P/L. Never compare it with ATR or another price distance.",
                        "favorable_price_move": "BUY: current_price - open_price; SELL: open_price - current_price.",
                    },
                    "indicators": (
                        "indicators.values is a map of alias arrays such as atr14, ema5 and ema30. Arrays are oldest "
                        "to newest; [-1] is the latest closed candle and [-2] is the previous closed candle."
                    ),
                    "atr": "ATR is a price distance calculated from high, low and close; it is not account-currency profit.",
                },
                "rules": {
                    "candlestick_patterns": "Do not create indicators for candlestick patterns; runtime supplies OHLCV arrays.",
                    "unsupported_indicator": "Put indicators outside available_indicators into unsupported_indicators.",
                    "indicators": "Include every explicitly referenced built-in indicator and every period, even when also used for opening.",
                    "data_type": "Use kline unless the rule explicitly requires a chart image or an unsupported custom indicator.",
                    "execution": "Do not invent new action types. Position actions are hold, close, add, modify.",
                    "staged_rules": (
                        "Preserve temporal words such as first, once, then, after, thereafter, already and not-yet. "
                        "Compile sequential stop rules into mutually exclusive stages. A completed earlier stage must "
                        "not become eligible again when the user says so; determine completion from observable position "
                        "fields referenced by the user's rule instead of inventing persistent state."
                    ),
                    "add_volume": (
                        "If position rules request adding but do not define a lot calculation, preserve the add condition, "
                        "state in the position template that lot must be null so the server uses the opening sizing algorithm, "
                        "and add a Chinese warning explaining this default. If a calculation is defined, preserve it exactly."
                    ),
                    "partial_close": (
                        "If position rules request partial close without a percentage or explicit volume, add a Chinese warning "
                        "asking the user to specify it. The runtime template must hold instead of guessing a close volume."
                    ),
                },
            },
        )
        open_content = open_result.content if open_result is not None else {}
        position_content = position_result.content if position_result is not None else {}
        open_prompt = str(
            open_content.get("prompt_template") or open_content.get("open_prompt_template") or ""
        ).strip() if isinstance(open_content, dict) else ""
        position_prompt = str(
            position_content.get("prompt_template") or position_content.get("position_prompt_template") or ""
        ).strip() if isinstance(position_content, dict) else ""
        if not open_prompt or not position_prompt:
            raise RuntimeError("custom_strategy_compile_failed")
        open_summary = _clean_stage_summary(open_content.get("summary"))
        position_summary = _clean_stage_summary(position_content.get("summary"))
        combined = {
            "summary": "；".join(filter(None, (
                f"开仓：{open_summary}" if open_summary else "",
                f"持仓风控：{position_summary}" if position_summary else "",
            ))),
            "open_prompt_template": open_prompt,
            "position_prompt_template": position_prompt,
            "open_indicators": open_content.get("indicators") or open_content.get("open_indicators") or [],
            "position_indicators": position_content.get("indicators") or position_content.get("position_indicators") or [],
            "open_rule_plan": open_content.get("rule_plan") or open_content.get("open_rule_plan") or {},
            "position_rule_plan": position_content.get("rule_plan") or position_content.get("position_rule_plan") or {},
            "open_data_type": config.get("open_data_type") or open_content.get("data_type") or open_content.get("open_data_type") or "kline",
            "position_data_type": config.get("position_data_type") or position_content.get("data_type") or position_content.get("position_data_type") or "kline",
            "unsupported_indicators": [
                *_as_list(open_content.get("unsupported_indicators")),
                *_as_list(position_content.get("unsupported_indicators")),
            ],
            "unsupported_conditions": [
                *_stage_unsupported_conditions(open_content.get("unsupported_conditions"), "open"),
                *_stage_unsupported_conditions(position_content.get("unsupported_conditions"), "position"),
            ],
            "visual_conditions": [
                *_stage_unsupported_conditions(open_content.get("visual_conditions"), "open"),
                *_stage_unsupported_conditions(position_content.get("visual_conditions"), "position"),
            ],
            "warnings": [*_as_list(open_content.get("warnings")), *_as_list(position_content.get("warnings"))],
        }
        return self.normalize_custom_strategy_compilation(
            combined,
            open_logic=open_logic,
            position_logic=position_logic,
        )

    def generate_custom_workflow_stage(
        self,
        deployment: dict[str, Any],
        *,
        stage: str,
        user_logic: str,
        data_requirements: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate and validate one visual workflow stage without persisting a strategy."""
        if stage not in {"open", "position"}:
            raise RuntimeError("invalid_workflow_stage")
        # ai_usage_logs.endpoint is intentionally compact (VARCHAR(16) in the
        # existing MySQL schema).  "workflow_position" exceeds that limit and
        # made an otherwise successful position-workflow response look like an
        # AI generation failure while usage metadata was being saved.
        endpoint = "workflow_open" if stage == "open" else "workflow_pos"
        base_payload = {
            "task": "generate_visual_strategy_workflow_stage",
            "stage": stage,
            "user_logic": user_logic,
            "data_requirements": data_requirements,
            "available_indicators": public_indicator_catalog(),
            "workflow_stage_schema": workflow_stage_json_schema(),
            "required_response": {"stage": "one WorkflowStage object matching workflow_stage_schema"},
        }
        result = self._chat_json(
            deployment=deployment,
            endpoint=endpoint,
            system_prompt=_custom_workflow_stage_generation_prompt(stage),
            user_payload=base_payload,
        )
        candidate = _normalize_generated_workflow_stage(
            _workflow_stage_candidate(result.content if result is not None else {}),
            stage=stage,
            data_requirements=data_requirements,
            user_logic=user_logic,
        )
        validation = workflow_stage_validation_result(candidate, stage)  # type: ignore[arg-type]
        repaired = False
        if not validation["valid"]:
            repaired = True
            source_rules = _workflow_source_rules(user_logic)
            classified_rules = []
            for index, source_rule in enumerate(source_rules):
                action_kind = _classify_workflow_source_rule(source_rule, stage=stage)
                if action_kind:
                    classified_rules.append({
                        "rule_index": index + 1,
                        "label": _workflow_rule_label(source_rule, action_kind),
                        "action_kind": action_kind,
                    })
            candidate = _workflow_stage_from_classified_rules(
                {"rules": classified_rules},
                stage=stage,
                data_requirements=data_requirements,
                source_rules=source_rules,
            )
            validation = workflow_stage_validation_result(candidate, stage)  # type: ignore[arg-type]
        if not validation["valid"] or not isinstance(validation.get("stage"), dict):
            logger.warning(
                "Workflow generation validation failed after repair: stage=%s errors=%s",
                stage,
                json.dumps(validation.get("errors") or [], ensure_ascii=False),
            )
            raise RuntimeError("workflow_generation_validation_failed")
        return {
            "stage": validation["stage"],
            "source_text": user_logic,
            "repaired": repaired,
        }

    def compile_custom_workflow(
        self,
        workflow: Any,
        *,
        open_logic: str,
        position_logic: str,
    ) -> dict[str, Any]:
        """Compile the confirmed visual workflow without asking a runtime model to reinterpret it."""
        compiled_workflow = compile_workflow(workflow)

        def stage_prompt(stage_name: str) -> str:
            title = "开仓" if stage_name == "open" else "持仓风控"
            stage = compiled_workflow[stage_name]
            return (
                f"严格执行用户已确认的{title}可视化流程。必须从 entry_node_id 开始，按 transitions 逐个判断；"
                "判断节点只允许选择 yes 或 no 对应分支，抵达动作节点后立即执行并结束本次流程。"
                "不得添加、删除、改写或优化任何条件和动作。流程数据如下：\n"
                + json.dumps(stage, ensure_ascii=False, separators=(",", ":"))
            )

        open_stage = compiled_workflow["open"]
        position_stage = compiled_workflow["position"]
        unsupported_conditions = [
            {"stage": stage_name, **item}
            for stage_name, stage_data in (("open", open_stage), ("position", position_stage))
            for item in stage_data.get("unsupported_conditions", [])
        ]
        content = {
            "summary": "已根据开仓与持仓风控流程图生成策略配置，运行时严格按已确认的节点和分支执行。",
            "open_prompt_template": stage_prompt("open"),
            "position_prompt_template": stage_prompt("position"),
            "open_indicators": open_stage.get("indicators", []),
            "position_indicators": position_stage.get("indicators", []),
            "open_rule_plan": {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []},
            "position_rule_plan": {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []},
            "unsupported_indicators": [],
            "unsupported_conditions": unsupported_conditions,
            "visual_conditions": [],
            "warnings": [],
        }
        normalized = self.normalize_custom_strategy_compilation(
            content,
            open_logic=open_logic,
            position_logic=position_logic,
        )
        # The visual workflow itself is the executable contract. A missing
        # legacy rule_plan is therefore not an unsupported condition; only
        # explicit ai_condition nodes should be counted as unstructured.
        normalized["unsupported_conditions"] = [
            item for item in normalized.get("unsupported_conditions", [])
            if str(item.get("code") or "") != "rule_plan_unavailable"
        ]
        normalized["unsupported_condition_count"] = len(normalized["unsupported_conditions"])
        normalized.update({
            "workflow": workflow,
            "compiled_workflow": compiled_workflow,
            "compile_status": "generated",
        })
        return normalized

    def normalize_custom_strategy_compilation(
        self,
        content: Any,
        *,
        open_logic: str = "",
        position_logic: str = "",
    ) -> dict[str, Any]:
        """Validate and normalize an AI compilation before previewing or persisting it."""
        if not isinstance(content, dict):
            raise RuntimeError("invalid_custom_strategy_compilation")
        open_prompt = str(content.get("open_prompt_template") or "").strip()
        position_prompt = str(content.get("position_prompt_template") or "").strip()
        if not open_prompt or not position_prompt:
            raise RuntimeError("invalid_custom_strategy_compilation")
        open_detected = _extract_explicit_indicator_specs(open_logic)
        position_detected = _extract_explicit_indicator_specs(position_logic)
        open_specs, open_unsupported = normalize_indicator_specs([
            *(content.get("open_indicators") if isinstance(content.get("open_indicators"), list) else []),
            *open_detected,
        ])
        position_specs, position_unsupported = normalize_indicator_specs([
            *(content.get("position_indicators") if isinstance(content.get("position_indicators"), list) else []),
            *position_detected,
        ])
        raw_unsupported = content.get("unsupported_indicators")
        unsupported_items = raw_unsupported if isinstance(raw_unsupported, list) else [raw_unsupported] if raw_unsupported else []
        unsupported = []
        for item in [*unsupported_items, *open_unsupported, *position_unsupported]:
            name = str(item).strip()
            if name and name not in unsupported:
                unsupported.append(name)
        raw_warnings = content.get("warnings")
        warning_items = raw_warnings if isinstance(raw_warnings, list) else [raw_warnings] if raw_warnings else []
        warnings = _reconcile_compilation_warnings(position_logic, warning_items)
        position_prompt = _apply_position_template_guardrails(position_prompt, position_specs)
        open_plan_value = _rewrite_rule_plan_indicator_aliases(content.get("open_rule_plan"), open_specs)
        position_plan_value = _rewrite_rule_plan_indicator_aliases(content.get("position_rule_plan"), position_specs)
        try:
            _validate_user_cross_rules(open_logic, open_plan_value)
            open_rule_plan = normalize_rule_plan(
                open_plan_value,
                stage="open",
                indicator_aliases={str(item.get("alias") or item.get("name") or "").lower() for item in open_specs},
            )
        except RulePlanError as exc:
            logger.warning("Open rule plan validation failed: %s", exc)
            open_rule_plan = {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []}
            warnings.append("开仓规则暂未能转换为精确执行规则，将继续使用 AI 判断。")
        try:
            _validate_user_stop_constraints(position_logic, position_plan_value)
            _validate_user_cross_rules(position_logic, position_plan_value)
            position_rule_plan = normalize_rule_plan(
                position_plan_value,
                stage="position",
                indicator_aliases={str(item.get("alias") or item.get("name") or "").lower() for item in position_specs},
            )
        except RulePlanError as exc:
            logger.warning("Position rule plan validation failed: %s", exc)
            position_rule_plan = {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []}
            warnings.append("持仓风控规则暂未能转换为精确执行规则，将继续使用 AI 判断。")
        unsupported_conditions = _normalize_unsupported_conditions(content.get("unsupported_conditions"))
        visual_conditions = _normalize_visual_conditions(content.get("visual_conditions"))
        open_data_type = _custom_data_type(content.get("open_data_type"), unsupported)
        position_data_type = _custom_data_type(content.get("position_data_type"), unsupported)
        if open_data_type in {"screenshot", "both"}:
            open_rule_plan = {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []}
        if position_data_type in {"screenshot", "both"}:
            position_rule_plan = {"version": RULE_ENGINE_VERSION, "mode": "ai", "rules": []}
        stages_with_gaps = {str(item.get("stage") or "") for item in unsupported_conditions}
        stages_with_visual = {str(item.get("stage") or "") for item in visual_conditions}
        if (
            open_logic.strip() and open_rule_plan.get("mode") != "deterministic"
            and "open" not in stages_with_gaps and "open" not in stages_with_visual
        ):
            unsupported_conditions.append({
                "stage": "open",
                "code": "rule_plan_unavailable",
                "text": open_logic.strip()[:1000],
                "reason": "当前规则暂时无法完整转换为服务端精确执行条件",
            })
        if (
            position_logic.strip() and position_rule_plan.get("mode") != "deterministic"
            and "position" not in stages_with_gaps and "position" not in stages_with_visual
        ):
            unsupported_conditions.append({
                "stage": "position",
                "code": "rule_plan_unavailable",
                "text": position_logic.strip()[:1000],
                "reason": "当前规则暂时无法完整转换为服务端精确执行条件",
            })
        return {
            "summary": str(content.get("summary") or "").strip()[:1000],
            "open_prompt_template": open_prompt[:8000],
            "position_prompt_template": position_prompt[:8000],
            "open_indicators": open_specs,
            "position_indicators": position_specs,
            "open_rule_plan": open_rule_plan,
            "position_rule_plan": position_rule_plan,
            "rule_engine_version": RULE_ENGINE_VERSION,
            "open_kline_count": required_candle_count(open_specs),
            "position_kline_count": required_candle_count(position_specs),
            "open_data_type": open_data_type,
            "position_data_type": position_data_type,
            "unsupported_indicators": unsupported[:20],
            "unsupported_conditions": unsupported_conditions[:30],
            "unsupported_condition_count": len(unsupported_conditions[:30]),
            "visual_conditions": visual_conditions[:30],
            "warnings": warnings[:10],
            "prompt_version": 3,
            "compile_status": "generated",
        }

    def custom_open_decision(
        self,
        *,
        deployment: dict[str, Any],
        request_payload: OpenEvaluateRequest,
    ) -> AiCallResult | None:
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        indicator_count = max(20, min(int(config.get("indicator_output_count") or 100), 300))
        candle_count = max(10, min(int(config.get("open_requested_kline_count") or config.get("open_kline_count") or 100), 1000))
        indicators = calculate_indicator_payload(
            request_payload.candles,
            list(config.get("open_indicators") or []),
            output_count=indicator_count,
        )
        return self._chat_json(
            deployment=deployment,
            endpoint="open",
            system_prompt=_custom_runtime_prompt("open"),
            user_payload={
                "task": "custom_strategy_open_decision",
                "strategy_name": deployment.get("strategy_name", ""),
                "user_rule": config.get("open_logic", ""),
                "prompt_template": _custom_runtime_template(config.get("open_prompt_template", "")),
                "data_convention": {
                    "order": "oldest_to_latest",
                    "last_item": "latest_closed_candle",
                    "prices": "absolute market prices",
                    "rule_evaluation": "Evaluate requested periods, crossovers and candle patterns directly from candles and indicator arrays.",
                },
                "symbol": request_payload.symbol,
                "timeframe": request_payload.timeframe,
                "account": request_payload.account.model_dump(mode="json"),
                "bid": request_payload.bid,
                "ask": request_payload.ask,
                "spread_points": request_payload.spread_points,
                "balance": request_payload.balance,
                "equity": request_payload.equity,
                "candles": _compact_candles(request_payload.candles, limit=candle_count),
                "indicators": indicators,
                "computed_facts": _workflow_computed_facts(config, "open", indicators),
                "screenshot": _screenshot_ai_metadata(request_payload.screenshot_metadata),
                "visual_conditions": _runtime_visual_conditions(config, "open"),
                "required_json_schema": {
                    "should_open": "boolean",
                    "direction": "buy|sell|null",
                    "confidence": "0..1 number",
                    "sl": "absolute stop-loss price or null",
                    "tp": "absolute take-profit price or null",
                    "reason": "short Chinese reason",
                    "analysis": "short natural Chinese strategy explanation; describe rule results and action without raw values or calculations",
                },
            },
            user_image_url=request_payload.screenshot_data_url,
        )

    def custom_position_decision(
        self,
        *,
        deployment: dict[str, Any],
        request_payload: PositionEvaluateRequest,
    ) -> AiCallResult | None:
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        indicator_count = max(20, min(int(config.get("indicator_output_count") or 100), 300))
        candle_count = max(10, min(int(config.get("position_requested_kline_count") or config.get("position_kline_count") or 100), 1000))
        indicators = calculate_indicator_payload(
            request_payload.candles,
            list(config.get("position_indicators") or []),
            output_count=indicator_count,
        )
        return self._chat_json(
            deployment=deployment,
            endpoint="position",
            system_prompt=_custom_runtime_prompt("position"),
            user_payload={
                "task": "custom_strategy_position_decision",
                "strategy_name": deployment.get("strategy_name", ""),
                "user_rule": config.get("position_logic", ""),
                "prompt_template": _custom_runtime_template(config.get("position_prompt_template", "")),
                "data_convention": {
                    "order": "oldest_to_latest",
                    "last_item": "latest_closed_candle",
                    "prices": "absolute market prices",
                    "rule_evaluation": "Evaluate requested periods, crossovers and candle patterns directly from candles and indicator arrays.",
                },
                "symbol": request_payload.symbol,
                "timeframe": request_payload.timeframe,
                "account": request_payload.account.model_dump(mode="json"),
                "bid": request_payload.bid,
                "ask": request_payload.ask,
                "spread_points": request_payload.spread_points,
                "balance": request_payload.balance,
                "equity": request_payload.equity,
                "positions": [item.model_dump(mode="json") for item in request_payload.positions],
                "candles": _compact_candles(request_payload.candles, limit=candle_count),
                "indicators": indicators,
                "computed_facts": _workflow_computed_facts(config, "position", indicators, {"position": ([item.model_dump(mode="json") for item in request_payload.positions] or [{}])[0]}),
                "screenshot": _screenshot_ai_metadata(request_payload.screenshot_metadata),
                "visual_conditions": _runtime_visual_conditions(config, "position"),
                "required_json_schema": {
                    "action": "hold|close|add|modify",
                    "ticket": "target ticket or null",
                    "direction": "buy|sell|null, required for add",
                    "lot": "add volume or null; null tells server to use the opening sizing algorithm",
                    "close_scope": "full|partial|null, required for close",
                    "volume": "close volume or null; null means full close",
                    "sl": "absolute stop-loss price or null",
                    "tp": "absolute take-profit price or null",
                    "confidence": "0..1 number",
                    "reason": "short Chinese reason",
                    "analysis": "short natural Chinese strategy explanation; describe rule results and action without raw values or calculations",
                },
            },
            user_image_url=request_payload.screenshot_data_url,
        )

    def custom_rule_explanation(
        self,
        *,
        deployment: dict[str, Any],
        endpoint: str,
        calculated_result: dict[str, Any],
    ) -> AiCallResult | None:
        """Ask AI to explain an authoritative deterministic rule result.

        The caller must execute the rule-engine result, never the model's copy
        of the action fields. This keeps a real, billable AI analysis while
        preventing a language model from changing exact comparisons or prices.
        """
        if endpoint not in {"open", "position"}:
            raise ValueError("invalid_custom_rule_explanation_endpoint")
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        return self._chat_json(
            deployment=deployment,
            endpoint=endpoint,
            system_prompt=_custom_rule_explanation_prompt(endpoint),
            user_payload={
                "task": f"explain_deterministic_{endpoint}_result",
                "strategy_name": deployment.get("strategy_name", ""),
                "user_rule": config.get("open_logic" if endpoint == "open" else "position_logic", ""),
                "calculated_result": calculated_result,
                "required_json_schema": (
                    {
                        "should_open": "copy authoritative result",
                        "direction": "copy authoritative result",
                        "confidence": 1,
                        "lot": 0,
                        "sl": "copy authoritative result",
                        "tp": "copy authoritative result",
                        "sl_distance_price": 0,
                        "tp_distance_price": 0,
                        "reason": "short Chinese conclusion",
                        "analysis": "short natural Chinese explanation",
                    }
                    if endpoint == "open"
                    else {
                        "action": "copy authoritative result",
                        "ticket": None,
                        "direction": None,
                        "confidence": 1,
                        "lot": None,
                        "close_scope": None,
                        "volume": None,
                        "sl": None,
                        "tp": None,
                        "reason": "short Chinese conclusion",
                        "analysis": "short natural Chinese explanation",
                    }
                ),
            },
        )

    def test_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """Make a minimal provider call without billing, caching, or usage logging."""
        endpoint = self.store.get_private_ai_endpoint(endpoint_id)
        if endpoint is None:
            raise RuntimeError("ai_endpoint_not_found")
        return self.test_configuration(
            base_url=str(endpoint.get("base_url") or endpoint.get("provider_base_url") or ""),
            api_key=str(endpoint.get("api_key") or endpoint.get("provider_api_key") or ""),
            model=str(endpoint.get("model") or endpoint.get("name") or ""),
            strict_json=bool(endpoint.get("strict_json", True)),
            endpoint_id=endpoint_id,
        )

    def test_configuration(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        strict_json: bool = True,
        endpoint_id: str = "",
    ) -> dict[str, Any]:
        """Test an unsaved OpenAI-compatible configuration without side effects."""
        base_url = str(base_url or "").strip()
        api_key = str(api_key or "").strip()
        model = str(model or "").strip()
        if not base_url:
            raise RuntimeError("ai_endpoint_base_url_missing")
        if not api_key:
            raise RuntimeError("ai_endpoint_api_key_missing")
        if not model:
            raise RuntimeError("ai_endpoint_model_missing")

        started_at = perf_counter()
        raw_response = self._post_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt="You are an API connection tester. Follow the user's output instruction exactly.",
            user_prompt=(
                'Return only this JSON object: {"status":"ok","message":"connection successful"}'
                if strict_json else "Reply with exactly: OK"
            ),
            max_tokens=256,
            strict_json=strict_json,
        )
        elapsed_ms = max(1, round((perf_counter() - started_at) * 1000))
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AI provider returned invalid JSON: {_preview_text(raw_response)}") from exc
        choice = (parsed.get("choices") or [{}])[0] if isinstance(parsed, dict) else {}
        message_payload = choice.get("message") if isinstance(choice, dict) else {}
        if not isinstance(message_payload, dict):
            message_payload = {}
        content = str(message_payload.get("content") or "").strip()
        if not content and message_payload.get("reasoning_content"):
            content = str(message_payload.get("reasoning_content") or "").strip()
        if not content:
            raise RuntimeError(f"AI provider response content empty: {_preview_text(raw_response)}")
        if strict_json:
            _extract_json_object(content)
        usage = parsed.get("usage") if isinstance(parsed, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        return {
            "success": True,
            "endpoint_id": endpoint_id,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "response_preview": _preview_text(content, limit=200),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }

    def test_vision_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """Test and persist whether an official endpoint can understand images."""
        endpoint = self.store.get_private_ai_endpoint(endpoint_id)
        if endpoint is None:
            raise RuntimeError("ai_endpoint_not_found")
        try:
            result = self.test_vision_configuration(
                base_url=str(endpoint.get("base_url") or endpoint.get("provider_base_url") or ""),
                api_key=str(endpoint.get("api_key") or endpoint.get("provider_api_key") or ""),
                model=str(endpoint.get("model") or endpoint.get("name") or ""),
                endpoint_id=endpoint_id,
            )
        except (RuntimeError, ValueError, TimeoutError) as exc:
            self.store.save_ai_endpoint_vision_test(endpoint_id, passed=False, error_message=str(exc))
            raise
        self.store.save_ai_endpoint_vision_test(endpoint_id, passed=True)
        return result

    def test_vision_configuration(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        endpoint_id: str = "",
    ) -> dict[str, Any]:
        """Test image understanding without billing, cache, or usage records."""
        base_url = str(base_url or "").strip()
        api_key = str(api_key or "").strip()
        model = str(model or "").strip()
        if not base_url:
            raise RuntimeError("ai_endpoint_base_url_missing")
        if not api_key:
            raise RuntimeError("ai_endpoint_api_key_missing")
        if not model:
            raise RuntimeError("ai_endpoint_model_missing")

        started_at = perf_counter()
        raw_response = self._post_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt="You are an image recognition tester. Follow the output instruction exactly.",
            user_prompt=(
                "Identify the two colored shapes in the attached image from left to right. "
                "Reply only in this format: COLOR SHAPE, COLOR SHAPE. "
                "Do not guess when no image is visible."
            ),
            user_image_url=_VISION_TEST_IMAGE_DATA_URL,
            max_tokens=512,
            strict_json=False,
        )
        elapsed_ms = max(1, round((perf_counter() - started_at) * 1000))
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"AI provider returned invalid JSON: {_preview_text(raw_response)}") from exc
        choice = (parsed.get("choices") or [{}])[0] if isinstance(parsed, dict) else {}
        message_payload = choice.get("message") if isinstance(choice, dict) else {}
        content = str(message_payload.get("content") or "").strip() if isinstance(message_payload, dict) else ""
        if not _vision_test_answer_is_correct(content):
            raise RuntimeError(
                f"模型未能正确识别测试图片（期望：红色圆形、蓝色方形；返回：{_preview_text(content or raw_response)}）"
            )
        usage = parsed.get("usage") if isinstance(parsed, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        return {
            "success": True,
            "supports_vision": True,
            "vision_test_status": "passed",
            "endpoint_id": endpoint_id,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "response_preview": _preview_text(content, limit=200),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }

    def pa_position_decision(
        self,
        *,
        deployment: dict[str, Any],
        request_payload: PositionEvaluateRequest,
        features: dict[str, Any],
    ) -> AiCallResult | None:
        return self._chat_json(
            deployment=deployment,
            endpoint="position",
            system_prompt=_pa_system_prompt(),
            user_payload={
                "task": "position_decision",
                "strategy_name": deployment["strategy_name"],
                "strategy_summary": deployment["config"].get("summary", ""),
                "position_logic": deployment["config"].get("position_logic", ""),
                "symbol": request_payload.symbol,
                "timeframe": request_payload.timeframe,
                "account": request_payload.account.model_dump(mode="json"),
                "bid": request_payload.bid,
                "ask": request_payload.ask,
                "spread_points": request_payload.spread_points,
                "features": features,
                "stage1_market_diagnosis": {
                    "market_regime": {
                        "cycle_position": features.get("cycle_position"),
                        "background_direction": features.get("background_direction"),
                        "recent_direction": features.get("recent_direction"),
                        "trend_relationship": features.get("trend_relationship"),
                        "recent_spike": features.get("recent_spike"),
                        "market_phase": features.get("market_phase"),
                        "transition_risk": features.get("transition_risk"),
                        "climax_risk": features.get("climax_risk"),
                        "always_in": features.get("always_in"),
                    },
                    "patterns": features.get("detected_patterns"),
                    "pattern_details": {
                        "wedge_type": features.get("wedge_type"),
                        "triangle_type": features.get("triangle_type"),
                        "double_structure": features.get("double_structure"),
                        "mtr_candidate": features.get("mtr_candidate"),
                        "final_flag_candidate": features.get("final_flag_candidate"),
                    },
                    "signal_bar": {
                        "type": features.get("signal_bar_type"),
                        "quality": features.get("signal_bar_quality"),
                        "follow_through": features.get("follow_through"),
                    },
                    "bar_by_bar_summary": features.get("bar_by_bar"),
                    "program_recommendation": {
                        "bias": features.get("setup_bias"),
                        "score": features.get("setup_score"),
                        "setup": features.get("setup_name"),
                    },
                    "risk_rules": [
                        "Close if current position conflicts with a strong opposite setup.",
                        "Modify stop when profit exists and structure allows a tighter protective stop.",
                        "Add only when setup_score is high and direction matches the position plan.",
                    ],
                    "execution_constraints": {
                        "allow_add": bool(deployment["config"].get("allow_add")),
                        "max_positions": int(deployment["config"].get("max_positions") or 1),
                        "current_positions": len(request_payload.positions),
                        "rule": "Never request add when allow_add is false or current_positions reached max_positions.",
                    },
                },
                "positions": [item.model_dump(mode="json") for item in request_payload.positions],
                "recent_candles": _compact_candles(request_payload.candles, limit=40),
                "required_json_schema": {
                    "action": "hold|close|add|modify",
                    "ticket": "ticket string when action targets an existing position",
                    "direction": "buy|sell|null, required for add",
                    "confidence": "0..1 number",
                    "lot": "number for add",
                    "sl": "new stop loss price or null",
                    "tp": "new take profit price or null",
                    "reason": "short Chinese reason",
                    "analysis": "detailed final Chinese position and risk explanation",
                },
            },
        )

    def _chat_json(
        self,
        *,
        deployment: dict[str, Any],
        endpoint: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        user_image_url: str = "",
    ) -> AiCallResult | None:
        model = self._select_model(deployment, endpoint)
        if model is None:
            return None

        cache_settings = self.store.get_ai_cache_settings()
        if not bool(cache_settings.get("enabled", True)):
            return self._chat_json_uncached(
                deployment=deployment,
                endpoint=endpoint,
                system_prompt=system_prompt,
                user_payload=user_payload,
                model=model,
                user_image_url=user_image_url,
            )

        cache_key = self._cache_key(
            deployment=deployment,
            endpoint=endpoint,
            system_prompt=system_prompt,
            user_payload=user_payload,
            model=model,
        )
        cached = self.store.get_ai_response_cache(cache_key)
        if cached is not None:
            return self._cached_result(
                deployment=deployment,
                endpoint=endpoint,
                system_prompt=system_prompt,
                user_payload=user_payload,
                model=model,
                cached=cached,
            )

        lock = self._cache_locks[int(cache_key[:8], 16) % len(self._cache_locks)]
        with lock:
            cached = self.store.get_ai_response_cache(cache_key)
            if cached is not None:
                return self._cached_result(
                    deployment=deployment,
                    endpoint=endpoint,
                    system_prompt=system_prompt,
                    user_payload=user_payload,
                    model=model,
                    cached=cached,
                )
            return self._chat_json_uncached(
                deployment=deployment,
                endpoint=endpoint,
                system_prompt=system_prompt,
                user_payload=user_payload,
                model=model,
                user_image_url=user_image_url,
                cache_key=cache_key,
                cache_ttl_seconds=int(cache_settings.get("ttl_seconds") or 120),
            )

    def _chat_json_uncached(
        self,
        *,
        deployment: dict[str, Any],
        endpoint: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        model: dict[str, Any],
        user_image_url: str = "",
        cache_key: str = "",
        cache_ttl_seconds: int = 120,
    ) -> AiCallResult:

        provider_id = str(model["provider_id"])
        model_id = self._usage_model_id(model)
        is_custom = bool(model.get("is_custom"))
        prompt = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        final_system_prompt = _json_api_system_prompt(
            endpoint,
            system_prompt,
            literal_user_rules=_uses_literal_user_rules(deployment, endpoint),
        )
        request_snapshot = _format_request_snapshot(
            model=model,
            endpoint=endpoint,
            system_prompt=final_system_prompt,
            user_prompt=prompt,
        )
        response_content = ""
        raw_response = ""
        usage = UsageSummary(ai_called=True)

        try:
            raw_response = self._post_chat_completion(
                base_url=str(model["provider_base_url"]),
                api_key=str(model["provider_api_key"]),
                model=str(model.get("model") or model.get("name") or ""),
                system_prompt=final_system_prompt,
                user_prompt=prompt,
                max_tokens=_max_tokens_for_endpoint(endpoint),
                strict_json=bool(model.get("strict_json", True)),
                user_image_url=user_image_url,
            )
            parsed = json.loads(raw_response)
            choice = (parsed.get("choices") or [{}])[0]
            message_payload = choice.get("message") if isinstance(choice, dict) else {}
            if not isinstance(message_payload, dict):
                message_payload = {}
            response_content = str(message_payload.get("content") or "").strip()
            if not response_content and message_payload.get("reasoning_content"):
                response_content = str(message_payload.get("reasoning_content") or "").strip()
            if not response_content:
                raise ValueError(
                    "AI response content empty; "
                    f"message_preview={_preview_json(message_payload)}; "
                    f"raw_preview={_preview_text(raw_response)}"
                )
            usage_payload = parsed.get("usage") or {}
            usage = UsageSummary(
                ai_called=True,
                input_tokens=int(usage_payload.get("prompt_tokens") or 0),
                output_tokens=int(usage_payload.get("completion_tokens") or 0),
                charged_points=int(usage_payload.get("total_tokens") or 0),
            )
            try:
                content = _extract_json_object(response_content, endpoint=endpoint)
            except (json.JSONDecodeError, ValueError) as parse_exc:
                original_response_content = response_content
                recovered = _recover_decision_from_text(original_response_content, endpoint=endpoint)
                if recovered is not None:
                    logger.warning(
                        "AI decision JSON recovered locally: %s",
                        f"{type(parse_exc).__name__}: {parse_exc}; response_preview={_preview_text(original_response_content)}",
                    )
                    response_preview = _with_indicator_request_preview(
                        _format_response_preview(raw=original_response_content, parsed=recovered),
                        user_payload,
                    )
                    cache_id = self._save_cache_result(
                        cache_key=cache_key,
                        cache_ttl_seconds=cache_ttl_seconds,
                        endpoint=endpoint,
                        provider_id=provider_id,
                        model_id=model_id,
                        content=recovered,
                        usage=usage,
                        response_preview=response_preview,
                    )
                    self._save_usage(
                        deployment,
                        endpoint,
                        provider_id,
                        model_id,
                        usage,
                        request_payload=user_payload,
                        request_snapshot=request_snapshot,
                        success=True,
                        is_custom=is_custom,
                        response_preview=response_preview,
                        cache_id=cache_id,
                    )
                    return AiCallResult(content=recovered, usage=usage)
                try:
                    fixed_response = self._repair_json_response(
                        base_url=str(model["provider_base_url"]),
                        api_key=str(model["provider_api_key"]),
                        model=str(model.get("model") or model.get("name") or ""),
                        endpoint=endpoint,
                        response_content=response_content,
                        strict_json=bool(model.get("strict_json", True)),
                    )
                    response_content = fixed_response
                    content = _extract_json_object(response_content, endpoint=endpoint)
                except Exception as repair_exc:  # noqa: BLE001
                    message = (
                        f"{type(parse_exc).__name__}: {parse_exc}; "
                        f"repair_failed={type(repair_exc).__name__}: {repair_exc}; "
                        f"response_preview={_preview_text(response_content)}"
                    )
                    logger.warning("AI decision JSON repair failed: %s", message)
                    self._save_usage(
                        deployment,
                        endpoint,
                        provider_id,
                        model_id,
                        usage,
                        request_payload=user_payload,
                        request_snapshot=request_snapshot,
                        success=False,
                        is_custom=is_custom,
                        error_message=message[:1000],
                        response_preview=_format_response_preview(raw=original_response_content or response_content),
                    )
                    return AiCallResult(content=_fallback_decision(endpoint, "AI未返回有效JSON，保守观望"), usage=usage)
            response_preview = _with_indicator_request_preview(
                _format_response_preview(raw=response_content, parsed=content),
                user_payload,
            )
            cache_id = self._save_cache_result(
                cache_key=cache_key,
                cache_ttl_seconds=cache_ttl_seconds,
                endpoint=endpoint,
                provider_id=provider_id,
                model_id=model_id,
                content=content,
                usage=usage,
                response_preview=response_preview,
            )
            self._save_usage(
                deployment,
                endpoint,
                provider_id,
                model_id,
                usage,
                request_payload=user_payload,
                request_snapshot=request_snapshot,
                success=True,
                is_custom=is_custom,
                response_preview=response_preview,
                cache_id=cache_id,
            )
            return AiCallResult(content=content, usage=usage)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            response_preview = ""
            if response_content:
                response_preview = _preview_text(response_content, 2000)
                message = f"{message}; response_preview={_preview_text(response_content)}"
            elif raw_response:
                response_preview = _preview_text(raw_response, 2000)
                message = f"{message}; raw_preview={_preview_text(raw_response)}"
            logger.warning("AI decision call failed: %s", message)
            self._save_usage(
                deployment,
                endpoint,
                provider_id,
                model_id,
                usage,
                request_payload=user_payload,
                request_snapshot=request_snapshot,
                success=False,
                is_custom=is_custom,
                error_message=message[:1000],
                response_preview=response_preview,
            )
            return AiCallResult(content=_fallback_decision(endpoint, "AI调用失败，保守观望"), usage=usage)

    def _cache_key(
        self,
        *,
        deployment: dict[str, Any],
        endpoint: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        model: dict[str, Any],
    ) -> str:
        is_custom = bool(model.get("is_custom"))
        scope = "official"
        if is_custom:
            api_key_hash = sha256(str(model.get("provider_api_key") or "").encode("utf-8")).hexdigest()
            scope = f"custom:{deployment.get('user_id', '')}:{api_key_hash}"
        material = {
            "cache_version": 1,
            "scope": scope,
            "endpoint": endpoint,
            "provider_id": str(model.get("provider_id") or ""),
            "provider_base_url": str(model.get("provider_base_url") or "").rstrip("/"),
            "model": str(model.get("model") or model.get("name") or ""),
            "system_prompt": _json_api_system_prompt(
                endpoint,
                system_prompt,
                literal_user_rules=_uses_literal_user_rules(deployment, endpoint),
            ),
            "user_payload": user_payload,
            "temperature": 0,
            "max_tokens": _max_tokens_for_endpoint(endpoint),
            "strict_json": bool(model.get("strict_json", True)),
        }
        canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _cached_result(
        self,
        *,
        deployment: dict[str, Any],
        endpoint: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        model: dict[str, Any],
        cached: dict[str, Any],
    ) -> AiCallResult:
        usage = UsageSummary(
            ai_called=True,
            input_tokens=int(cached.get("input_tokens") or 0),
            output_tokens=int(cached.get("output_tokens") or 0),
            charged_points=int(cached.get("total_tokens") or 0),
        )
        prompt = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        request_snapshot = _format_request_snapshot(
            model=model,
            endpoint=endpoint,
            system_prompt=_json_api_system_prompt(
                endpoint,
                system_prompt,
                literal_user_rules=_uses_literal_user_rules(deployment, endpoint),
            ),
            user_prompt=prompt,
        )
        self._save_usage(
            deployment,
            endpoint,
            str(model["provider_id"]),
            self._usage_model_id(model),
            usage,
            request_payload=user_payload,
            request_snapshot=request_snapshot,
            success=True,
            is_custom=bool(model.get("is_custom")),
            response_preview=str(cached.get("response_preview") or ""),
            provider_called=False,
            response_source="cache",
            cache_id=str(cached.get("id") or ""),
        )
        return AiCallResult(content=dict(cached.get("content") or {}), usage=usage)

    def _save_cache_result(
        self,
        *,
        cache_key: str,
        cache_ttl_seconds: int,
        endpoint: str,
        provider_id: str,
        model_id: str,
        content: dict[str, Any],
        usage: UsageSummary,
        response_preview: str,
    ) -> str:
        if not cache_key:
            return ""
        if endpoint == "open" and bool(content.get("should_open", False)):
            return ""
        if endpoint == "position" and str(content.get("action") or "hold").strip().lower() != "hold":
            return ""
        cached = self.store.save_ai_response_cache(
            {
                "cache_key": cache_key,
                "endpoint": endpoint,
                "provider_id": provider_id,
                "model_id": model_id,
                "content": content,
                "response_preview": response_preview,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.charged_points or usage.input_tokens + usage.output_tokens,
            },
            ttl_seconds=cache_ttl_seconds,
        )
        return str(cached.get("id") or "")

    def _select_model(self, deployment: dict[str, Any], endpoint: str) -> dict[str, Any] | None:
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        prefix = "open" if endpoint in {"open", "compile", "compile_open", "workflow_open"} else "position"
        if str(config.get(f"{prefix}_ai_mode") or "official") == "custom":
            base_url = str(config.get(f"{prefix}_ai_base_url") or config.get(f"{prefix}_ai_provider") or "").strip()
            model_name = str(config.get(f"{prefix}_ai_model") or "").strip()
            api_key = str(config.get(f"{prefix}_ai_key") or "").strip()
            if base_url and model_name and api_key:
                return {
                    "id": f"custom_{prefix}",
                    "provider_id": f"custom_{prefix}",
                    "name": model_name,
                    "model": model_name,
                    "provider_base_url": base_url,
                    "provider_api_key": api_key,
                    "provider_type": "openai_compatible",
                    "strict_json": True,
                    "is_custom": True,
                }
        configured_endpoint_id = str(config.get(f"{prefix}_ai_endpoint_id") or "").strip()
        if configured_endpoint_id:
            configured_endpoint = self.store.get_private_ai_endpoint(configured_endpoint_id)
            if configured_endpoint is not None:
                return configured_endpoint
        official_strategy = self.store.get_official_ai_strategy(str(deployment.get("strategy_code") or ""))
        if official_strategy is not None:
            endpoint_key = "open_ai_endpoint_id" if endpoint == "open" else "position_ai_endpoint_id"
            configured_endpoint_id = str(config.get(f"{prefix}_ai_endpoint_id") or official_strategy.get(endpoint_key) or "").strip()
            if configured_endpoint_id:
                configured_endpoint = self.store.get_private_ai_endpoint(configured_endpoint_id)
                if configured_endpoint is not None:
                    return configured_endpoint
        default_endpoint = self.store.get_default_ai_endpoint()
        if default_endpoint is not None:
            return default_endpoint
        return None

    def _usage_model_id(self, model: dict[str, Any]) -> str:
        if bool(model.get("is_custom")):
            return str(model.get("model") or model.get("name") or model.get("id") or "")
        return str(model.get("id") or model.get("model") or model.get("name") or "")

    def _post_chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        user_image_url: str = "",
        max_tokens: int,
        strict_json: bool = True,
    ) -> str:
        normalized_base_url = base_url.strip().rstrip("/")
        url = normalized_base_url if normalized_base_url.lower().endswith("/chat/completions") else f"{normalized_base_url}/chat/completions"
        user_content: str | list[dict[str, Any]] = user_prompt
        if user_image_url:
            user_content = [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": user_image_url}},
            ]
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if strict_json:
            body["response_format"] = {"type": "json_object"}
        attempts = [body]
        if strict_json:
            compatible_body = dict(body)
            compatible_body.pop("response_format", None)
            attempts.append(compatible_body)
        if user_image_url:
            vision_compatible_body = dict(attempts[-1])
            vision_compatible_body.pop("temperature", None)
            vision_compatible_body.pop("max_tokens", None)
            vision_compatible_body["max_completion_tokens"] = max_tokens
            attempts.append(vision_compatible_body)

            minimal_vision_body = dict(vision_compatible_body)
            minimal_vision_body.pop("max_completion_tokens", None)
            attempts.append(minimal_vision_body)

        compatibility_errors: list[str] = []
        for index, attempt_body in enumerate(attempts):
            payload = json.dumps(attempt_body, ensure_ascii=False).encode("utf-8")
            req = request.Request(
                url,
                data=payload,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with request.urlopen(req, timeout=self.timeout) as response:
                    return response.read().decode("utf-8")
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if index < len(attempts) - 1 and exc.code in {400, 404, 422}:
                    compatibility_errors.append(detail[:500])
                    logger.info(
                        "AI provider rejected request parameters; retrying compatible request: model=%s, url=%s, status=%s, attempt=%s",
                        model,
                        url,
                        exc.code,
                        index + 1,
                    )
                    continue
                suffix = f"; previous compatibility error: {compatibility_errors[0]}" if compatibility_errors else ""
                raise RuntimeError(f"AI provider HTTP {exc.code}: {detail[:500]}; url={url}{suffix}") from exc
            except TimeoutError as exc:
                raise TimeoutError(f"AI provider timeout after {self.timeout:g}s: model={model}, url={url}") from exc
            except SocketTimeout as exc:
                raise TimeoutError(f"AI provider timeout after {self.timeout:g}s: model={model}, url={url}") from exc
            except error.URLError as exc:
                raise RuntimeError(f"AI provider connection failed: {exc.reason}; model={model}, url={url}") from exc

        raise RuntimeError(f"AI provider request failed: model={model}, url={url}")

    def _repair_json_response(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        endpoint: str,
        response_content: str,
        strict_json: bool = True,
    ) -> str:
        schema = (
            '{"should_open":false,"direction":null,"confidence":0,'
            '"lot":0,"sl_distance_price":0,"tp_distance_price":0,'
            '"reason":"具体原因","analysis":"详细中文行情和风控说明"}'
            if endpoint == "open"
            else '{"action":"hold","ticket":null,"direction":null,"confidence":0,'
            '"lot":0,"sl":null,"tp":null,'
            '"reason":"具体原因","analysis":"详细中文持仓和风控说明"}'
        )
        raw_response = self._post_chat_completion(
            base_url=base_url,
            api_key=api_key,
            model=model,
            system_prompt=_json_api_system_prompt(
                endpoint,
                (
                    "把下面这段模型返回内容修复成一个合法 JSON 对象。"
                    "只返回 JSON，不要解释。"
                    "如果内容里没有可恢复的明确交易结论，返回安全的不开仓或继续持有对象。"
                    f"必须使用这个结构: {schema}"
                ),
                literal_user_rules=True,
            ),
            user_prompt=response_content[:6000],
            max_tokens=700,
            strict_json=strict_json,
        )
        parsed = json.loads(raw_response)
        choice = (parsed.get("choices") or [{}])[0]
        message_payload = choice.get("message") if isinstance(choice, dict) else {}
        if not isinstance(message_payload, dict):
            return raw_response
        content = str(message_payload.get("content") or "").strip()
        if not content and message_payload.get("reasoning_content"):
            content = str(message_payload.get("reasoning_content") or "").strip()
        if not content:
            raise ValueError(f"AI repair response content empty; raw_preview={_preview_text(raw_response)}")
        return content

    def _save_usage(
        self,
        deployment: dict[str, Any],
        endpoint: str,
        provider_id: str,
        model_id: str,
        usage: UsageSummary,
        *,
        request_payload: dict[str, Any] | None = None,
        request_snapshot: str = "",
        success: bool,
        is_custom: bool = False,
        error_message: str = "",
        response_preview: str = "",
        provider_called: bool = True,
        response_source: str = "",
        cache_id: str = "",
    ) -> None:
        request_payload = request_payload or {}
        account = request_payload.get("account") if isinstance(request_payload.get("account"), dict) else {}
        self.store.save_ai_usage_log(
            {
                "user_id": deployment.get("user_id", ""),
                "deployment_id": deployment.get("id", ""),
                "strategy_code": deployment.get("strategy_code", ""),
                "endpoint": endpoint,
                "provider_id": provider_id,
                "model_id": model_id,
                "account_login": str(account.get("login") or ""),
                "account_server": str(account.get("server") or ""),
                "symbol": str(request_payload.get("symbol") or "").upper(),
                "timeframe": str(request_payload.get("timeframe") or "").upper(),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.charged_points or usage.input_tokens + usage.output_tokens,
                "official_tokens": 0 if is_custom else usage.charged_points or usage.input_tokens + usage.output_tokens,
                "custom_tokens": usage.charged_points or usage.input_tokens + usage.output_tokens if is_custom else 0,
                "billing_source": "custom" if is_custom else "official",
                "input_price_snapshot": 0 if is_custom else model_price(provider_id, model_id, "input", self.store),
                "output_price_snapshot": 0 if is_custom else model_price(provider_id, model_id, "output", self.store),
                "success": success,
                "provider_called": provider_called,
                "response_source": response_source or ("provider" if success else "fallback"),
                "cache_id": cache_id,
                "error_message": error_message,
                "request_snapshot": request_snapshot,
                "response_preview": response_preview,
            },
        )


def model_price(provider_id: str, model_id: str, price_type: str, store: SqliteStore) -> str:
    endpoint = store.get_private_ai_endpoint(model_id) or store.get_private_ai_endpoint(provider_id)
    if endpoint is None:
        return "0"
    field = "input_price_per_million" if price_type == "input" else "output_price_per_million"
    return str(endpoint.get(field) or "0")


def _pa_system_prompt() -> str:
    return (
        "You are GainLab PA Agent trading decision API. Return only one compact JSON object. "
        "First character must be { and last character must be }. "
        "Do not output thoughts, reasoning process, markdown, code fences, prefixes, suffixes, or prose outside JSON. "
        "Analyze silently. Put the final useful explanation inside the JSON fields reason and analysis. "
        "If uncertain, weak, conflicting, or unsafe, choose no-open or hold. "
        "Open endpoint keys: should_open, direction, confidence, lot, sl_distance_price, tp_distance_price, reason, analysis. "
        "Position endpoint keys: action, ticket, direction, confidence, lot, sl, tp, reason, analysis. "
        "reason must be Chinese, concrete, <=60 Chinese characters. "
        "analysis must be Chinese, 120-300 Chinese characters, with market structure, setup score, price/risk context, and action rationale. "
        "Never invent prices. Use price distances for open SL/TP."
    )


def _workflow_stage_candidate(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    stage = content.get("stage")
    return dict(stage) if isinstance(stage, dict) else dict(content)


def _normalize_generated_workflow_stage(
    content: dict[str, Any],
    *,
    stage: str,
    data_requirements: dict[str, Any],
    user_logic: str = "",
) -> dict[str, Any]:
    """Normalize common provider aliases without changing the user's strategy semantics."""
    value = deepcopy(content)
    value["data_requirements"] = {
        "data_type": str(data_requirements.get("data_type") or "kline"),
        "kline_count": int(data_requirements.get("kline_count") or 100),
        "call_mode": str(data_requirements.get("call_mode") or "bar"),
        "call_value": float(data_requirements.get("call_value") or 1),
    }

    def normalize_operand(operand: Any) -> None:
        if not isinstance(operand, dict):
            return
        if str(operand.get("kind") or "") == "indicator":
            indicator = str(operand.get("indicator") or operand.get("name") or "").strip().lower()
            if indicator:
                operand["indicator"] = indicator
                params = operand.get("params") if isinstance(operand.get("params"), dict) else {}
                length = params.get("length")
                if not str(operand.get("alias") or "").strip():
                    operand["alias"] = f"{indicator}{length}" if length not in (None, "") else indicator
                operand.setdefault("source", "close")
                operand.setdefault("component", "value")

    def normalize_condition(condition: Any) -> None:
        if not isinstance(condition, dict):
            return
        normalize_operand(condition.get("left"))
        normalize_operand(condition.get("right"))
        direction = str(condition.get("direction") or "").strip().lower()
        if condition.get("kind") == "cross" and direction in {"up", "bullish"}:
            condition["direction"] = "above"
        elif condition.get("kind") == "cross" and direction in {"down", "bearish"}:
            condition["direction"] = "below"
        elif condition.get("kind") == "cross" and not direction:
            operator = str(condition.get("operator") or "").strip().lower()
            if operator in {"gt", "gte"}:
                condition["direction"] = "above"
            elif operator in {"lt", "lte"}:
                condition["direction"] = "below"
        for child in condition.get("conditions") if isinstance(condition.get("conditions"), list) else []:
            normalize_condition(child)

    cross_window_match = re.search(r"最近\s*(\d+)\s*根", user_logic, flags=re.IGNORECASE)
    cross_window = int(cross_window_match.group(1)) if cross_window_match else 0
    latest_cross = bool(re.search(r"(?:时间)?最近(?:的)?一次交叉|最新(?:的)?交叉|latest\s+cross", user_logic, flags=re.IGNORECASE))
    nodes = value.get("nodes") if isinstance(value.get("nodes"), list) else []
    nested_edges: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("position") is None:
            node.pop("position", None)
        if isinstance(node.get("edges"), list):
            nested_edges.extend(item for item in node.pop("edges") if isinstance(item, dict))
        if node.get("type") == "entry":
            node["stage"] = stage
        if node.get("type") == "condition":
            condition = node.get("condition")
            normalize_condition(condition)
            if isinstance(condition, dict) and condition.get("kind") == "cross":
                if cross_window >= 2:
                    condition["lookback"] = cross_window
                if latest_cross:
                    condition["cross_mode"] = "latest"
            if isinstance(condition, dict) and condition.get("kind") == "consecutive" and (
                condition.get("operator") is None or not isinstance(condition.get("right"), dict)
            ):
                node.pop("condition", None)
                node["type"] = "ai_condition"
                node["instruction"] = str(condition.get("description") or node.get("label") or "按用户规则判断")
                node["data_type"] = str(data_requirements.get("data_type") or "kline")
        if node.get("type") == "action" and isinstance(node.get("action"), dict):
            action = node["action"]
            for key in ("volume", "target", "stop_loss", "take_profit"):
                if action.get(key) is None:
                    action.pop(key, None)
            if action.get("kind") in {"open_buy", "open_sell"} and action.get("entry_mode") == "market":
                action["entry_price_rule"] = ""
            explicit_stop = _explicit_recent_extreme_stop_rule(
                user_logic,
                action_kind=str(action.get("kind") or ""),
                action_description=str(action.get("description") or node.get("label") or ""),
            )
            if explicit_stop:
                action["stop_loss_rule"] = explicit_stop
    cross_nodes = [
        node for node in nodes
        if node.get("type") == "condition"
        and isinstance(node.get("condition"), dict)
        and node["condition"].get("kind") == "cross"
        and node["condition"].get("cross_mode") == "latest"
    ]
    redundant_node_ids: set[str] = set()
    if latest_cross and len(cross_nodes) >= 2:
        for node in nodes:
            condition = node.get("condition") if isinstance(node.get("condition"), dict) else {}
            operands = [condition.get("left"), condition.get("right")]
            references_conditions = any(
                isinstance(operand, dict) and operand.get("kind") == "condition" for operand in operands
            )
            text = f"{node.get('label') or ''} {condition.get('description') or ''}"
            if node.get("type") == "condition" and references_conditions and re.search(
                r"最近|最新|交叉|latest", text, flags=re.IGNORECASE,
            ):
                redundant_node_ids.add(str(node.get("id") or ""))
    if redundant_node_ids:
        nodes = [node for node in nodes if str(node.get("id") or "") not in redundant_node_ids]
        value["nodes"] = nodes
    edges = value.get("edges") if isinstance(value.get("edges"), list) else []
    if redundant_node_ids:
        edges = [
            edge for edge in edges
            if str(edge.get("source") or "") not in redundant_node_ids
            and str(edge.get("target") or "") not in redundant_node_ids
        ]
    edge_ids = {str(item.get("id") or "") for item in edges if isinstance(item, dict)}
    for edge in nested_edges:
        edge_id = str(edge.get("id") or "")
        if edge_id and edge_id not in edge_ids:
            edges.append(edge)
            edge_ids.add(edge_id)
    value["edges"] = edges
    _connect_generated_flat_branches(value)
    return value


def _connect_generated_flat_branches(stage: dict[str, Any]) -> None:
    """Connect flat if/else rules that share one fallback action into a decision chain."""
    nodes = [item for item in stage.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in stage.get("edges", []) if isinstance(item, dict)]
    entry_id = str(stage.get("entry_node_id") or "")
    condition_ids = [str(node.get("id") or "") for node in nodes if node.get("type") in {"condition", "ai_condition"}]

    def reachable_ids() -> set[str]:
        outgoing: dict[str, list[str]] = {}
        for edge in edges:
            outgoing.setdefault(str(edge.get("source") or ""), []).append(str(edge.get("target") or ""))
        reached: set[str] = set()
        queue = [entry_id]
        while queue:
            current = queue.pop(0)
            if not current or current in reached:
                continue
            reached.add(current)
            queue.extend(outgoing.get(current, []))
        return reached

    for _ in range(len(condition_ids)):
        reachable = reachable_ids()
        disconnected = [node_id for node_id in condition_ids if node_id not in reachable]
        if not disconnected:
            return
        attached = False
        for target_condition in disconnected:
            target_no = next((edge for edge in edges if edge.get("source") == target_condition and edge.get("source_handle") == "no"), None)
            if target_no is None:
                continue
            shared_fallback = target_no.get("target")
            predecessor = next((
                edge for edge in edges
                if edge.get("source") in reachable
                and edge.get("source") in condition_ids
                and edge.get("source_handle") == "no"
                and edge.get("target") == shared_fallback
            ), None)
            if predecessor is None:
                continue
            predecessor["target"] = target_condition
            attached = True
            break
        if not attached:
            return


def _explicit_recent_extreme_stop_rule(
    user_logic: str,
    *,
    action_kind: str,
    action_description: str = "",
) -> str:
    if action_kind not in {"open_buy", "open_sell"}:
        return ""
    direction_terms = ("开多", "做多", "buy", "long") if action_kind == "open_buy" else ("开空", "做空", "sell", "short")
    sentences = [item.strip() for item in re.split(r"[。；;\n]+", str(user_logic or "")) if item.strip()]
    candidates = [
        sentence for sentence in sentences
        if any(term in sentence.lower() for term in direction_terms)
    ]
    if action_description:
        candidates.append(action_description)
    for text in candidates:
        compact = re.sub(r"\s+", "", text.lower())
        # A clause may contain another lookback first, for example
        # "最近3根内出现下穿，止损设在最近5根最高价".  Only parse an
        # explicit extreme expression near the stop-loss clause; a broad
        # wildcard would incorrectly turn the first lookback into the stop.
        stop_positions = [compact.rfind(marker) for marker in ("止损", "stoploss", "stop-loss")]
        stop_position = max(stop_positions)
        search_areas = [compact[stop_position:]] if stop_position >= 0 else []
        search_areas.append(compact)
        for area in search_areas:
            match = re.search(
                r"最近(\d+)根(?:(?:已收盘)?(?:k线|蜡烛|柱)?(?:内|以内|之内|范围内|区间内|区间|中|的)*)?(最低|最高)价?",
                area,
            )
            if match:
                function = "recent_low" if match.group(2) == "最低" else "recent_high"
                return f"{function}({int(match.group(1))})"
            english = re.search(
                r"(?:last|recent)(\d+)(?:closed)?(?:bars?|candles?)(?:range)?(?:'s)?(lowest|highest)",
                area,
            )
            if english:
                function = "recent_low" if english.group(2) == "lowest" else "recent_high"
                return f"{function}({int(english.group(1))})"
    return ""


def _workflow_stage_compact_contract(stage: str) -> dict[str, Any]:
    return {
        "root": {"entry_node_id": "entry id", "data_requirements": "copy supplied value", "nodes": "array", "edges": "array"},
        "node_types": {
            "entry": {"id": "ASCII id", "type": "entry", "stage": stage, "label": "Chinese label"},
            "condition": {"id": "ASCII id", "type": "condition", "label": "Chinese label", "condition": "valid condition object"},
            "ai_condition": {"id": "ASCII id", "type": "ai_condition", "label": "Chinese label", "instruction": "exact open rule", "data_type": "kline|screenshot|both"},
            "action": {"id": "ASCII id", "type": "action", "label": "Chinese label", "action": {"kind": "allowed stage action"}},
        },
        "edges": {"fields": ["id", "source", "target", "source_handle"], "handles": "entry:next; condition/ai_condition:yes and no"},
        "graph_rules": [
            "entry has exactly one next edge",
            "every condition has exactly one yes and one no edge",
            "every node is reachable from entry",
            "every branch ends at an action",
            "action nodes have no outgoing edge",
        ],
    }


def _workflow_source_rules(user_logic: str) -> list[str]:
    """Split author text into stable rules while keeping each rule's wording intact."""
    parts = re.split(r"(?:\r?\n)+|[。；;]+", str(user_logic or ""))
    rules: list[str] = []
    for part in parts:
        text = re.sub(r"^\s*(?:[-*•]+|\d+[.、)])\s*", "", part).strip()
        if text:
            rules.append(text)
    return rules


def _classify_workflow_source_rule(source_rule: str, *, stage: str) -> str:
    """Classify only explicit action verbs; position direction is not itself an action."""
    text = re.sub(r"\s+", "", str(source_rule or "")).lower()
    if stage == "open":
        if any(term in text for term in ("开多", "做多", "buy", "long")):
            return "open_buy"
        if any(term in text for term in ("开空", "做空", "sell", "short")):
            return "open_sell"
        if any(term in text for term in ("不开仓", "不操作", "noaction")):
            return "no_action"
        return ""

    if any(term in text for term in ("取消挂单", "撤销挂单", "撤单", "cancelpending")):
        return "cancel_pending"
    if any(term in text for term in ("部分平仓", "平仓一半", "关闭一半", "半仓", "partialclose")):
        return "close_partial"
    if any(term in text for term in ("全部平仓", "立即平仓", "平仓", "closeall", "closeposition")):
        return "close_all"
    if any(term in text for term in ("移动止损", "修改止损", "调整止损", "止损移动", "止损设", "stoploss")):
        return "modify_sl"
    if any(term in text for term in ("移动止盈", "修改止盈", "调整止盈", "止盈移动", "止盈设", "takeprofit")):
        return "modify_tp"
    if any(term in text for term in ("加多仓", "多单加仓", "加仓做多", "addbuy", "addlong")):
        return "add_buy"
    if any(term in text for term in ("加空仓", "空单加仓", "加仓做空", "addsell", "addshort")):
        return "add_sell"
    if "加仓" in text:
        if any(term in text for term in ("空单", "空头", "做空")):
            return "add_sell"
        if any(term in text for term in ("多单", "多头", "做多")):
            return "add_buy"
    if any(term in text for term in ("保持持仓", "继续持有", "不操作", "hold")):
        return "hold"
    return ""


def _workflow_rule_label(source_rule: str, action_kind: str) -> str:
    action_titles = {
        "open_buy": "开多",
        "open_sell": "开空",
        "no_action": "不开仓",
        "close_all": "全部平仓",
        "close_partial": "部分平仓",
        "add_buy": "加多仓",
        "add_sell": "加空仓",
        "modify_sl": "修改止损",
        "modify_tp": "修改止盈",
        "cancel_pending": "取消挂单",
        "hold": "保持持仓",
    }
    prefix = str(source_rule or "").strip()[:72]
    title = action_titles.get(action_kind, "执行动作")
    return f"{prefix} → {title}"[:100]


def _workflow_stage_from_classified_rules(
    content: Any,
    *,
    stage: str,
    data_requirements: dict[str, Any],
    source_rules: list[str],
) -> dict[str, Any]:
    """Render a valid decision chain from AI action classifications and exact source text."""
    allowed = (
        {"open_buy", "open_sell", "no_action"}
        if stage == "open"
        else {"close_all", "close_partial", "add_buy", "add_sell", "modify_sl", "modify_tp", "cancel_pending", "hold"}
    )
    raw_rules = content.get("rules") if isinstance(content, dict) else None
    classified: dict[int, dict[str, Any]] = {}
    for item in raw_rules if isinstance(raw_rules, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("rule_index") or 0)
        except (TypeError, ValueError):
            continue
        kind = str(item.get("action_kind") or item.get("kind") or "").strip().lower()
        if 1 <= index <= len(source_rules) and kind in allowed:
            classified[index] = {**item, "action_kind": kind}

    entry_id = f"{stage}_entry"
    fallback_kind = "no_action" if stage == "open" else "hold"
    fallback_id = f"{stage}_fallback"
    nodes: list[dict[str, Any]] = [
        {"id": entry_id, "type": "entry", "stage": stage, "label": "开仓数据入口" if stage == "open" else "持仓风控入口"},
    ]
    edges: list[dict[str, Any]] = []
    decision_ids: list[str] = []
    for index in sorted(classified):
        item = classified[index]
        source_text = source_rules[index - 1]
        kind = str(item["action_kind"])
        # Reject classifications that introduce an action absent from the
        # authoritative source sentence.
        normalized = source_text.lower().replace(" ", "")
        action_markers = {
            "modify_sl": ("止损", "stoploss", "stop-loss"),
            "modify_tp": ("止盈", "takeprofit", "take-profit"),
            "close_all": ("平仓", "close"),
            "close_partial": ("部分平仓", "平仓一半", "closepartial", "partialclose"),
            "add_buy": ("加仓", "加多", "add"),
            "add_sell": ("加仓", "加空", "add"),
            "cancel_pending": ("取消挂单", "撤单", "cancel"),
        }
        markers = action_markers.get(kind)
        if markers and not any(marker in normalized for marker in markers):
            continue

        condition_id = f"{stage}_rule_{index}"
        action_id = f"{stage}_action_{index}"
        label = str(item.get("label") or source_text)[:100]
        action: dict[str, Any] = {"kind": kind, "description": source_text[:500]}
        if kind in {"open_buy", "open_sell"}:
            action["entry_mode"] = "market"
        elif kind == "modify_sl":
            action["stop_loss_rule"] = source_text
        elif kind == "modify_tp":
            action["take_profit_rule"] = source_text
        elif kind in {"add_buy", "add_sell"}:
            action["volume"] = {"mode": "open_sizing", "value": 1}
        elif kind == "close_partial":
            # The classifier must not guess a partial-close size. Keep an
            # unclassified rule out of the graph when no explicit ratio exists.
            ratio = re.search(r"(?:平仓|关闭)\s*(\d+(?:\.\d+)?)\s*%", source_text)
            half = bool(re.search(r"(?:一半|半仓)", source_text))
            if ratio:
                action["volume"] = {"mode": "current_ratio", "value": float(ratio.group(1)) / 100}
            elif half:
                action["volume"] = {"mode": "current_ratio", "value": 0.5}
            else:
                continue
        nodes.extend([
            {
                "id": condition_id,
                "type": "ai_condition",
                "label": label,
                "instruction": source_text,
                "data_type": str(data_requirements.get("data_type") or "kline"),
            },
            {"id": action_id, "type": "action", "label": label, "action": action},
        ])
        decision_ids.append(condition_id)
        edges.append({
            "id": f"{condition_id}_yes",
            "source": condition_id,
            "target": action_id,
            "source_handle": "yes",
        })

    nodes.append({"id": fallback_id, "type": "action", "label": "不操作", "action": {"kind": fallback_kind}})
    if not decision_ids:
        return {}
    edges.append({"id": f"{entry_id}_next", "source": entry_id, "target": decision_ids[0], "source_handle": "next"})
    for offset, condition_id in enumerate(decision_ids):
        target = decision_ids[offset + 1] if offset + 1 < len(decision_ids) else fallback_id
        edges.append({
            "id": f"{condition_id}_no",
            "source": condition_id,
            "target": target,
            "source_handle": "no",
        })
    return {
        "entry_node_id": entry_id,
        "data_requirements": {
            "data_type": str(data_requirements.get("data_type") or "kline"),
            "kline_count": int(data_requirements.get("kline_count") or 100),
            "call_mode": str(data_requirements.get("call_mode") or "bar"),
            "call_value": float(data_requirements.get("call_value") or 1),
        },
        "nodes": nodes,
        "edges": edges,
    }


def _custom_workflow_rule_classification_prompt(stage: str) -> str:
    allowed = "open_buy, open_sell, no_action" if stage == "open" else (
        "close_all, close_partial, add_buy, add_sell, modify_sl, modify_tp, cancel_pending, hold"
    )
    return (
        "Classify each supplied trading rule by its explicit resulting action. Return only one compact JSON object "
        "with shape {\"rules\":[{\"rule_index\":1,\"label\":\"short Chinese label\","
        "\"action_kind\":\"allowed kind\"}]}. Include every source rule exactly once and preserve source order. "
        "rule_index must be copied exactly; never merge, split, rewrite or add a rule. Classify only the explicit action, "
        "not the condition. A full close is close_all, partial close is close_partial, moving/changing stop loss is "
        "modify_sl, changing take profit is modify_tp, adding long/short is add_buy/add_sell, cancelling a pending order "
        "is cancel_pending, and an explicit no-operation is hold/no_action according to stage. Never infer an action that "
        f"the source text does not state. Allowed action kinds for this stage: {allowed}."
    )


def _custom_workflow_stage_generation_prompt(stage: str, *, repair: bool = False) -> str:
    stage_title = "开仓" if stage == "open" else "持仓风控"
    allowed_actions = "open_buy, open_sell, no_action" if stage == "open" else (
        "close_all, close_partial, add_buy, add_sell, modify_sl, modify_tp, cancel_pending, hold"
    )
    contract_rule = (
        "The previous candidate, compact_contract and validation_errors are supplied. Correct every listed error, preserve all "
        "user rules and valid fields, and return a complete replacement stage matching compact_contract. "
        if repair else "Follow workflow_stage_schema exactly. "
    )
    position_rule = ""
    if stage == "position":
        position_rule = (
            "Treat each independently stated position-management rule as one complete decision and preserve its full "
            "meaning. If a trigger combines position direction/state with another condition, or contains arithmetic "
            "between current price, open price, current stop, profit or ATR, represent the complete trigger as one "
            "ai_condition and copy the user's corresponding sentence into instruction without rewriting it. Do not split "
            "such a trigger into partial structured conditions. A plain indicator crossover may use a structured cross "
            "condition only when doing so preserves every qualifier in that rule; for example, a long-only or short-only "
            "crossover must not lose the position-direction qualifier. Never introduce an indicator into an ATR/price "
            "condition unless that indicator occurs in the same user rule. For modify_sl put the requested new-stop "
            "formula in stop_loss_rule; for modify_tp put it in take_profit_rule. Never create modify_tp when the user did "
            "not mention take profit, and never create modify_sl when the user did not mention stop loss. "
        )
    return (
        f"Convert the user's {stage_title} rules into exactly one visual WorkflowStage JSON object. "
        f"Output only {{\"stage\":{{...}}}}. {contract_rule}Never output markdown or explanations. "
        "The user's text is the sole source of trading logic. Preserve every explicit condition, order, direction, "
        "priority, price rule, stop-loss rule, take-profit rule and volume rule. Never add optimization, confirmation, "
        "trend, risk, scoring or safety conditions. Create exactly one entry node with stage matching the requested stage. "
        "Every reachable branch must end at an action node and action nodes must have no outgoing edges. Condition and "
        "ai_condition nodes require both yes and no edges; entry and vision_extract require one next edge. Use unique ASCII "
        "IDs beginning with a letter. Use concise Chinese node labels. Use structured condition nodes whenever the supplied "
        "condition can be represented by the schema and available indicator capabilities. Preserve indicator name, component, "
        "source, period and comparison direction exactly. For cross conditions use cross_mode latest when the user says the "
        "most recent/latest crossing takes priority; otherwise use cross_mode any. Preserve the user's cross lookback window. "
        "Use ai_condition only for an open semantic condition that cannot be "
        "represented structurally. For screenshot rules, create vision_extract with user-defined enum outputs and then compare "
        "its result using a condition node; fallback must be one enum option such as uncertain. Set data_requirements according "
        "to the supplied selection and do not silently change it. Open price, stop-loss and take-profit descriptions belong in "
        "entry_price_rule, stop_loss_rule and take_profit_rule. A market order uses entry_mode market; a pending order uses "
        "entry_mode pending and must have entry_price_rule. When the user's rule does not trigger, end in no_action for opening "
        "or hold for position management. Do not invent partial-close volume or add volume. "
        f"{position_rule}"
        f"Only these actions are allowed in this stage: {allowed_actions}."
    )


def _custom_strategy_stage_compile_prompt(stage: str) -> str:
    common = (
        "You compile one stage of a user's natural-language trading strategy into a reusable prompt template. "
        "Analyze only the supplied stage. The user's original rule is the sole source of trading logic. "
        "Preserve every condition exactly and do not add, optimize, recommend or infer any condition the user did not write. "
        "Do not add trend, confirmation, score, market-structure, volatility, risk-reward or safety filters. "
        "Extract every explicitly referenced built-in indicator and period. Candlestick sequences, engulfing, pin bars, "
        "support, resistance, recent highs and recent lows are inferred directly from OHLCV and are not indicators. "
        "The template must apply the rule strictly to supplied closed candles and calculated indicator arrays. "
        "Also produce rule_plan for deterministic execution when every condition and action in this stage can be represented "
        "by the safe expression language below. rule_plan is {version:1,mode:'deterministic',rules:[...]}; each rule is "
        "{when:'boolean expression',action:{...},description:'short Chinese rule'}. Preserve the user's rule order. "
        "Allowed variables: bid, ask, side, open_price, current_price, sl, tp, volume, profit, favorable_move and indicator "
        "aliases such as ema5, ema30 and atr14. Allowed functions: latest_cross('ema5','ema30',3), "
        "cross_above('ema5','ema30',3), cross_below('ema5','ema30',3), indicator('ema5',-1), lowest_low(5), "
        "highest_high(5), consecutive('up',10), pattern('bullish_engulfing',3), pattern('bearish_engulfing',3), "
        "pattern('bullish_pinbar',3), pattern('bearish_pinbar',3), pattern('doji',3), min, max and abs. "
        "Expressions may use and/or/not, parentheses, arithmetic and comparisons. String constants BUY and SELL must be quoted. "
        "latest_cross returns 1 for the most recent upward cross, -1 for downward cross and 0 for none within the window. "
        "Use exactly the listed variable names; use profit, never current_profit or another synonym. Every position rule that "
        "mentions a long/BUY position must include side == 'BUY'; every rule that mentions a short/SELL position must include "
        "side == 'SELL'. A crossover needs at least two values: use window 2 for only the latest crossover, never window 1. "
        "Open-stage conditions cannot use side or position fields because no position exists before opening. Every open rule's "
        "when expression must contain the actual user trigger; never replace an EMA crossover with a BUY/SELL side comparison. "
        "Open action shape: {type:'open',direction:'buy|sell',sl:'expression or null',tp:'expression or null'}. "
        "Position modify action is {type:'modify',sl:'expression or null',tp:'expression or null',"
        "sl_constraint:'not_below_current|not_above_current|null'}. If the user says a new stop cannot be below the old "
        "stop, use not_below_current; if it cannot be above the old stop, use not_above_current. "
        "All absent optional values must be JSON null without quotation marks, never the strings 'null' or 'none'. "
        "{type:'close',close_scope:'full|partial',volume:'expression or null'}, or "
        "{type:'add',direction:'buy|sell',lot:'expression or null',sl:'expression or null',tp:'expression or null'}. "
        "If any condition is screenshot-based, return rule_plan {version:1,mode:'ai',rules:[]} and list each screenshot "
        "condition in visual_conditions. If a condition cannot be handled exactly by either the safe expression language "
        "or the supplied screenshot, return AI mode and list it in unsupported_conditions instead of approximating it. "
    )
    if stage == "open":
        return common + (
            "Preserve entry direction, trigger, stop-loss and take-profit semantics. Return Chinese summary, template and warnings."
        )
    return common + (
        "Preserve hold, close, add, partial-close and stop modification semantics. Preserve an explicit add-lot formula; "
        "when add has no sizing formula, declare that server opening sizing is used. Never invent a partial-close amount. "
        "Preserve temporal and staged semantics expressed by words such as first, once, then, after, thereafter, already "
        "and not-yet. Convert sequential rules into explicit stage conditions: once the observable current position state "
        "shows an earlier stage is completed, that stage must not execute again only when this follows from the user's "
        "wording. Never flatten a user-defined sequence into independent conditions that remain simultaneously eligible. "
        "Preserve exactly any user-defined action priority and stop direction. Do not invent a default priority, a rule that "
        "stops may only tighten, or any indicator, threshold, stage, trigger or risk rule. If simultaneous rules conflict "
        "and give no priority, report that ambiguity in warnings instead of silently choosing a policy. "
        "Write the template as clear Chinese instructions, not executable code or pseudocode. Follow runtime_data_contract "
        "exactly. warnings must contain only user-actionable missing, ambiguous, unsupported or conflicting rules; never "
        "put implementation notes, default data sources, array indexes or normal calculation conventions in warnings. "
        "Return Chinese summary, template and warnings."
    )


def _custom_runtime_prompt(endpoint: str) -> str:
    if endpoint == "open":
        return (
            "You execute a user-defined trading strategy. Apply the supplied user_rule and prompt_template "
            "strictly and literally to closed OHLCV candles and indicator arrays. user_rule is the sole authoritative "
            "source of trading logic; prompt_template may only clarify it and must never extend or override it. Use only "
            "conditions explicitly written by the user. Never add confirmation, breakout, trend, volatility, "
            "market-structure, score, risk-reward, safety or subjective suitability filters. Do not evaluate whether the "
            "strategy is sensible; only determine whether the user's stated conditions are satisfied. "
            "If all explicit user conditions are satisfied, should_open must be true even if you personally consider "
            "the setup weak or risky. Evaluate crossovers directly from supplied indicator arrays over exactly the number "
            "of closed candles requested by the user, and evaluate candlestick patterns directly from OHLCV. Do not reduce "
            "a multi-candle rule to only the latest two values and do not require an additional price breakout or trend "
            "confirmation. A stop-loss formula based on recent candle "
            "highs/lows, ATR, indicators or another supplied value is a complete stop definition even when it contains "
            "no literal price. When opening, calculate that formula and return the resulting absolute sl price; never "
            "return sl as null merely because the user did not write a numeric price. Candles are oldest to newest and the last "
            "item is the latest closed candle. Do not invent missing facts or prices. In analysis, summarize every explicit "
            "opening condition in short natural Chinese. State whether the requested crossover or pattern occurred within "
            "the requested candle window, or identify the exact condition that failed. If the rule defines a stop or target, "
            "say that it was set according to that rule without quoting its calculated price or calculation process. "
            "If every requested condition is not clearly satisfied, do not open. "
            "When screenshot metadata and an image are attached, the image is an authoritative input for every screenshot-"
            "dependent condition in user_rule, prompt_template and visual_conditions. Evaluate those conditions directly "
            "from the image; never search for a custom "
            "visual indicator such as HG in indicators.values and never report its numeric array as missing. Combine only "
            "the observed visual result with the supplied user rule; do not invent visual conditions the user did not request. "
            "Return absolute sl and tp prices when the rule defines them."
        )
    return (
        "You execute the position-management part of a user-defined trading strategy. Apply only the supplied "
        "user_rule and prompt_template to the closed candles, indicators and current positions. user_rule is the sole "
        "authoritative source of trading logic; prompt_template may only clarify it and must never extend or override it. "
        "Do not add trend, confirmation, market-structure, score, risk-reward or safety conditions. Do not evaluate or "
        "optimize the strategy. If no explicit "
        "risk condition is met, hold. Use only hold, close, add or modify. Never close or add without a clear rule. "
        "Indicator arrays are in indicators.values keyed by aliases such as atr14 and ema5, oldest to newest; [-1] "
        "is the latest closed candle. Evaluate crossovers over exactly the user-requested candle window directly from "
        "those arrays, and infer requested candlestick patterns directly from OHLCV. In analysis, summarize each "
        "applicable position rule in short natural Chinese, identify the current user-defined stage when relevant, and "
        "state exactly why the selected action is hold, close, add or modify. Do not quote raw market values, action prices, "
        "full arrays, formulas or intermediate calculation steps. "
        "ATR is a price distance, while position.profit is account-currency P/L: never "
        "compare them directly. For ATR-based favorable movement use BUY current_price-open_price and SELL "
        "open_price-current_price. "
        "Preserve the sequence and priority explicitly written by the user. Use observable current position fields to "
        "determine whether a user-defined prior stage has completed. Apply a one-way stop restriction only if the user "
        "explicitly wrote one; otherwise a requested stop may tighten or loosen according to the user's rule. If multiple "
        "user rules conflict and provide no priority, hold and clearly report the ambiguity rather than inventing a policy. "
        "The structured sl value must exactly match the final stop calculated from the user's rule. "
        "When screenshot metadata and an image are attached, the image is an authoritative input for every screenshot-"
        "dependent condition in user_rule, prompt_template and visual_conditions. Evaluate those conditions directly "
        "from the image; never search for a custom visual "
        "indicator such as HG in indicators.values and never report its numeric array as missing. Do not invent visual "
        "filters or override exact supplied indicator facts. "
        "These instructions must not create a new trigger, filter, stage, priority or risk condition. "
        "For add, return lot calculated strictly from an explicit user formula; if the user gave no lot formula, return "
        "lot as null and let the server use opening sizing. For close, return close_scope as full or partial. A partial "
        "close must include a positive volume calculated from the target position; otherwise hold instead of guessing."
    )


def _custom_rule_explanation_prompt(endpoint: str) -> str:
    action_key = "should_open, direction, sl and tp" if endpoint == "open" else "action and all supplied action fields"
    return (
        "You explain a deterministic user-defined strategy result in short natural Chinese. The calculated_result was "
        "produced by the server from the user's confirmed rules and is authoritative. Copy its "
        f"{action_key} into the required JSON shape and never change, recalculate, challenge or add conditions to it. "
        "Write a concise explanation of which named rule passed or failed and the resulting action. Do not quote raw "
        "prices, indicator arrays, formulas, intermediate calculations, token counts or implementation details. "
        "Do not add market structure, trend, score, confirmation, risk-reward or advice unless the user's rule contains it."
    )


def _custom_strategy_fallback(open_logic: str, position_logic: str) -> dict[str, Any]:
    return {
        "summary": "根据用户自然语言规则，由 AI 结合已收盘 K 线和所需指标执行开仓与持仓风控判断。",
        "open_prompt_template": (
            "严格按用户开仓规则判断。必须逐项验证全部条件；K线形态、连续涨跌、近期高低点直接从"
            "按时间升序提供的OHLCV判断。条件不完整、不明确或数据不足时不开仓。"
        ),
        "position_prompt_template": (
            "严格按用户持仓风控规则判断。只有明确触发规则时才能平仓、加仓或修改止盈止损；"
            "没有触发时继续持有。"
        ),
        "open_indicators": [],
        "position_indicators": [],
        "open_data_type": "kline",
        "position_data_type": "kline",
        "unsupported_indicators": [],
        "unsupported_conditions": [],
        "unsupported_condition_count": 0,
        "visual_conditions": [],
        "warnings": [],
        "prompt_version": 2,
        "compile_status": "fallback",
        "open_logic": open_logic,
        "position_logic": position_logic,
    }


def _rewrite_rule_plan_indicator_aliases(value: Any, specs: list[dict[str, Any]]) -> Any:
    if not isinstance(value, dict):
        return value
    plan = json.loads(json.dumps(value, ensure_ascii=False))
    grouped: dict[str, list[str]] = {}
    for spec in specs:
        name = str(spec.get("name") or "").strip().lower()
        alias = str(spec.get("alias") or name).strip().lower()
        if name and alias:
            grouped.setdefault(name, []).append(alias)
    replacements = {
        name: aliases[0]
        for name, aliases in grouped.items()
        if len(set(aliases)) == 1 and aliases[0] != name
    }
    for rule in plan.get("rules") if isinstance(plan.get("rules"), list) else []:
        if not isinstance(rule, dict):
            continue
        for container, keys in ((rule, ("when",)), (rule.get("action"), ("sl", "tp", "lot", "volume"))):
            if not isinstance(container, dict):
                continue
            for key in keys:
                expression = container.get(key)
                if not isinstance(expression, str):
                    continue
                for source, target in replacements.items():
                    expression = re.sub(rf"\b{re.escape(source)}\b", target, expression, flags=re.IGNORECASE)
                container[key] = expression
    return plan


def _validate_user_stop_constraints(position_logic: str, plan: Any) -> None:
    if not isinstance(plan, dict) or str(plan.get("mode") or "").lower() != "deterministic":
        return
    actions = [
        rule.get("action")
        for rule in (plan.get("rules") if isinstance(plan.get("rules"), list) else [])
        if isinstance(rule, dict) and isinstance(rule.get("action"), dict)
    ]
    normalized_logic = re.sub(r"\s+", "", str(position_logic or ""))
    requires_not_below = any(item in normalized_logic for item in ("不能低于原止损价", "不得低于原止损价"))
    requires_not_above = any(item in normalized_logic for item in ("不能高于原止损价", "不得高于原止损价"))
    if requires_not_below and not any(action.get("sl_constraint") == "not_below_current" for action in actions):
        raise RulePlanError("missing_not_below_current_stop_constraint")
    if requires_not_above and not any(action.get("sl_constraint") == "not_above_current" for action in actions):
        raise RulePlanError("missing_not_above_current_stop_constraint")


def _validate_user_cross_rules(user_logic: str, plan: Any) -> None:
    if not isinstance(plan, dict) or str(plan.get("mode") or "").lower() != "deterministic":
        return
    rules = plan.get("rules") if isinstance(plan.get("rules"), list) else []
    expressions = [str(rule.get("when") or "") for rule in rules if isinstance(rule, dict)]
    joined = "\n".join(expressions)
    logic = str(user_logic or "")
    upward = bool(re.search(r"\bcross_above\s*\(", joined)) or bool(
        re.search(r"\blatest_cross\s*\([^)]*\)\s*==\s*1\b", joined)
    )
    downward = bool(re.search(r"\bcross_below\s*\(", joined)) or bool(
        re.search(r"\blatest_cross\s*\([^)]*\)\s*==\s*-1\b", joined)
    )
    if "上穿" in logic and not upward:
        raise RulePlanError("missing_upward_cross_condition")
    if ("下穿" in logic or "下破" in logic) and not downward:
        raise RulePlanError("missing_downward_cross_condition")


def _custom_data_type(value: Any, unsupported: list[str]) -> str:
    normalized = str(value or "kline").strip().lower()
    if normalized not in {"kline", "screenshot", "both"}:
        normalized = "kline"
    if unsupported and normalized == "kline":
        return "both"
    return normalized


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [] if value is None or value == "" else [value]


def _stage_unsupported_conditions(value: Any, stage: str) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value] if value else []
    result: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            result.append({**item, "stage": stage})
        elif str(item).strip():
            result.append({"stage": stage, "text": str(item).strip()})
    return result


def _normalize_unsupported_conditions(value: Any) -> list[dict[str, str]]:
    items = value if isinstance(value, list) else [value] if value else []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        raw = item if isinstance(item, dict) else {"text": item}
        stage = str(raw.get("stage") or "").strip().lower()
        if stage not in {"open", "position"}:
            stage = "open"
        text = str(raw.get("text") or raw.get("condition") or "").strip()[:1000]
        if not text:
            continue
        key = (stage, re.sub(r"\s+", "", text).lower())
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "stage": stage,
            "code": str(raw.get("code") or "unsupported_condition").strip()[:80],
            "text": text,
            "reason": str(raw.get("reason") or "暂时无法转换为服务端精确执行条件").strip()[:500],
        })
    return result


def _normalize_visual_conditions(value: Any) -> list[dict[str, str]]:
    items = value if isinstance(value, list) else [value] if value else []
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        raw = item if isinstance(item, dict) else {"text": item}
        stage = str(raw.get("stage") or "").strip().lower()
        if stage not in {"open", "position"}:
            stage = "open"
        text = str(raw.get("text") or raw.get("condition") or "").strip()[:1000]
        if not text:
            continue
        key = (stage, re.sub(r"\s+", "", text).lower())
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "stage": stage,
            "code": str(raw.get("code") or "visual_condition").strip()[:80],
            "text": text,
        })
    return result


def _runtime_visual_conditions(config: dict[str, Any], stage: str) -> list[dict[str, str]]:
    """Return only the screenshot conditions relevant to the current decision stage."""
    return [
        {"code": item["code"], "text": item["text"]}
        for item in _normalize_visual_conditions(config.get("visual_conditions"))
        if item["stage"] == stage
    ]


def _clean_stage_summary(value: Any) -> str:
    return str(value or "").strip().strip("；;。 ")


def _extract_explicit_indicator_specs(logic: str) -> list[dict[str, Any]]:
    """Extract unambiguous built-in indicator references such as EMA5 or ATR(14)."""
    text = str(logic or "").lower()
    if not text:
        return []
    definitions = {item.name: item for item in INDICATOR_DEFINITIONS}
    names = sorted(definitions, key=len, reverse=True)
    specs: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for name in [*names, "ma", "boll", "kdj", "stochastic"]:
        canonical = {"ma": "sma", "boll": "bbands", "kdj": "stoch", "stochastic": "stoch"}.get(name, name)
        pattern = re.compile(
            rf"(?<![a-z_]){re.escape(name)}\s*(?:\(|（)?\s*(\d+(?:\.\d+)?)?\s*(?:\)|）)?(?![a-z_])",
            re.IGNORECASE,
        )
        definition = definitions[canonical]
        for match in pattern.finditer(text):
            raw_period = match.group(1)
            period = int(float(raw_period)) if raw_period else None
            signature = (canonical, period)
            if signature in seen:
                continue
            seen.add(signature)
            params = dict(definition.default_params)
            if period is not None and "length" in params:
                params["length"] = period
            specs.append({"name": canonical, "source": "close", "params": params})
    return specs


def _reconcile_compilation_warnings(position_logic: str, raw_warnings: list[Any]) -> list[str]:
    text = str(position_logic or "").strip().lower()
    has_add = bool(re.search(r"加仓|补仓|增仓|add\s*(?:position)?|pyramid", text, re.IGNORECASE))
    has_partial_close = bool(re.search(r"部分平仓|减仓|平(?:掉|掉仓)?\s*(?:一半|半仓|\d+(?:\.\d+)?\s*(?:%|％|手))|reduce", text, re.IGNORECASE))
    warnings: list[str] = []
    for raw in raw_warnings:
        warning = str(raw or "").strip()[:300]
        if not warning:
            continue
        lowered = warning.lower()
        if re.search(r"默认(?:数据)?源|基于收盘价|索引|闭合性|不变更\s*input|default\s*source", lowered, re.IGNORECASE):
            continue
        if not has_add and re.search(r"加仓|补仓|增仓|add|pyramid|sizing", lowered, re.IGNORECASE):
            continue
        if not has_partial_close and re.search(r"部分平仓|减仓|partial\s*close|reduce", lowered, re.IGNORECASE):
            continue
        if not re.search(
            r"不支持|无法|缺少|未指定|未明确|不明确|歧义|冲突|需要.{0,8}(?:截图|补充|确认)|数据不足|默认使用策略|请补充|unsupported|missing|ambiguous|conflict",
            warning,
            re.IGNORECASE,
        ):
            continue
        if warning not in warnings:
            warnings.append(warning)

    if has_add:
        has_add_sizing = bool(re.search(
            r"(?:加仓|补仓|增仓).{0,30}(?:\d+(?:\.\d+)?\s*(?:倍|手|%|％)|一半|半仓|相同|同等|固定|仓位|手数|lot|比例)",
            text,
            re.IGNORECASE,
        ))
        default_warning = "未指定加仓手数计算方式，将默认使用策略的开仓仓位算法。"
        if not has_add_sizing and not any("加仓" in item and ("手数" in item or "仓位" in item or "sizing" in item.lower()) for item in warnings):
            warnings.append(default_warning)

    if has_partial_close:
        has_partial_size = bool(re.search(
            r"(?:部分平仓|减仓|平(?:掉|掉仓)?).{0,30}(?:\d+(?:\.\d+)?\s*(?:%|％|手)|一半|半仓|三分之一|四分之一|比例|全部的)",
            text,
            re.IGNORECASE,
        ))
        partial_warning = "部分平仓规则未指定平仓比例或手数，请补充具体数量。"
        if not has_partial_size and not any("部分平仓" in item or "减仓" in item for item in warnings):
            warnings.append(partial_warning)
    return warnings


def _apply_position_template_guardrails(template: str, specs: list[dict[str, Any]]) -> str:
    marker = "【系统数据说明】"
    cleaned = str(template or "").strip()
    for generated_marker in ("【系统运行约束】", marker):
        if generated_marker in cleaned:
            cleaned = cleaned.split(generated_marker, 1)[0].rstrip()
    cleaned = re.sub(r"\b([a-z][a-z0-9_]*)\s*\[\s*(\d+)\s*\]", r"\1\2", cleaned, flags=re.IGNORECASE)
    if any(str(spec.get("name") or "") in {"atr", "natr"} for spec in specs):
        lines = []
        for line in cleaned.splitlines():
            if re.search(r"\bprofit\b", line, re.IGNORECASE) and re.search(r"\b(?:n?atr)\d*\b", line, re.IGNORECASE):
                line = re.sub(r"\bprofit\b", "favorable_price_move", line, flags=re.IGNORECASE)
            lines.append(line)
        cleaned = "\n".join(lines)
    aliases = "、".join(str(spec.get("alias") or spec.get("name") or "") for spec in specs) or "无"
    guardrails = (
        f"{marker}\n"
        f"1. 指标数组别名：{aliases}；数组按时间从旧到新排列，[-1] 是最新已收盘K线，[-2] 是上一根已收盘K线。\n"
        "2. ATR是由最高价、最低价和收盘价计算的价格距离，不能与账户货币盈亏profit直接比较。"
        "多单有利价格移动=current_price-open_price，空单有利价格移动=open_price-current_price。\n"
    )
    return f"{cleaned}\n\n{guardrails}".strip()


def _workflow_computed_facts(config: dict[str, Any], stage_name: str, indicators: dict[str, Any], runtime: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Derive objective comparison/crossover facts from a compiled workflow.

    These facts are advisory context for the model; the workflow remains the
    source of truth and visual/AI conditions are intentionally left unresolved.
    """
    compiled = config.get("compiled_workflow") if isinstance(config.get("compiled_workflow"), dict) else {}
    stage = compiled.get(stage_name) if isinstance(compiled.get(stage_name), dict) else {}
    nodes = stage.get("nodes") if isinstance(stage.get("nodes"), dict) else {}
    values = indicators.get("values") if isinstance(indicators.get("values"), dict) else {}
    runtime = runtime or {}
    position = runtime.get("position") if isinstance(runtime.get("position"), dict) else {}
    facts: list[dict[str, Any]] = []

    def operand_key(operand: Any) -> str:
        if not isinstance(operand, dict) or operand.get("kind") != "indicator":
            return ""
        return str(operand.get("alias") or "").strip()

    def nums(key: str) -> list[float]:
        raw = values.get(key, [])
        if not isinstance(raw, list): return []
        out: list[float] = []
        for item in raw:
            try: out.append(float(item))
            except (TypeError, ValueError): pass
        return out

    def operand_values(operand: Any) -> list[float]:
        if not isinstance(operand, dict): return []
        kind, name = operand.get("kind"), str(operand.get("name") or "")
        if kind == "indicator": return nums(operand_key(operand))
        if kind == "constant":
            try: return [float(operand.get("value"))]
            except (TypeError, ValueError): return []
        mapping = {"open_price": "open_price", "current_price": "current_price", "sl": "sl", "tp": "tp", "profit": "profit", "volume": "volume"}
        if kind == "position" and name in mapping:
            try: return [float(position.get(mapping[name]))]
            except (TypeError, ValueError): return []
        if kind == "position" and name == "price_open_distance":
            try: return [float(position["current_price"]) - float(position["open_price"])]
            except (KeyError, TypeError, ValueError): return []
        if kind == "position" and name == "price_sl_distance":
            try: return [float(position["current_price"]) - float(position["sl"])]
            except (KeyError, TypeError, ValueError): return []
        if kind == "position" and name == "price_tp_distance":
            try: return [float(position["current_price"]) - float(position["tp"])]
            except (KeyError, TypeError, ValueError): return []
        return []

    for node in nodes.values():
        if not isinstance(node, dict) or node.get("type") != "condition": continue
        condition = node.get("condition") if isinstance(node.get("condition"), dict) else {}
        left_key, right_key = operand_key(condition.get("left")), operand_key(condition.get("right"))
        left, right = operand_values(condition.get("left")), operand_values(condition.get("right"))
        if left_key and right_key and left and right:
            count = min(len(left), len(right))
            left, right = left[-count:], right[-count:]
            relation = ["above" if a > b else "below" if a < b else "equal" for a, b in zip(left, right)]
            # Limit crossover facts to the condition's requested lookback.
            # The previous implementation scanned the complete indicator
            # history, which could report both an old up-cross and down-cross
            # for a single condition.
            lookback = max(1, int(condition.get("lookback") or condition.get("count") or 1))
            recent_relation = relation[-lookback:]
            cross_events: list[dict[str, Any]] = []
            for index in range(1, len(recent_relation)):
                previous, current = recent_relation[index - 1], recent_relation[index]
                if previous in {"below", "equal"} and current == "above":
                    cross_events.append({"direction": "up", "index": -len(recent_relation) + index})
                elif previous in {"above", "equal"} and current == "below":
                    cross_events.append({"direction": "down", "index": -len(recent_relation) + index})
            latest_cross = cross_events[-1]["direction"] if cross_events else None
            cross_up = any(item["direction"] == "up" for item in cross_events)
            cross_down = any(item["direction"] == "down" for item in cross_events)
            operator = condition.get("operator") or "gt"
            a, b = left[-1], right[-1]
            result = {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b, "eq": a == b, "neq": a != b}.get(operator)
            if condition.get("kind") == "cross":
                expected = "up" if condition.get("direction") == "above" else "down"
                result = (latest_cross == expected) if condition.get("cross_mode") == "latest" else (cross_up if expected == "up" else cross_down)
            facts.append({"node_id": node.get("id"), "description": condition.get("description") or node.get("label"), "latest_left": a, "latest_right": b, "operator": operator, "condition_result": result, "latest_relation": relation[-1], "relations_oldest_to_latest": recent_relation, "lookback": lookback, "cross_up": cross_up, "cross_down": cross_down, "latest_cross": latest_cross, "cross_events": cross_events})
        elif left_key and left:
            facts.append({"node_id": node.get("id"), "description": condition.get("description") or node.get("label"), "latest_value": left[-1]})
    return facts


def _custom_runtime_template(value: Any) -> str:
    template = str(value or "").strip()
    for marker in ("【系统运行约束】", "【系统数据说明】"):
        if marker in template:
            template = template.split(marker, 1)[0].rstrip()
    return template


def _uses_literal_user_rules(deployment: dict[str, Any], endpoint: str) -> bool:
    return endpoint.startswith("compile") or str(deployment.get("strategy_code") or "") == "CUSTOM_AI_V1"


def _json_api_system_prompt(
    endpoint: str,
    task_prompt: str,
    *,
    literal_user_rules: bool = False,
) -> str:
    if endpoint.startswith("workflow_"):
        return (
            "Strict JSON API mode. Output exactly one compact JSON object and nothing else. "
            "Start with { and end with }. No markdown, code fences, prefix, suffix or prose outside JSON. "
            f"Task: {task_prompt}"
        )
    if endpoint.startswith("compile"):
        return (
            "Strict JSON API mode. Output exactly one compact JSON object and nothing else. "
            "Required keys: summary, prompt_template, indicators, rule_plan, data_type, unsupported_indicators, "
            "visual_conditions, unsupported_conditions, warnings. visual_conditions is an array of "
            "{text:'exact screenshot-dependent user condition',code:'short visual capability code'}. "
            "unsupported_conditions is an array of "
            "{text:'exact unsupported user condition',code:'short capability code',reason:'short Chinese reason'}. "
            "Use an empty array when every user condition is represented exactly by rule_plan. "
            "Each indicator item uses {name,source,params,alias}. data_type is kline, screenshot, or both. "
            f"Task: {task_prompt}"
        )
    schema = (
        '{"should_open":false,"direction":null,"confidence":0,'
        '"lot":0,"sl":null,"tp":null,"sl_distance_price":0,"tp_distance_price":0,'
        '"reason":"short Chinese reason","analysis":"concise Chinese conclusion"}'
        if endpoint == "open"
        else '{"action":"hold","ticket":null,"direction":null,"confidence":0,'
        '"lot":null,"close_scope":null,"volume":null,"sl":null,"tp":null,'
        '"reason":"short Chinese reason","analysis":"concise Chinese conclusion"}'
    )
    if literal_user_rules:
        return (
            "Strict JSON API mode. Output exactly one compact JSON object and nothing else. "
            "Start with { and end with }. No markdown, code fences, prefix, suffix or prose outside JSON. "
            f"Required JSON shape: {schema}. "
            "The user's original rule is the sole source of trading logic. Do not add, optimize, recommend or infer "
            "any trading condition. reason and analysis must explain only the supplied user rule, relevant supplied "
            "data and the resulting action. Do not add market structure, setup score, trend, confirmation, risk-reward "
            "or safety analysis unless the user explicitly wrote it. Never invent missing facts or prices. If required "
            "data is missing, state that fact without replacing it with another condition. "
            "Complete valid JSON has highest priority. Analyze silently; never put step-by-step reasoning, repeated checks, "
            "full arrays, candle lists or long timestamp lists in the response. reason must be a short Chinese conclusion "
            "of at most 60 Chinese characters. analysis must be a clear, natural and compact Chinese strategy explanation, "
            "normally 40-220 Chinese characters. Check the user's explicit conditions one by one, state which condition "
            "passed or failed, and explain the final action. Do not quote raw prices, indicator values, calculated action "
            "prices or volumes. Do not show formulas, substitutions, intermediate calculations or long decimal precision. "
            "Mention indicator names, rule stages and qualitative comparisons only when they help explain the decision. "
            "For hold/none, identify the exact unmet condition instead of only saying that conditions were not met. "
            "Never copy placeholder text from the schema. "
            f"Task: {task_prompt}"
        )
    return (
        "Strict JSON API mode. Output exactly one compact JSON object and nothing else. "
        "Start with { and end with }. No markdown, no code fences, no prefix, no suffix, no prose outside JSON. "
        "Never write phrases like 'We need', 'Need decide', 'Let's think', or 'JSON only'. "
        "Analyze internally and write only the final conclusion into reason and analysis. "
        "If uncertain or unsafe, return the safe object directly. "
        f"Required JSON shape: {schema}. "
        "reason: Chinese, concrete, <=60 Chinese characters. "
        "analysis: Chinese, 120-300 Chinese characters, include market structure, setup_score, price/risk context, and action rationale. "
        "Never copy placeholder text such as reason, analysis, or .... "
        f"Task: {task_prompt}"
    )


def _max_tokens_for_endpoint(endpoint: str) -> int:
    if endpoint.startswith("workflow_"):
        return 6000
    if endpoint.startswith("compile"):
        return 2400
    if endpoint == "open":
        return 1000
    if endpoint == "position":
        return 1000
    return 500

def _compact_candles(candles: list[Candle], *, limit: int) -> list[dict[str, Any]]:
    ordered = sorted(candles, key=lambda candle: candle.timestamp)[-limit:]
    return [
        {
            "t": candle.timestamp,
            "o": candle.open,
            "h": candle.high,
            "l": candle.low,
            "c": candle.close,
            "v": candle.volume,
        }
        for candle in ordered
    ]


def _extract_json_object(content: str, *, endpoint: str = "") -> dict[str, Any]:
    stripped = content.strip()
    if not stripped:
        raise ValueError("AI response content empty")
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        json_object = _find_decision_json_object(stripped, endpoint=endpoint)
        if json_object is None:
            raise
        parsed = json.loads(json_object)
    if not isinstance(parsed, dict):
        raise ValueError("AI response must be a JSON object")
    required_key = "should_open" if endpoint == "open" else "action" if endpoint == "position" else ""
    if required_key and required_key not in parsed:
        raise ValueError(f"AI response missing required key: {required_key}")
    _normalize_decision_reason(parsed, endpoint=endpoint)
    return parsed


def _normalize_decision_reason(parsed: dict[str, Any], *, endpoint: str = "") -> None:
    reason = str(parsed.get("reason") or "").strip()
    analysis = str(parsed.get("analysis") or "").strip()
    placeholder_reasons = {
        "",
        "...",
        "reason",
        "analysis",
        "短原因",
        "观望原因",
        "根据行情给出具体原因",
        "具体原因",
        "原因",
        "详细中文行情和风控说明",
        "详细中文持仓和风控说明",
    }

    if endpoint == "open":
        default_reason = "开仓条件不足，继续观望"
        default_analysis = "当前开仓条件不足，暂未看到足够清晰的突破、趋势延续或回调确认，继续等待更稳定的结构、动能延续与风险空间。"
    elif endpoint == "position":
        default_reason = "风控条件未触发，继续持有"
        default_analysis = "当前持仓未触发明确反向信号、结构失效或止损调整条件，暂按原有止盈止损和策略节奏继续管理。"
    else:
        default_reason = "条件不足，继续观望"
        default_analysis = default_reason

    if reason.lower() in placeholder_reasons:
        reason = default_reason
    if analysis.lower() in placeholder_reasons:
        analysis = reason
    if len(reason) > 90:
        if len(analysis) < len(reason):
            analysis = reason
        reason = reason[:90]
    if len(analysis) < 16:
        analysis = default_analysis
    parsed["reason"] = reason[:120]
    parsed["analysis"] = analysis[:800]


def _recover_decision_from_text(content: str, *, endpoint: str = "") -> dict[str, Any] | None:
    summary = _summarize_malformed_ai_text(content)
    if not summary:
        return None
    lowered = summary.lower()
    if lowered.startswith("<html") or lowered.startswith("<!doctype"):
        return None
    if "invalid_request_error" in lowered or "api provider http" in lowered:
        return None

    if endpoint == "open":
        recovered = _fallback_decision("open", "AI返回格式异常，按风控不开仓")
        recovered["analysis"] = (
            "AI返回不是标准JSON，系统已拒绝执行开仓并保留原始分析。"
            f"原始分析要点：{summary}"
        )[:800]
        return recovered

    if endpoint == "position":
        recovered = _fallback_decision("position", "AI返回格式异常，按风控继续持有")
        recovered["analysis"] = (
            "AI返回不是标准JSON，系统已按保守风控继续持有，不执行平仓、加仓或改价。"
            f"原始分析要点：{summary}"
        )[:800]
        return recovered

    return None


def _summarize_malformed_ai_text(content: str, *, limit: int = 520) -> str:
    text = " ".join(str(content or "").split())
    if not text:
        return ""
    noise_phrases = (
        "We need answer JSON only, minified.",
        "We need output JSON only.",
        "We need respond JSON only.",
        "We need produce JSON object.",
        "We need produce final JSON.",
        "Need output minified JSON",
        "Need decide open.",
        "Need decide position.",
        "Need decide.",
        "Need examine features.",
        "Need analyze.",
        "Let's reason.",
        "Let’s reason.",
        "Let's think carefully.",
        "Need answer JSON only.",
        "Strict JSON API mode.",
    )
    for phrase in noise_phrases:
        text = text.replace(phrase, " ")
    return " ".join(text.split())[:limit]


def _find_decision_json_object(value: str, *, endpoint: str = "") -> str | None:
    required_key = "should_open" if endpoint == "open" else "action" if endpoint == "position" else ""
    candidates = _iter_json_objects(value)
    fallback = None
    for candidate in candidates:
        if fallback is None:
            fallback = candidate
        if not required_key:
            return candidate
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and required_key in parsed:
            return candidate
    return fallback if not required_key else None


def _iter_json_objects(value: str) -> list[str]:
    objects: list[str] = []
    starts = [index for index, char in enumerate(value) if char == "{"]
    for start in starts:
        obj = _scan_json_object(value, start)
        if obj is not None:
            objects.append(obj)
    return objects


def _scan_json_object(value: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return None


def _format_response_preview(*, raw: str = "", parsed: dict[str, Any] | None = None) -> str:
    parts: list[str] = []
    raw = str(raw or "").strip()
    if raw:
        parts.append(f"原始返回:\n{_preview_text(raw, 1600)}")
    if parsed is not None:
        parts.append(f"解析结果:\n{_preview_text(json.dumps(parsed, ensure_ascii=False), 1600)}")
    return "\n\n".join(parts)[:3200]


def _format_request_snapshot(
    *,
    model: dict[str, Any],
    endpoint: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    body: dict[str, Any] = {
        "model": str(model.get("model") or model.get("name") or ""),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": _max_tokens_for_endpoint(endpoint),
    }
    if bool(model.get("strict_json", True)):
        body["response_format"] = {"type": "json_object"}
    return json.dumps(body, ensure_ascii=False, indent=2)


def _with_indicator_request_preview(preview: str, user_payload: dict[str, Any]) -> str:
    indicators = user_payload.get("indicators")
    computed_facts = user_payload.get("computed_facts")
    if not isinstance(indicators, dict) and not computed_facts:
        return preview
    snapshot_data: dict[str, Any] = {}
    if isinstance(indicators, dict) and indicators.get("recent_values"):
        snapshot_data["recent_values"] = indicators["recent_values"]
    if isinstance(computed_facts, list) and computed_facts:
        snapshot_data["computed_facts"] = computed_facts
    if not snapshot_data:
        return preview
    snapshot = json.dumps(snapshot_data, ensure_ascii=False, separators=(",", ":"))
    return f"请求指标快照:\n{snapshot}\n\n{preview}"[:4000]


def _screenshot_ai_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("preview_id", "mime_type", "size_bytes", "sha256")
        if value.get(key) not in (None, "")
    }

def _fallback_decision(endpoint: str, reason: str) -> dict[str, Any]:
    if endpoint == "open":
        return {
            "should_open": False,
            "direction": None,
            "confidence": 0,
            "lot": 0,
            "sl_distance_price": 0,
            "tp_distance_price": 0,
            "reason": reason,
            "analysis": reason,
        }
    return {
        "action": "hold",
        "ticket": None,
        "direction": None,
        "confidence": 0,
        "lot": 0,
        "sl": None,
        "tp": None,
        "reason": reason,
        "analysis": reason,
    }

def _vision_test_answer_is_correct(value: str) -> bool:
    text = str(value or "")
    normalized_english = re.sub(r"[^A-Z]+", " ", text.upper()).strip()
    red_circle = bool(re.search(
        r"\b(?:RED (?:CIRCLE|SEMICIRCLE)S?|(?:CIRCLE|SEMICIRCLE)S? RED)\b",
        normalized_english,
    ))
    blue_shape = bool(re.search(
        r"\b(?:BLUE (?:SQUARE|RECTANGLE)S?|(?:SQUARE|RECTANGLE)S? BLUE)\b",
        normalized_english,
    ))
    english_correct = red_circle and blue_shape
    normalized_chinese = re.sub(r"[^\u4e00-\u9fff]", "", text)
    chinese_correct = (
        any(pair in normalized_chinese for pair in ("红色圆", "红圆", "圆形为红", "圆是红"))
        and any(pair in normalized_chinese for pair in ("蓝色方", "蓝方", "蓝色矩", "蓝矩", "蓝色长方", "方形为蓝", "矩形为蓝"))
    )
    return english_correct or chinese_correct


def _preview_text(value: str, limit: int = 500) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()[:limit]


def _preview_json(value: Any, limit: int = 500) -> str:
    try:
        return _preview_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), limit)
    except TypeError:
        return _preview_text(str(value), limit)

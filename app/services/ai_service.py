from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from app.models import Candle, OpenEvaluateRequest, PositionEvaluateRequest, UsageSummary
from app.store import SqliteStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiCallResult:
    content: dict[str, Any]
    usage: UsageSummary


class AiDecisionClient:
    def __init__(self, store: SqliteStore, *, timeout: float = 20.0) -> None:
        self.store = store
        self.timeout = timeout

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
                        "rule": "score >= 70 means a valid PA candidate; AI may reject only for clear risk or conflicting structure.",
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
                },
            },
        )

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
    ) -> AiCallResult | None:
        model = self._select_model(deployment, endpoint)
        if model is None:
            return None

        provider_id = str(model["provider_id"])
        model_id = str(model["id"])
        prompt = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        response_content = ""
        usage = UsageSummary(ai_called=True)

        try:
            raw = self._post_chat_completion(
                base_url=str(model["provider_base_url"]),
                api_key=str(model["provider_api_key"]),
                model=str(model["name"]),
                system_prompt=system_prompt,
                user_prompt=prompt,
            )
            parsed = json.loads(raw)
            response_content = str(parsed["choices"][0]["message"]["content"])
            usage_payload = parsed.get("usage") or {}
            usage = UsageSummary(
                ai_called=True,
                input_tokens=int(usage_payload.get("prompt_tokens") or 0),
                output_tokens=int(usage_payload.get("completion_tokens") or 0),
                charged_points=int(usage_payload.get("total_tokens") or 0),
            )
            content = _extract_json_object(response_content)
            self._save_usage(deployment, endpoint, provider_id, model_id, usage, request_payload=user_payload, success=True)
            return AiCallResult(content=content, usage=usage)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            logger.warning("AI decision call failed: %s", message)
            self._save_usage(
                deployment,
                endpoint,
                provider_id,
                model_id,
                usage,
                request_payload=user_payload,
                success=False,
                error_message=message[:1000],
            )
            return None

    def _select_model(self, deployment: dict[str, Any], endpoint: str) -> dict[str, Any] | None:
        official_strategy = self.store.get_official_ai_strategy(str(deployment.get("strategy_code") or ""))
        if official_strategy is not None:
            model_key = "open_model_id" if endpoint == "open" else "position_model_id"
            configured_model_id = str(official_strategy.get(model_key) or "").strip()
            if configured_model_id:
                model = self.store.get_private_ai_model(configured_model_id)
                if model is not None:
                    return model
        return self.store.get_default_ai_model()

    def _post_chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        url = f"{base_url.rstrip('/')}/chat/completions"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
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
            raise RuntimeError(f"AI provider HTTP {exc.code}: {detail[:500]}") from exc

    def _save_usage(
        self,
        deployment: dict[str, Any],
        endpoint: str,
        provider_id: str,
        model_id: str,
        usage: UsageSummary,
        *,
        request_payload: dict[str, Any] | None = None,
        success: bool,
        error_message: str = "",
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
                "official_tokens": usage.charged_points or usage.input_tokens + usage.output_tokens,
                "custom_tokens": 0,
                "success": success,
                "error_message": error_message,
            },
        )


def _pa_system_prompt() -> str:
    return (
        "You are GainLab PA Agent, a price-action trading decision engine. "
        "Return exactly one JSON object and no Markdown. "
        "Think in two stages internally: first diagnose market regime, structure, breakout quality, "
        "barbwire/range risk, H1/H2/L1/L2, failed breakouts, spike/channel/range cycle, and momentum; "
        "Also consider wedge, MTR, final flag, triangle compression, double top/bottom, signal bar quality, "
        "follow-through, climax and transition risk. "
        "then decide the trade. "
        "Respect program_recommendation: when setup_score >= 70, treat it as a valid candidate and "
        "reject only if there is a clear risk conflict. "
        "Never invent prices. Use price distances for SL/TP. If evidence is weak, hold."
    )


def _compact_candles(candles: list[Candle], *, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "t": candle.timestamp,
            "o": candle.open,
            "h": candle.high,
            "l": candle.low,
            "c": candle.close,
            "v": candle.volume,
        }
        for candle in candles[-limit:]
    ]


def _extract_json_object(content: str) -> dict[str, Any]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI response must be a JSON object")
    return parsed

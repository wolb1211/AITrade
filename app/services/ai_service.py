from __future__ import annotations

import json
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
    calculate_indicator_payload,
    normalize_indicator_specs,
    public_indicator_catalog,
    required_candle_count,
)
from app.store import SqliteStore

logger = logging.getLogger(__name__)

_VISION_TEST_IMAGE_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAPAAAABgCAIAAACsUWiGAAABbElEQVR42u3cUQrEIBBEQe9/6UhOEAiidlMP9nskFuwSlhmPVNR4P1JFQKsXtG8rBf/SAFpAS0BLQEtAC2igBbQEtAS0BLSABlpAnz797/+vmAU00EADDTTQQAMNNNBAAx0FeuHl7XTwOeuqw9z5DIEGGmiggQYaaKCBBhpooIEGGmiggQYaaKCBBhpooIEGGmiggQYaaKCBBhpooIEGGmiggQYaaKCBBhpooF0G0EADDTTQQAMNNNBAAw000C4DaKCB9gxtTrI5yZYmoM0CGmizgAbaLKAhA9pbDm8eQo8NNNBAAw000EADDTTQQAMNNNBAAw000EADDTTQQAMNNNBAAw000EADDTTQQAMNNNBAAw000EADDTTQQAMNNNBAAw000EADDTTQQAMNNNBAAw000EADbZtR8DYjoIEGGmiggQbaLKCBBhpoCWgJaAENtICWgJaAloAW0EALaAloCWgJaAEtpQe0GkFLNU07nX/NRiJJzgAAAABJRU5ErkJggg=="
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
                    "analysis": "detailed final Chinese market explanation",
                },
            },
        )

    def compile_custom_strategy(self, deployment: dict[str, Any]) -> dict[str, Any]:
        """Turn the user's natural-language rules into a reusable runtime definition."""
        config = deployment.get("config") if isinstance(deployment.get("config"), dict) else {}
        open_logic = str(config.get("open_logic") or "").strip()
        position_logic = str(config.get("position_logic") or "").strip()
        fallback = _custom_strategy_fallback(open_logic, position_logic)
        result = self._chat_json(
            deployment=deployment,
            endpoint="compile",
            system_prompt=_custom_strategy_compile_prompt(),
            user_payload={
                "task": "compile_custom_trading_strategy",
                "open_logic": open_logic,
                "position_logic": position_logic,
                "available_indicators": public_indicator_catalog(),
                "rules": {
                    "candlestick_patterns": "Do not create indicators for candlestick patterns; runtime supplies OHLCV arrays.",
                    "unsupported_indicator": "Put indicators outside available_indicators into unsupported_indicators.",
                    "data_type": "Use kline unless the rule explicitly requires a chart image or an unsupported custom indicator.",
                    "execution": "Do not invent new action types. Position actions are hold, close, add, modify.",
                },
            },
        )
        content = result.content if result is not None else {}
        if not isinstance(content, dict) or not content.get("open_prompt_template"):
            return fallback
        open_specs, open_unsupported = normalize_indicator_specs(content.get("open_indicators"))
        position_specs, position_unsupported = normalize_indicator_specs(content.get("position_indicators"))
        raw_unsupported = content.get("unsupported_indicators")
        unsupported_items = raw_unsupported if isinstance(raw_unsupported, list) else [raw_unsupported] if raw_unsupported else []
        unsupported = []
        for item in [*unsupported_items, *open_unsupported, *position_unsupported]:
            name = str(item).strip()
            if name and name not in unsupported:
                unsupported.append(name)
        raw_warnings = content.get("warnings")
        warning_items = raw_warnings if isinstance(raw_warnings, list) else [raw_warnings] if raw_warnings else []
        return {
            "summary": str(content.get("summary") or fallback["summary"]).strip()[:1000],
            "open_prompt_template": str(content.get("open_prompt_template") or fallback["open_prompt_template"]).strip()[:8000],
            "position_prompt_template": str(content.get("position_prompt_template") or fallback["position_prompt_template"]).strip()[:8000],
            "open_indicators": open_specs,
            "position_indicators": position_specs,
            "open_kline_count": required_candle_count(open_specs),
            "position_kline_count": required_candle_count(position_specs),
            "open_data_type": _custom_data_type(content.get("open_data_type"), unsupported),
            "position_data_type": _custom_data_type(content.get("position_data_type"), unsupported),
            "unsupported_indicators": unsupported[:20],
            "warnings": [str(item)[:300] for item in warning_items if str(item).strip()][:10],
            "prompt_version": 1,
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
                "prompt_template": config.get("open_prompt_template", ""),
                "data_convention": {
                    "order": "oldest_to_latest",
                    "last_item": "latest_closed_candle",
                    "prices": "absolute market prices",
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
                "required_json_schema": {
                    "should_open": "boolean",
                    "direction": "buy|sell|null",
                    "confidence": "0..1 number",
                    "sl": "absolute stop-loss price or null",
                    "tp": "absolute take-profit price or null",
                    "reason": "short Chinese reason",
                    "analysis": "detailed final Chinese explanation",
                },
            },
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
                "prompt_template": config.get("position_prompt_template", ""),
                "data_convention": {
                    "order": "oldest_to_latest",
                    "last_item": "latest_closed_candle",
                    "prices": "absolute market prices",
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
                "required_json_schema": {
                    "action": "hold|close|add|modify",
                    "ticket": "target ticket or null",
                    "direction": "buy|sell|null, required for add",
                    "volume": "close volume or null; null means full close",
                    "sl": "absolute stop-loss price or null",
                    "tp": "absolute take-profit price or null",
                    "confidence": "0..1 number",
                    "reason": "short Chinese reason",
                    "analysis": "detailed final Chinese explanation",
                },
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
            system_prompt="You are an image recognition tester. Return only the digits visible in the image.",
            user_prompt="Read the four digits in this image. Reply with digits only.",
            user_image_url=_VISION_TEST_IMAGE_DATA_URL,
            max_tokens=32,
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
        recognized_digits = "".join(character for character in content if character.isdigit())
        if "8264" not in recognized_digits:
            raise RuntimeError(f"模型未能正确识别测试图片（期望 8264，返回：{_preview_text(content or raw_response)}）")
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
        cache_key: str = "",
        cache_ttl_seconds: int = 120,
    ) -> AiCallResult:

        provider_id = str(model["provider_id"])
        model_id = self._usage_model_id(model)
        is_custom = bool(model.get("is_custom"))
        prompt = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        response_content = ""
        raw_response = ""
        usage = UsageSummary(ai_called=True)

        try:
            raw_response = self._post_chat_completion(
                base_url=str(model["provider_base_url"]),
                api_key=str(model["provider_api_key"]),
                model=str(model.get("model") or model.get("name") or ""),
                system_prompt=_json_api_system_prompt(endpoint, system_prompt),
                user_prompt=prompt,
                max_tokens=_max_tokens_for_endpoint(endpoint),
                strict_json=bool(model.get("strict_json", True)),
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
                    response_preview = _format_response_preview(raw=original_response_content, parsed=recovered)
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
                        success=False,
                        is_custom=is_custom,
                        error_message=message[:1000],
                        response_preview=_format_response_preview(raw=original_response_content or response_content),
                    )
                    return AiCallResult(content=_fallback_decision(endpoint, "AI未返回有效JSON，保守观望"), usage=usage)
            response_preview = _format_response_preview(raw=response_content, parsed=content)
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
            "system_prompt": _json_api_system_prompt(endpoint, system_prompt),
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
        self._save_usage(
            deployment,
            endpoint,
            str(model["provider_id"]),
            self._usage_model_id(model),
            usage,
            request_payload=user_payload,
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
        prefix = "open" if endpoint in {"open", "compile"} else "position"
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

        first_compatibility_error = ""
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
                if index == 0 and strict_json and exc.code in {400, 404, 422}:
                    first_compatibility_error = detail[:500]
                    logger.info(
                        "AI provider rejected response_format; retrying compatible request: model=%s, url=%s, status=%s",
                        model,
                        url,
                        exc.code,
                    )
                    continue
                suffix = f"; initial response_format error: {first_compatibility_error}" if first_compatibility_error else ""
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


def _custom_strategy_compile_prompt() -> str:
    return (
        "You compile a user's natural-language trading rules into reusable prompt templates. "
        "Preserve every condition and do not add trading conditions the user did not request. "
        "Extract only indicators that must be calculated by the server. Candlestick sequences, engulfing, "
        "pin bars, support, resistance, recent highs and recent lows are inferred directly from OHLCV and "
        "must not be listed as indicators. Templates must tell the runtime model to apply the user's rule "
        "strictly to supplied closed candles and calculated indicator arrays. Return Chinese template text."
    )


def _custom_runtime_prompt(endpoint: str) -> str:
    if endpoint == "open":
        return (
            "You execute a user-defined trading strategy. Apply the supplied user_rule and prompt_template "
            "strictly to closed OHLCV candles and indicator arrays. Candles are oldest to newest and the last "
            "item is the latest closed candle. Infer candlestick patterns directly from OHLCV. Do not invent "
            "missing facts or prices. If every requested condition is not clearly satisfied, do not open. "
            "Return absolute sl and tp prices when the rule defines them."
        )
    return (
        "You execute the position-management part of a user-defined trading strategy. Apply only the supplied "
        "user_rule and prompt_template to the closed candles, indicators and current positions. If no explicit "
        "risk condition is met, hold. Use only hold, close, add or modify. Never close or add without a clear rule."
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
        "warnings": [],
        "prompt_version": 1,
        "compile_status": "fallback",
        "open_logic": open_logic,
        "position_logic": position_logic,
    }


def _custom_data_type(value: Any, unsupported: list[str]) -> str:
    normalized = str(value or "kline").strip().lower()
    if normalized not in {"kline", "screenshot", "both"}:
        normalized = "kline"
    if unsupported and normalized == "kline":
        return "both"
    return normalized


def _json_api_system_prompt(endpoint: str, task_prompt: str) -> str:
    if endpoint == "compile":
        return (
            "Strict JSON API mode. Output exactly one compact JSON object and nothing else. "
            "Required keys: summary, open_prompt_template, position_prompt_template, open_indicators, "
            "position_indicators, open_data_type, position_data_type, unsupported_indicators, warnings. "
            "Each indicator item uses {name,source,params,alias}. data_type is kline, screenshot, or both. "
            f"Task: {task_prompt}"
        )
    schema = (
        '{"should_open":false,"direction":null,"confidence":0,'
        '"lot":0,"sl":null,"tp":null,"sl_distance_price":0,"tp_distance_price":0,'
        '"reason":"short Chinese reason","analysis":"detailed Chinese market and risk explanation"}'
        if endpoint == "open"
        else '{"action":"hold","ticket":null,"direction":null,"confidence":0,'
        '"lot":0,"sl":null,"tp":null,'
        '"reason":"short Chinese reason","analysis":"detailed Chinese position and risk explanation"}'
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
    if endpoint == "compile":
        return 1600
    if endpoint == "open":
        return 1000
    if endpoint == "position":
        return 1000
    return 500

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

def _preview_text(value: str, limit: int = 500) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()[:limit]


def _preview_json(value: Any, limit: int = 500) -> str:
    try:
        return _preview_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), limit)
    except TypeError:
        return _preview_text(str(value), limit)

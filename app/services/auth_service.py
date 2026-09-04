from __future__ import annotations

import re
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.security import hash_auth_value, hash_password, verify_password
from app.services.email_service import EmailService
from app.services.screenshot_preview import ScreenshotError, load_preview
from app.store import SqliteStore


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class UserAuthService:
    def __init__(
        self,
        store: SqliteStore,
        settings: Settings,
        email_service: EmailService,
        *,
        ai_client: Any | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.email_service = email_service
        self.ai_client = ai_client

    def register(self, *, email: str, password: str, invite_code: str = "") -> dict[str, Any]:
        normalized = self._email(email)
        self._password(password)
        try:
            user = self.store.prepare_registration(
                email=normalized,
                password_hash=hash_password(password),
                invite_code=invite_code,
            )
        except RuntimeError as exc:
            self._raise_store_error(exc)
        self._send_code(normalized, "register")
        return {"email": normalized, "user_id": user["id"], "expires_in": self.settings.verification_minutes * 60}

    def verify_registration(self, *, email: str, code: str) -> dict[str, Any]:
        normalized = self._email(email)
        self._consume_code(normalized, "register", code)
        try:
            user = self.store.activate_user(email=normalized)
        except RuntimeError as exc:
            self._raise_store_error(exc)
        return self._session(user)

    def password_login(self, *, email: str, password: str) -> dict[str, Any]:
        normalized = self._email(email)
        row = self.store.get_auth_user_by_email(normalized)
        if not row or not row.get("password_hash") or not verify_password(password, str(row["password_hash"])):
            raise AuthError("invalid_credentials", 401)
        self._assert_login_allowed(row)
        user = self.store.get_user(int(row["id"]))
        if user is None:
            raise AuthError("user_not_found", 404)
        return self._session(user)

    def send_login_code(self, *, email: str) -> dict[str, Any]:
        normalized = self._email(email)
        row = self.store.get_auth_user_by_email(normalized)
        if not row:
            raise AuthError("user_not_found", 404)
        if str(row.get("status") or "") == "disabled":
            raise AuthError("user_disabled", 403)
        self._send_code(normalized, "login")
        return {"email": normalized, "expires_in": self.settings.verification_minutes * 60}

    def code_login(self, *, email: str, code: str) -> dict[str, Any]:
        normalized = self._email(email)
        row = self.store.get_auth_user_by_email(normalized)
        if not row:
            raise AuthError("user_not_found", 404)
        if str(row.get("status") or "") == "disabled":
            raise AuthError("user_disabled", 403)
        self._consume_code(normalized, "login", code)
        try:
            user = self.store.activate_user(email=normalized)
        except RuntimeError as exc:
            self._raise_store_error(exc)
        return self._session(user)

    def forgot_password(self, *, email: str) -> dict[str, Any]:
        normalized = self._email(email)
        row = self.store.get_auth_user_by_email(normalized)
        if row and str(row.get("status") or "") != "disabled":
            self._send_code(normalized, "reset_password")
        return {"email": normalized, "expires_in": self.settings.verification_minutes * 60}

    def reset_password(self, *, email: str, code: str, password: str) -> dict[str, Any]:
        normalized = self._email(email)
        self._password(password)
        row = self.store.get_auth_user_by_email(normalized)
        if not row:
            raise AuthError("invalid_verification_code")
        self._consume_code(normalized, "reset_password", code)
        try:
            user = self.store.activate_user(email=normalized, password_hash=hash_password(password))
        except RuntimeError as exc:
            self._raise_store_error(exc)
        return self._session(user)

    def me(self, token: str) -> dict[str, Any]:
        user = self.store.get_session_user(self._token_hash(token))
        if user is None or not str(user.get("email") or "").strip():
            raise AuthError("invalid_session", 401)
        return user

    def portal(self, token: str) -> dict[str, Any]:
        user = self.me(token)
        return self.store.get_user_portal_data(int(user["id"]))

    def official_strategies(self, token: str) -> dict[str, Any]:
        self.me(token)
        return self.store.list_public_official_ai_strategies()

    def ai_model_options(self, token: str) -> dict[str, Any]:
        self.me(token)
        return self.store.list_public_ai_model_options()

    def ea_downloads(self, token: str) -> dict[str, Any]:
        self.me(token)
        return self.store.list_ea_downloads()

    def agent_dashboard(self, token: str, *, page: int, size: int) -> dict[str, Any]:
        user = self.me(token)
        try:
            return self.store.get_agent_dashboard(int(user["id"]), page=page, size=size)
        except RuntimeError as exc:
            self._raise_store_error(exc)

    def update_strategy_ai(self, token: str, *, deployment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        user = self.me(token)
        try:
            deployment = self.store.update_user_deployment_ai_config(
                user_id=int(user["id"]),
                deployment_id=deployment_id,
                payload=payload,
            )
        except RuntimeError as exc:
            self._raise_store_error(exc)
        return {"id": deployment["id"], "updated_at": deployment["updated_at"]}

    def update_strategy_settings(self, token: str, *, deployment_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        user = self.me(token)
        previous = next(
            (item for item in self.store.list_web_deployments(str(user["id"])) if item["id"] == deployment_id),
            None,
        )
        if previous and previous.get("strategy_code") == "CUSTOM_AI_V1":
            previous_config = dict(previous.get("config") or {})
            next_open_logic = str(payload.get("open_logic", previous_config.get("open_logic") or "")).strip()
            next_position_logic = str(payload.get("position_logic", previous_config.get("position_logic") or "")).strip()
            logic_changed = (
                next_open_logic != str(previous_config.get("open_logic") or "")
                or next_position_logic != str(previous_config.get("position_logic") or "")
            )
            # A visual graph can change while the legacy text rules remain
            # unchanged. Recompile whenever the client submits a workflow so
            # indicators/unsupported conditions cannot remain stale.
            if isinstance(payload.get("workflow"), dict):
                if self.ai_client is None:
                    raise AuthError("custom_strategy_compile_unavailable", 503)
                try:
                    compiled = self.ai_client.compile_custom_workflow(
                        payload["workflow"],
                        open_logic=next_open_logic,
                        position_logic=next_position_logic,
                    )
                except (RuntimeError, ValueError) as exc:
                    raise AuthError("invalid_custom_strategy_workflow") from exc
                payload = {**payload, "_compiled_config": compiled}
            elif logic_changed or payload.get("compiled_config") is not None:
                if self.ai_client is None:
                    raise AuthError("custom_strategy_compile_unavailable", 503)
                try:
                    compiled = self.ai_client.normalize_custom_strategy_compilation(
                        payload.get("compiled_config"),
                        open_logic=next_open_logic,
                        position_logic=next_position_logic,
                    )
                except RuntimeError as exc:
                    raise AuthError(str(exc) or "invalid_custom_strategy_compilation") from exc
                payload = {**payload, "_compiled_config": compiled}
        try:
            deployment = self.store.update_user_deployment_settings(
                user_id=int(user["id"]),
                deployment_id=deployment_id,
                payload=payload,
            )
        except RuntimeError as exc:
            self._raise_store_error(exc)
        if deployment.get("strategy_code") == "CUSTOM_AI_V1" and self.ai_client is not None:
            config = dict(deployment.get("config") or {})
            open_logic = str(config.get("open_logic") or "").strip()
            position_logic = str(config.get("position_logic") or "").strip()
            if len(open_logic) < 5 or len(position_logic) < 5:
                raise AuthError("custom_strategy_logic_required")
            previous_config = dict(previous.get("config") or {}) if previous else {}
            logic_changed = (
                open_logic != str(previous_config.get("open_logic") or "")
                or position_logic != str(previous_config.get("position_logic") or "")
            )
            if logic_changed and "_compiled_config" not in payload:
                selected_data = {
                    f"{prefix}_{field}": config.get(f"{prefix}_{field}")
                    for prefix in ("open", "position")
                    for field in ("data_type", "kline_count", "requested_kline_count")
                }
                compiled = self.ai_client.compile_custom_strategy(deployment)
                config.update(compiled)
                for prefix in ("open", "position"):
                    selected_type = str(selected_data.get(f"{prefix}_data_type") or "kline")
                    selected_count = int(
                        selected_data.get(f"{prefix}_requested_kline_count")
                        or selected_data.get(f"{prefix}_kline_count")
                        or 100
                    )
                    required_count = int(compiled.get(f"{prefix}_kline_count") or 100)
                    config[f"{prefix}_data_type"] = selected_type
                    config[f"{prefix}_requested_kline_count"] = selected_count
                    config[f"{prefix}_indicator_kline_count"] = required_count
                    config[f"{prefix}_kline_count"] = (
                        max(selected_count, required_count) if selected_type in {"kline", "both"} else 1
                    )
                deployment = self.store.upsert_web_deployment(
                    str(config.get("deployment_key") or ""),
                    user_id=str(user["id"]),
                    strategy_code="CUSTOM_AI_V1",
                    strategy_name=str(deployment.get("strategy_name") or "自定义策略"),
                    status=str(deployment.get("status") or "active"),
                    symbol=str(deployment.get("symbol") or "*"),
                    timeframe=str(deployment.get("timeframe") or "*"),
                    config=config,
                )
        return {"id": deployment["id"], "updated_at": deployment["updated_at"]}

    def preview_custom_strategy(self, token: str, *, payload: dict[str, Any]) -> dict[str, Any]:
        prepared = {**payload, "strategy_code": "CUSTOM_AI_V1"}
        deployment_id = str(payload.get("deployment_id") or "").strip()
        if deployment_id:
            user = self.me(token)
            existing = next(
                (item for item in self.store.list_web_deployments(str(user["id"])) if item["id"] == deployment_id),
                None,
            )
            if existing is None or existing.get("strategy_code") != "CUSTOM_AI_V1":
                raise AuthError("deployment_not_found", 404)
            existing_config = dict(existing.get("config") or {})
            for prefix in ("open", "position"):
                if str(prepared.get(f"{prefix}_ai_mode") or "official") == "custom":
                    prepared[f"{prefix}_ai_key"] = str(
                        prepared.get(f"{prefix}_ai_key") or existing_config.get(f"{prefix}_ai_key") or ""
                    )
        return self.create_strategy(token, payload=prepared, preview_only=True)

    def generate_custom_workflow_stage(self, token: str, *, payload: dict[str, Any]) -> dict[str, Any]:
        session_user = self.me(token)
        user = self.store.get_user(int(session_user["id"]))
        if user is None:
            raise AuthError("user_not_found", 404)
        if not bool(user.get("vip_active")):
            raise AuthError("vip_required", 403)
        if self.ai_client is None:
            raise AuthError("custom_strategy_compile_unavailable", 503)
        stage = str(payload.get("stage") or "").strip().lower()
        if stage not in {"open", "position"}:
            raise AuthError("invalid_workflow_stage")
        user_logic = str(payload.get("user_logic") or "").strip()
        if len(user_logic) < 5 or len(user_logic) > 12000:
            raise AuthError("custom_strategy_logic_required")
        raw_requirements = payload.get("data_requirements")
        requirements = dict(raw_requirements) if isinstance(raw_requirements, dict) else {}
        data_type = str(requirements.get("data_type") or "kline").strip().lower()
        try:
            kline_count = int(requirements.get("kline_count") or 100)
            call_value = float(requirements.get("call_value") or 1)
        except (TypeError, ValueError) as exc:
            raise AuthError("invalid_strategy_data_settings") from exc
        call_mode = str(requirements.get("call_mode") or "bar").strip().lower()
        if data_type not in {"kline", "screenshot", "both"} or not 1 <= kline_count <= 1000:
            raise AuthError("invalid_strategy_data_settings")
        if call_mode not in {"bar", "timer", "tick", "price_step"} or call_value < 0:
            raise AuthError("invalid_strategy_data_settings")

        workflow_endpoint = self.store.get_gl_ai_endpoint_by_model("qwen-plus")
        if workflow_endpoint is None:
            raise AuthError("workflow_generation_model_unavailable", 503)
        config: dict[str, Any] = {
            f"{stage}_ai_mode": "official",
            f"{stage}_ai_endpoint_id": str(workflow_endpoint.get("id") or ""),
            f"{stage}_ai_model": str(workflow_endpoint.get("model") or "qwen-plus"),
        }
        temporary = {
            "id": "",
            # Workflow generation is a platform-provided authoring feature.
            # Keep it out of the customer's balance ledger and usage details.
            "user_id": "",
            "strategy_code": "WORKFLOW_BUILDER",
            "strategy_name": "自定义AI策略流程生成",
            "config": config,
        }
        try:
            generated = self.ai_client.generate_custom_workflow_stage(
                temporary,
                stage=stage,
                user_logic=user_logic,
                data_requirements={
                    "data_type": data_type,
                    "kline_count": kline_count,
                    "call_mode": call_mode,
                    "call_value": call_value,
                },
            )
            return {**generated, "generation_model": "qwen-plus", "customer_billed": False}
        except RuntimeError as exc:
            raise AuthError(str(exc) or "workflow_generation_failed", 502) from exc

    def create_strategy(
        self,
        token: str,
        *,
        payload: dict[str, Any],
        preview_only: bool = False,
    ) -> dict[str, Any]:
        session_user = self.me(token)
        user = self.store.get_user(int(session_user["id"]))
        if user is None:
            raise AuthError("user_not_found", 404)
        if not bool(user.get("vip_active")):
            raise AuthError("vip_required", 403)
        is_existing_preview = preview_only and bool(str(payload.get("deployment_id") or "").strip())
        if not is_existing_preview and int(user.get("strategy_count") or 0) >= int(user.get("max_strategy_keys") or 0):
            raise AuthError("strategy_key_limit_reached", 409)
        strategy_code = str(payload.get("strategy_code") or "PA_AGENT_V1").strip()
        is_custom_strategy = strategy_code == "CUSTOM_AI_V1"
        logger.info(
            "create_strategy code=%s custom=%s workflow_type=%s workflow_keys=%s",
            strategy_code,
            is_custom_strategy,
            type(payload.get("workflow")).__name__,
            sorted(payload.get("workflow", {}).keys()) if isinstance(payload.get("workflow"), dict) else [],
        )
        strategy = None if is_custom_strategy else self.store.get_official_ai_strategy(strategy_code)
        if not is_custom_strategy and (strategy is None or not bool(strategy.get("enabled"))):
            raise AuthError("official_strategy_not_found", 404)
        open_logic = str(payload.get("open_logic") or "").strip()
        position_logic = str(payload.get("position_logic") or "").strip()
        ea_description = str(payload.get("ea_description") or "").strip()
        if is_custom_strategy and (len(open_logic) < 5 or len(position_logic) < 5):
            raise AuthError("custom_strategy_logic_required")
        if len(ea_description) > 1000:
            raise AuthError("invalid_strategy_description")
        custom_data: dict[str, Any] = {}
        if is_custom_strategy:
            for prefix in ("open", "position"):
                data_type = str(payload.get(f"{prefix}_data_type") or "kline").strip().lower()
                try:
                    kline_count = int(payload.get(f"{prefix}_kline_count") or 100)
                except (TypeError, ValueError) as exc:
                    raise AuthError("invalid_strategy_data_settings") from exc
                if data_type not in {"kline", "screenshot", "both"} or not 10 <= kline_count <= 1000:
                    raise AuthError("invalid_strategy_data_settings")
                custom_data[f"{prefix}_data_type"] = data_type
                custom_data[f"{prefix}_kline_count"] = kline_count
        default_config = dict(strategy.get("default_config") or {}) if strategy else {}
        allowed_options = {item["id"]: item for item in self.store.list_public_ai_model_options()["list"]}
        config: dict[str, Any] = {
            "strategy_type": "custom" if is_custom_strategy else "official",
            "open_data_type": (strategy.get("open_data_type") or "kline") if strategy else "kline",
            "open_kline_count": int(strategy.get("open_kline_count") or 100) if strategy else 100,
            "position_data_type": (strategy.get("position_data_type") or "kline") if strategy else "kline",
            "position_kline_count": int(strategy.get("position_kline_count") or 100) if strategy else 100,
            "call_mode": (strategy.get("call_mode") or "bar") if strategy else "bar",
            "call_val": float(strategy.get("call_value") or 1) if strategy else 1.0,
        }
        if is_custom_strategy:
            config.update({
                "open_logic": open_logic,
                "position_logic": position_logic,
                "ea_description": ea_description,
                "indicator_output_count": 100,
                "prompt_version": 1,
                "compile_status": "pending",
                **custom_data,
            })
        for prefix in ("open", "position"):
            mode = str(payload.get(f"{prefix}_ai_mode") or "official")
            if mode not in {"official", "custom"}:
                raise AuthError("invalid_ai_mode")
            config[f"{prefix}_ai_mode"] = mode
            if mode == "official":
                endpoint_id = str(payload.get(f"{prefix}_ai_endpoint_id") or (strategy.get(f"{prefix}_ai_endpoint_id") if strategy else "") or "")
                option = allowed_options.get(endpoint_id)
                if option is None:
                    raise AuthError("invalid_ai_endpoint")
                config[f"{prefix}_ai_endpoint_id"] = endpoint_id
                config[f"{prefix}_ai_model"] = str(option.get("model") or "")
                config[f"{prefix}_ai_base_url"] = ""
                config[f"{prefix}_ai_key"] = ""
                config[f"{prefix}_ai_vision_verified"] = bool(option.get("supports_vision"))
            else:
                base_url = str(payload.get(f"{prefix}_ai_base_url") or "").strip().rstrip("/")
                model = str(payload.get(f"{prefix}_ai_model") or "").strip()
                api_key = str(payload.get(f"{prefix}_ai_key") or "").strip()
                if not base_url or not model or not api_key:
                    raise AuthError("custom_ai_config_required")
                config[f"{prefix}_ai_endpoint_id"] = ""
                config[f"{prefix}_ai_model"] = model
                config[f"{prefix}_ai_base_url"] = base_url
                config[f"{prefix}_ai_key"] = api_key
                config[f"{prefix}_ai_vision_verified"] = bool(payload.get(f"{prefix}_ai_vision_verified", False))
            if is_custom_strategy and custom_data.get(f"{prefix}_data_type") in {"screenshot", "both"}:
                if not bool(config.get(f"{prefix}_ai_vision_verified")):
                    raise AuthError("ai_vision_test_required")
        size_mode = str(payload.get("position_size_mode") or default_config.get("position_sizing_mode") or "fixed")
        raw_risk_mode = str(payload.get("risk_base_mode") or default_config.get("risk_mode") or "fixed_stop_amount")
        risk_mode = "balance_percent" if raw_risk_mode == "balance_percent" else "fixed_loss"
        try:
            fixed_volume = float(payload.get("fixed_volume", default_config.get("fixed_lot", 0.01)))
            risk_amount = float(payload.get("risk_amount", default_config.get("max_stop_amount", 100)))
            risk_percent = float(payload.get("risk_percent", default_config.get("risk_percent", 1)))
            max_positions = int(payload.get("max_positions", default_config.get("max_positions", 1)))
        except (TypeError, ValueError) as exc:
            raise AuthError("invalid_strategy_settings") from exc
        if size_mode not in {"fixed", "risk"} or fixed_volume < 0 or risk_amount < 0 or risk_percent <= 0 or max_positions < 1:
            raise AuthError("invalid_strategy_settings")
        config.update({
            "position_size_mode": size_mode,
            "fixed_volume": fixed_volume,
            "lot": fixed_volume,
            "risk_base_mode": risk_mode,
            "risk_amount": risk_amount,
            "risk_percent": risk_percent,
            "max_positions": max_positions,
            "allow_add": bool(payload.get("allow_add", default_config.get("allow_add_position", False))),
            "ai_user_configured": True,
        })
        visual_compilation: dict[str, Any] | None = None
        if is_custom_strategy and payload.get("workflow") is not None:
            if self.ai_client is None:
                raise AuthError("custom_strategy_compile_unavailable", 503)
            try:
                visual_compilation = self.ai_client.compile_custom_workflow(
                    payload.get("workflow"),
                    open_logic=open_logic,
                    position_logic=position_logic,
                )
            except (RuntimeError, ValueError) as exc:
                raise AuthError("invalid_custom_strategy_workflow") from exc
        if preview_only:
            if not is_custom_strategy or self.ai_client is None:
                raise AuthError("custom_strategy_compile_unavailable", 503)
            if visual_compilation is not None:
                self._apply_custom_compilation(config, visual_compilation, custom_data)
                fields = (
                    "summary", "open_prompt_template", "position_prompt_template",
                    "open_indicators", "position_indicators", "open_rule_plan", "position_rule_plan",
                    "rule_engine_version", "open_kline_count",
                    "position_kline_count", "open_requested_kline_count",
                    "position_requested_kline_count", "open_indicator_kline_count",
                    "position_indicator_kline_count", "open_data_type", "position_data_type",
                    "unsupported_indicators", "unsupported_conditions", "unsupported_condition_count",
                    "visual_conditions", "warnings", "prompt_version", "compile_status",
                )
                return {key: config[key] for key in fields}
            temporary = {
                "id": "",
                "user_id": str(user["id"]),
                "strategy_code": strategy_code,
                "strategy_name": str(payload.get("name") or "自定义策略").strip(),
                "config": config,
            }
            try:
                compiled = self.ai_client.compile_custom_strategy(temporary)
            except RuntimeError as exc:
                raise AuthError(str(exc) or "custom_strategy_compile_failed", 502) from exc
            self._apply_custom_compilation(config, compiled, custom_data)
            fields = (
                "summary", "open_prompt_template", "position_prompt_template",
                "open_indicators", "position_indicators", "open_rule_plan", "position_rule_plan",
                "rule_engine_version", "open_kline_count",
                "position_kline_count", "open_requested_kline_count",
                "position_requested_kline_count", "open_indicator_kline_count",
                "position_indicator_kline_count", "open_data_type", "position_data_type",
                "unsupported_indicators", "unsupported_conditions", "unsupported_condition_count",
                "visual_conditions", "warnings", "prompt_version", "compile_status",
            )
            return {key: config[key] for key in fields}

        if is_custom_strategy:
            if self.ai_client is None:
                raise AuthError("custom_strategy_compile_unavailable", 503)
            if visual_compilation is not None:
                self._apply_custom_compilation(config, visual_compilation, custom_data)
                config["workflow"] = visual_compilation["workflow"]
                config["compiled_workflow"] = visual_compilation["compiled_workflow"]
            else:
                try:
                    compiled = self.ai_client.normalize_custom_strategy_compilation(
                        payload.get("compiled_config"),
                        open_logic=open_logic,
                        position_logic=position_logic,
                    )
                except RuntimeError as exc:
                    raise AuthError(str(exc) or "invalid_custom_strategy_compilation") from exc
                self._apply_custom_compilation(config, compiled, custom_data)
        raw_key = "gl_" + secrets.token_urlsafe(18).replace("-", "").replace("_", "")
        config["deployment_key"] = raw_key
        deployment = self.store.upsert_web_deployment(
            raw_key,
            user_id=str(user["id"]),
            strategy_code=strategy_code,
            strategy_name=str(payload.get("name") or (strategy.get("name") if strategy else "自定义策略") or "").strip(),
            status="active" if str(payload.get("status") or "active") == "active" else "paused",
            symbol="*",
            timeframe="*",
            config=config,
        )
        mt_login = str(payload.get("mt_login") or "").strip()
        if mt_login:
            deployment = self.store.set_deployment_login(raw_key, mt_login) or deployment
        return {"id": deployment["id"], "deployment_key": raw_key, "status": deployment["status"]}

    @staticmethod
    def _apply_custom_compilation(
        config: dict[str, Any],
        compiled: dict[str, Any],
        custom_data: dict[str, Any],
    ) -> None:
        config.update(compiled)
        config.setdefault("open_rule_plan", {"version": 1, "mode": "ai", "rules": []})
        config.setdefault("position_rule_plan", {"version": 1, "mode": "ai", "rules": []})
        config.setdefault("rule_engine_version", 1)
        config.setdefault("unsupported_conditions", [])
        config["unsupported_condition_count"] = len(config["unsupported_conditions"])
        config.setdefault("visual_conditions", [])
        for prefix in ("open", "position"):
            selected_type = str(custom_data[f"{prefix}_data_type"])
            selected_count = int(custom_data[f"{prefix}_kline_count"])
            required_count = int(compiled.get(f"{prefix}_kline_count") or 100)
            config[f"{prefix}_data_type"] = selected_type
            config[f"{prefix}_requested_kline_count"] = selected_count
            config[f"{prefix}_indicator_kline_count"] = required_count
            config[f"{prefix}_kline_count"] = (
                max(selected_count, required_count) if selected_type in {"kline", "both"} else 1
            )

    def usage(
        self,
        token: str,
        *,
        page: int,
        size: int,
        model_id: str = "",
        deployment_id: str = "",
        start_at: str = "",
        end_at: str = "",
    ) -> dict[str, Any]:
        user = self.me(token)
        return self.store.list_user_ai_usage(
            user_id=int(user["id"]),
            page=page,
            size=size,
            model_id=str(model_id or "").strip(),
            deployment_id=str(deployment_id or "").strip(),
            start_at=str(start_at or "").strip(),
            end_at=str(end_at or "").strip(),
        )

    def usage_screenshot_preview(self, token: str, *, usage_id: str) -> dict[str, Any]:
        user = self.me(token)
        preview_id = self.store.get_user_ai_usage_screenshot_preview_id(
            user_id=int(user["id"]),
            usage_id=str(usage_id or "").strip(),
        )
        if not preview_id:
            raise AuthError("screenshot_preview_not_found", 404)
        try:
            return load_preview(preview_id)
        except ScreenshotError as exc:
            raise AuthError(str(exc), 404) from exc

    def orders(
        self,
        token: str,
        *,
        page: int,
        size: int,
        deployment_id: str = "",
        symbol: str = "",
        start_at: str = "",
        end_at: str = "",
    ) -> dict[str, Any]:
        user = self.me(token)
        return self.store.list_user_orders(
            user_id=int(user["id"]),
            page=page,
            size=size,
            deployment_id=str(deployment_id or "").strip(),
            symbol=str(symbol or "").strip(),
            start_at=str(start_at or "").strip(),
            end_at=str(end_at or "").strip(),
        )

    def update_strategy_status(self, token: str, *, deployment_id: str, status: str) -> dict[str, Any]:
        user = self.me(token)
        try:
            deployment = self.store.update_user_deployment_status(
                user_id=int(user["id"]),
                deployment_id=str(deployment_id or "").strip(),
                status=str(status or "").strip(),
            )
        except RuntimeError as exc:
            self._raise_store_error(exc)
        return {"id": deployment["id"], "status": deployment["status"], "updated_at": deployment["updated_at"]}

    def delete_strategy(self, token: str, *, deployment_id: str) -> None:
        user = self.me(token)
        try:
            self.store.delete_user_deployment(
                user_id=int(user["id"]),
                deployment_id=str(deployment_id or "").strip(),
            )
        except RuntimeError as exc:
            self._raise_store_error(exc)

    def logout(self, token: str) -> None:
        self.store.revoke_session(self._token_hash(token))

    def update_profile(self, token: str, *, nickname: str) -> dict[str, Any]:
        user = self.me(token)
        normalized = str(nickname or "").strip()
        if len(normalized) > 100:
            raise AuthError("invalid_nickname")
        try:
            return self.store.update_user_nickname(int(user["id"]), normalized)
        except RuntimeError as exc:
            self._raise_store_error(exc)

    def change_password(self, token: str, *, current_password: str, new_password: str) -> dict[str, Any]:
        user = self.me(token)
        self._password(new_password)
        row = self.store.get_auth_user_by_id(int(user["id"]))
        if row is None:
            raise AuthError("user_not_found", 404)
        existing_hash = str(row.get("password_hash") or "")
        if existing_hash and not verify_password(current_password, existing_hash):
            raise AuthError("invalid_current_password", 401)
        updated = self.store.update_user_password(int(user["id"]), hash_password(new_password))
        self.store.revoke_other_user_sessions(int(user["id"]), self._token_hash(token))
        return updated

    def send_current_email_code(self, token: str) -> dict[str, Any]:
        user = self.me(token)
        current_email = self._email(str(user.get("email") or ""))
        self._send_code(current_email, "change_email_old")
        return {"email": current_email, "expires_in": self.settings.verification_minutes * 60}

    def send_change_email_code(self, token: str, *, email: str) -> dict[str, Any]:
        user = self.me(token)
        normalized = self._email(email)
        if normalized == str(user.get("email") or "").lower():
            raise AuthError("email_unchanged")
        existing = self.store.get_auth_user_by_email(normalized)
        if existing and int(existing["id"]) != int(user["id"]):
            raise AuthError("email_already_registered", 409)
        self._send_code(normalized, "change_email_new")
        return {"email": normalized, "expires_in": self.settings.verification_minutes * 60}

    def verify_change_email(
        self,
        token: str,
        *,
        email: str,
        current_email_code: str,
        new_email_code: str,
    ) -> dict[str, Any]:
        user = self.me(token)
        current_email = self._email(str(user.get("email") or ""))
        normalized = self._email(email)
        self._consume_code(current_email, "change_email_old", current_email_code)
        self._consume_code(normalized, "change_email_new", new_email_code)
        try:
            return self.store.update_user_email(int(user["id"]), normalized)
        except RuntimeError as exc:
            self._raise_store_error(exc)

    def list_sessions(self, token: str) -> list[dict[str, Any]]:
        user = self.me(token)
        return self.store.list_user_sessions(int(user["id"]), self._token_hash(token))

    def revoke_session_by_id(self, token: str, *, session_id: str) -> dict[str, Any]:
        user = self.me(token)
        sessions = self.store.list_user_sessions(int(user["id"]), self._token_hash(token))
        target = next((item for item in sessions if item["id"] == session_id), None)
        if target is None:
            raise AuthError("session_not_found", 404)
        self.store.revoke_user_session(int(user["id"]), session_id)
        return {"revoked": True, "current": bool(target["is_current"])}

    def logout_all(self, token: str) -> None:
        user = self.me(token)
        self.store.revoke_all_user_sessions(int(user["id"]))

    def resend_code(self, *, email: str, purpose: str) -> dict[str, Any]:
        normalized = self._email(email)
        if purpose not in {"register", "login", "reset_password"}:
            raise AuthError("invalid_verification_purpose")
        row = self.store.get_auth_user_by_email(normalized)
        if not row:
            raise AuthError("user_not_found", 404)
        if str(row.get("status") or "") == "disabled":
            raise AuthError("user_disabled", 403)
        self._send_code(normalized, purpose)
        return {"email": normalized, "expires_in": self.settings.verification_minutes * 60}

    def _send_code(self, email: str, purpose: str) -> None:
        latest = self.store.latest_verification_created_at(email=email, purpose=purpose)
        if latest:
            try:
                created = datetime.fromisoformat(latest.replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                remaining = 60 - int((datetime.now(timezone.utc) - created).total_seconds())
                if remaining > 0:
                    raise AuthError("verification_too_frequent", 429)
            except ValueError:
                pass
        if not self.settings.auth_secret:
            raise AuthError("auth_not_configured", 503)
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = self._code_hash(email, purpose, code)
        try:
            self.email_service.send_verification_code(
                to=email,
                code=code,
                purpose=purpose,
                minutes=self.settings.verification_minutes,
            )
        except RuntimeError as exc:
            raise AuthError(str(exc), 503) from exc
        except Exception as exc:
            raise AuthError("mail_send_failed", 502) from exc
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=self.settings.verification_minutes)).isoformat()
        self.store.save_verification_code(
            email=email,
            purpose=purpose,
            code_hash=code_hash,
            expires_at=expires_at,
        )

    def _consume_code(self, email: str, purpose: str, code: str) -> None:
        if not re.fullmatch(r"\d{6}", code or ""):
            raise AuthError("invalid_verification_code")
        result = self.store.consume_verification_code(
            email=email,
            purpose=purpose,
            code_hash=self._code_hash(email, purpose, code),
        )
        if result != "ok":
            raise AuthError(result if result != "invalid" else "invalid_verification_code")

    def _session(self, user: dict[str, Any]) -> dict[str, Any]:
        if not str(user.get("email") or "").strip():
            raise AuthError("invalid_email")
        raw_token = secrets.token_urlsafe(48)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=self.settings.session_days)).isoformat()
        self.store.create_user_session(
            user_id=int(user["id"]),
            token_hash=self._token_hash(raw_token),
            expires_at=expires_at,
        )
        refreshed = self.store.get_user(int(user["id"])) or user
        return {"token": raw_token, "expires_at": expires_at, "user": refreshed}

    def _token_hash(self, token: str) -> str:
        if not token:
            raise AuthError("invalid_session", 401)
        return hash_auth_value(f"session|{token}", self.settings.auth_secret)

    def _code_hash(self, email: str, purpose: str, code: str) -> str:
        return hash_auth_value(f"code|{email}|{purpose}|{code}", self.settings.auth_secret)

    @staticmethod
    def _email(email: str) -> str:
        normalized = (email or "").strip().lower()
        if len(normalized) > 255 or not EMAIL_PATTERN.fullmatch(normalized):
            raise AuthError("invalid_email")
        return normalized

    @staticmethod
    def _password(password: str) -> None:
        if len(password or "") < 8 or len(password) > 128:
            raise AuthError("invalid_password")
        if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
            raise AuthError("weak_password")

    @staticmethod
    def _assert_login_allowed(row: dict[str, Any]) -> None:
        status = str(row.get("status") or "")
        if status == "disabled":
            raise AuthError("user_disabled", 403)
        if status != "active" or not row.get("email_verified_at"):
            raise AuthError("email_not_verified", 403)

    @staticmethod
    def _raise_store_error(exc: RuntimeError) -> None:
        code = str(exc)
        statuses = {
            "email_already_registered": 409,
            "user_email_exists": 409,
            "user_disabled": 403,
            "user_not_found": 404,
            "deployment_not_found": 404,
            "invalid_ai_mode": 400,
            "invalid_ai_endpoint": 400,
            "custom_ai_config_required": 400,
            "ai_vision_test_required": 400,
            "invalid_strategy_settings": 400,
            "custom_strategy_logic_required": 400,
            "invalid_strategy_data_settings": 400,
            "official_strategy_not_found": 404,
            "vip_required": 403,
            "strategy_key_limit_reached": 409,
            "invalid_invite_code": 400,
            "agent_required": 403,
        }
        raise AuthError(code, statuses.get(code, 400)) from exc

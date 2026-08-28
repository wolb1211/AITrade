from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
from typing import Any

from PIL import Image

from app.models import AccountIdentity, OpenEvaluateRequest
from app.services.ai_service import AiCallResult, AiDecisionClient
from app.services import screenshot_preview
from app.services.screenshot_preview import load_preview, prepare_screenshot
from app.store import SqliteStore
from app.models import UsageSummary


_PNG_BUFFER = BytesIO()
Image.new("RGB", (2, 2), "red").save(_PNG_BUFFER, format="PNG")
_PNG_BYTES = _PNG_BUFFER.getvalue()


def test_screenshot_preview_accepts_base64_and_expires_outside_usage_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(screenshot_preview, "_PREVIEW_ROOT", tmp_path / "screenshots")
    data_url, metadata = prepare_screenshot("", base64.b64encode(_PNG_BYTES).decode("ascii"))

    assert data_url.startswith("data:image/jpeg;base64,")
    assert metadata["size_bytes"] == len(_PNG_BYTES)
    loaded = load_preview(metadata["preview_id"])
    assert loaded["data_url"].startswith("data:image/png;base64,")


def test_custom_open_decision_passes_screenshot_to_multimodal_ai(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "screenshot-ai.db")
    store.initialize()
    client = AiDecisionClient(store)
    captured: dict[str, Any] = {}

    def fake_chat_json(**kwargs: Any) -> AiCallResult:
        captured.update(kwargs)
        return AiCallResult(
            content={"should_open": False, "direction": None, "reason": "未触发", "analysis": "未触发"},
            usage=UsageSummary(ai_called=True),
        )

    client._chat_json = fake_chat_json  # type: ignore[method-assign]
    request = OpenEvaluateRequest(
        deployment_key="gl_screenshot_test",
        request_id="screenshot-request-001",
        account=AccountIdentity(login="10001"),
        symbol="XAUUSD",
        timeframe="M5",
        bar_time=1,
        bid=100,
        ask=100.1,
        spread_points=0.1,
        balance=10000,
        equity=10000,
        data_type="screenshot",
        screenshot_data_url="data:image/png;base64,AAAA",
        screenshot_metadata={"preview_id": "a" * 32, "sha256": "b" * 64},
    )
    client.custom_open_decision(
        deployment={
            "strategy_name": "截图策略",
            "config": {
                "open_data_type": "screenshot",
                "visual_conditions": [
                    {"stage": "open", "code": "hg_color", "text": "HG颜色"},
                    {"stage": "position", "code": "hg_exit", "text": "HG反转"},
                ],
            },
        },
        request_payload=request,
    )

    assert captured["user_image_url"] == request.screenshot_data_url
    assert captured["user_payload"]["screenshot"]["preview_id"] == "a" * 32
    assert captured["user_payload"]["visual_conditions"] == [
        {"code": "hg_color", "text": "HG颜色"}
    ]


def test_user_screenshot_preview_lookup_is_scoped_to_usage_owner(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "screenshot-owner.db")
    store.initialize()
    owner = store.save_user({"email": "screenshot-owner@example.com", "status": "active"})
    other = store.save_user({"email": "screenshot-other@example.com", "status": "active"})
    preview_id = "c" * 32
    usage = store.save_ai_usage_log({
        "user_id": str(owner["id"]),
        "strategy_code": "CUSTOM_AI_V1",
        "endpoint": "open",
        "provider_id": "vision-provider",
        "model_id": "vision-model",
        "request_snapshot": json.dumps({
            "messages": [
                {"role": "system", "content": "test"},
                {"role": "user", "content": json.dumps({"screenshot": {"preview_id": preview_id}})},
            ]
        }),
    })

    assert store.get_user_ai_usage_screenshot_preview_id(
        user_id=int(owner["id"]), usage_id=usage["id"]
    ) == preview_id
    assert store.get_user_ai_usage_screenshot_preview_id(
        user_id=int(other["id"]), usage_id=usage["id"]
    ) == ""

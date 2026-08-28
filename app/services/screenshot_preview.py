from __future__ import annotations

import base64
import binascii
import json
import re
from io import BytesIO
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
PREVIEW_TTL_HOURS = 6
_PREVIEW_ROOT = Path(__file__).resolve().parents[2] / "runtime" / "screenshot_previews"
_MIME_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class ScreenshotError(ValueError):
    pass


def prepare_screenshot(mime_type: str, encoded: str) -> tuple[str, dict[str, Any]]:
    raw_text = str(encoded or "").strip()
    declared_mime = str(mime_type or "").strip().lower()
    match = re.match(r"^data:([^;,]+);base64,(.*)$", raw_text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        declared_mime = str(match.group(1)).strip().lower()
        raw_text = match.group(2)
    compact = re.sub(r"\s+", "", raw_text)
    try:
        image_bytes = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ScreenshotError("invalid_screenshot_base64") from exc
    if not image_bytes:
        raise ScreenshotError("empty_screenshot")
    if len(image_bytes) > MAX_SCREENSHOT_BYTES:
        raise ScreenshotError("screenshot_too_large")
    if not declared_mime:
        declared_mime = _detect_mime(image_bytes)
    if declared_mime == "image/jpg":
        declared_mime = "image/jpeg"
    if declared_mime not in _MIME_EXTENSIONS:
        raise ScreenshotError("unsupported_screenshot_type")
    _validate_signature(declared_mime, image_bytes)

    digest = sha256(image_bytes).hexdigest()
    preview_id = digest[:32]
    _PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    _cleanup_expired()
    image_path = _PREVIEW_ROOT / f"{preview_id}.{_MIME_EXTENSIONS[declared_mime]}"
    metadata_path = _PREVIEW_ROOT / f"{preview_id}.json"
    if not image_path.exists():
        image_path.write_bytes(image_bytes)
    metadata = {
        "preview_id": preview_id,
        "mime_type": declared_mime,
        "size_bytes": len(image_bytes),
        "sha256": digest,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    ai_mime, ai_bytes = _normalize_for_ai(declared_mime, image_bytes)
    ai_encoded = base64.b64encode(ai_bytes).decode("ascii")
    return f"data:{ai_mime};base64,{ai_encoded}", metadata


def load_preview(preview_id: str) -> dict[str, Any]:
    normalized = str(preview_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        raise ScreenshotError("screenshot_preview_not_found")
    metadata_path = _PREVIEW_ROOT / f"{normalized}.json"
    if not metadata_path.exists():
        raise ScreenshotError("screenshot_preview_not_found")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScreenshotError("screenshot_preview_not_found") from exc
    created_at = datetime.fromisoformat(str(metadata.get("created_at") or "").replace("Z", "+00:00"))
    if created_at < datetime.now(timezone.utc) - timedelta(hours=PREVIEW_TTL_HOURS):
        raise ScreenshotError("screenshot_preview_expired")
    mime_type = str(metadata.get("mime_type") or "")
    image_path = _PREVIEW_ROOT / f"{normalized}.{_MIME_EXTENSIONS.get(mime_type, '')}"
    if not image_path.exists():
        raise ScreenshotError("screenshot_preview_not_found")
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return {**metadata, "data_url": f"data:{mime_type};base64,{encoded}"}


def _validate_signature(mime_type: str, content: bytes) -> None:
    valid = (
        mime_type == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n")
        or mime_type == "image/jpeg" and content.startswith(b"\xff\xd8\xff")
        or mime_type == "image/webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
    )
    if not valid:
        raise ScreenshotError("screenshot_type_mismatch")


def _detect_mime(content: bytes) -> str:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _normalize_for_ai(mime_type: str, content: bytes) -> tuple[str, bytes]:
    """Use JPEG for provider compatibility while retaining the original preview file."""
    if mime_type == "image/jpeg":
        return mime_type, content
    try:
        with Image.open(BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=92, optimize=True)
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ScreenshotError("invalid_screenshot_image") from exc
    return "image/jpeg", output.getvalue()


def _cleanup_expired() -> None:
    threshold = datetime.now(timezone.utc) - timedelta(hours=PREVIEW_TTL_HOURS)
    for metadata_path in _PREVIEW_ROOT.glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(str(metadata.get("created_at") or "").replace("Z", "+00:00"))
            if created_at >= threshold:
                continue
            preview_id = metadata_path.stem
            for image_path in _PREVIEW_ROOT.glob(f"{preview_id}.*"):
                image_path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError):
            continue

"""Vision sidecar helpers for media preprocessing.

The main agent can stay on a strong text/tool model while image attachments
are summarized by a smaller local vision server first. The default target is
the Gemma vision sidecar used in the Ziggy deployment, exposed as an
OpenAI-compatible llama-server on ``127.0.0.1:8002``.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from nanobot.utils.helpers import detect_image_mime

_DEFAULT_BASE_URL = "http://127.0.0.1:8002/v1"
_DEFAULT_MODEL = "gemma-4-12b-vision"
_DEFAULT_TIMEOUT_S = 45.0
_PROMPT = (
    "Describe this image for a downstream text-only agent. "
    "Be concise but include all visible text, UI elements, objects, people, "
    "spatial relationships, numbers, charts, errors, and anything relevant "
    "to answering the user's request. Do not invent hidden context."
)


def vision_preprocess_enabled() -> bool:
    raw = os.environ.get("NANOBOT_VISION_PREPROCESS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _timeout_s() -> float:
    raw = os.environ.get("NANOBOT_VISION_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid NANOBOT_VISION_TIMEOUT_S={!r}", raw)
        return _DEFAULT_TIMEOUT_S
    return max(1.0, value)


def _image_data_url(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("vision: failed to read image {}: {}", path, exc)
        return None
    mime = detect_image_mime(raw) or mimetypes.guess_type(str(path))[0]
    if not mime or not mime.startswith("image/"):
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _extract_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return "\n".join(p.strip() for p in parts if p.strip()).strip()
    text = first.get("text")
    return text.strip() if isinstance(text, str) else ""


async def describe_images(paths: list[str], *, prompt: str = _PROMPT) -> str:
    """Return a text description for image paths using the local vision sidecar.

    Empty string means "no usable description"; callers should then fall back
    to their previous multimodal path.
    """
    if not paths or not vision_preprocess_enabled():
        return ""

    base_url = os.environ.get("NANOBOT_VISION_API_BASE", _DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("NANOBOT_VISION_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    api_key = os.environ.get("NANOBOT_VISION_API_KEY", "no-key")
    descriptions: list[str] = []

    async with httpx.AsyncClient(timeout=_timeout_s()) as client:
        for index, raw_path in enumerate(paths, 1):
            path = Path(raw_path)
            data_url = _image_data_url(path)
            if data_url is None:
                continue
            body = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                "temperature": 0.0,
                "max_tokens": 700,
            }
            try:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
                response.raise_for_status()
                text = _extract_text(response.json())
            except Exception as exc:
                logger.warning("vision: sidecar description failed for {}: {}", path, exc)
                return ""
            if text:
                descriptions.append(f"Image {index} ({path.name}): {text}")

    if not descriptions:
        return ""
    return "[Vision analysis from local Gemma sidecar]\n" + "\n\n".join(descriptions)

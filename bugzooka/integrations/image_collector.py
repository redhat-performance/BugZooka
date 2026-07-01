"""Per-request collector for images returned by MCP tool calls.

Uses a contextvars.ContextVar so the collector is implicitly available
to invoke_mcp_tool() without threading it through every function signature.
The Slack handler sets/clears the collector around each request.
"""

import base64
import contextvars
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_current_collector: contextvars.ContextVar[
    Optional["ImageCollector"]
] = contextvars.ContextVar("_current_collector", default=None)


def get_collector() -> Optional["ImageCollector"]:
    return _current_collector.get()


def set_collector(collector: Optional["ImageCollector"]) -> contextvars.Token:
    return _current_collector.set(collector)


def reset_collector(token: contextvars.Token) -> None:
    _current_collector.reset(token)


_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


class ImageCollector:
    """Collects base64 images from MCP tool results during a single request."""

    def __init__(self):
        self._images: list[dict] = []

    def add_image(self, base64_data: str, mime_type: str, tool_name: str) -> None:
        ext = _MIME_EXTENSIONS.get(mime_type, "png")
        self._images.append(
            {
                "base64_data": base64_data,
                "mime_type": mime_type,
                "tool_name": tool_name,
                "filename": f"{tool_name}_{len(self._images)}.{ext}",
            }
        )
        logger.info("Collected image from tool %s (%s)", tool_name, mime_type)

    def get_images(self) -> list[dict]:
        return list(self._images)

    def has_images(self) -> bool:
        return len(self._images) > 0

    def decode_image(self, image: dict) -> bytes:
        return base64.b64decode(image["base64_data"])

    def clear(self) -> None:
        self._images.clear()

"""Generate images through the active Responses connection's Images API."""

from __future__ import annotations

import asyncio
import base64
import tempfile
from pathlib import Path
from uuid import uuid4

from langchain_core.tools import BaseTool, tool

from .. import paths
from ..files import get_registry
from ..files.images import encode_images_marker
from ..lang import t

IMAGE_TOOL_NAME = "generate_image"
# Magpie's image requests can take several minutes before returning a body.
_IMAGE_TIMEOUT_S = 300.0
_MAX_ENCODED_BYTES = 44 * 1024 * 1024


def _save_image(encoded: str | None, directory: Path) -> Path:
    invalid_image = t(
        "The Images API did not return a supported base64 image.",
        "生图接口没有返回受支持的 Base64 图片。",
    )
    if not isinstance(encoded, str) or not encoded or len(encoded) > _MAX_ENCODED_BYTES:
        raise ValueError(invalid_image)
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise ValueError(invalid_image) from None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise ValueError(invalid_image)

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"generated-{uuid4().hex}{suffix}"
    file = tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False)
    temp = Path(file.name)
    try:
        with file:
            file.write(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def build_image_tools(project_slug: str | None, session_id: str | None) -> list[BaseTool]:
    if not project_slug or not session_id:
        return []

    @tool(IMAGE_TOOL_NAME)
    async def generate_image(prompt: str) -> str:
        """Generate a new image from a text prompt when the user asks for one.

        The image appears in the conversation after the call succeeds.
        """
        from ..server_shared import _SESSIONS

        model = (_SESSIONS.get(session_id) or {}).get("model")
        model_id = getattr(model, "image_generation_model", "")
        if not model_id:
            raise ValueError(t(
                "Enable image generation for this session's Responses connection in Model API settings.",
                "请在「模型 API」设置中为当前会话的 Responses 连接开启生图。",
            ))
        if not prompt.strip():
            raise ValueError(t("Enter an image prompt.", "请输入生图描述。"))

        # Reuse the connection's URL, authentication and session headers. A
        # transport failure may occur after the generation has been billed.
        client = model.root_async_client.with_options(
            timeout=max(_IMAGE_TIMEOUT_S, model.request_timeout or 0),
            max_retries=0,
        )
        reply = await client.images.generate(model=model_id, prompt=prompt, n=1)
        encoded = reply.data[0].b64_json if reply.data else None
        directory = paths.session_files_dir(project_slug, session_id) / "generated"
        path = await asyncio.to_thread(_save_image, encoded, directory)
        get_registry(project_slug).register(path.name, str(path), kind="image", session_id=session_id)
        result = t("Image generated and attached.", "图片已生成并附在对话中。")
        return f"{result}\n{encode_images_marker([str(path)])}"

    return [generate_image]

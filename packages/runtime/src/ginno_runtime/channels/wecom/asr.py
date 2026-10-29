"""企微语音转写：千问云 ASR（录音文件转写，chat completions + input_audio）。

对接 docs/developer-guides/speech/asr（qwen3-asr-flash 系，含
qwen-audio-3.0-asr-flash）：OpenAI 兼容 ``POST {base}/chat/completions``，
user 消息携带 ``input_audio``（base64 data URL，≤10MB），识别文本从
``choices[0].message.content`` 取。配置在企微连接器（asr_base_url /
asr_api_key / asr_model），典型 base：
``https://dashscope.aliyuncs.com/compatible-mode/v1``。

不新增 SDK 依赖，直接 httpx。
"""

from __future__ import annotations

import base64

import httpx

_MIME_BY_FORMAT = {
    "wav": "audio/wav",
    "amr": "audio/amr",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "flac": "audio/flac",
}


def mime_for_format(fmt: str) -> str:
    """企微 voice.format → data URL 的 MIME；未知格式按 amr 兜底（企微
    语音传统容器）。"""
    return _MIME_BY_FORMAT.get(fmt.strip().lstrip(".").lower(), "audio/amr")


async def transcribe_audio(data: bytes, *, fmt: str, base_url: str,
                           api_key: str, model: str,
                           http: httpx.AsyncClient) -> str:
    """转写一段音频，返回文本（可能为空串——静音/不可识别）。

    失败抛异常由调用方定格提示；语言不指定（模型自动识别）。
    """
    data_uri = ("data:" + mime_for_format(fmt) + ";base64,"
                + base64.b64encode(data).decode("ascii"))
    resp = await http.post(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "input_audio",
                     "input_audio": {"data": data_uri}},
                ],
            }],
        },
    )
    resp.raise_for_status()
    payload = resp.json() or {}
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"asr response missing choices: {payload}")
    content = (choices[0].get("message") or {}).get("content")
    # 兼容两种形状：纯字符串 / content blocks 列表
    if isinstance(content, list):
        content = "".join(str(b.get("text") or "") for b in content
                          if isinstance(b, dict))
    return str(content or "").strip()

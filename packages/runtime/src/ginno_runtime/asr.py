"""语音转写（ASR）：模型配置里的 kind="asr" 卡片 + OpenAI 兼容云端点。

对接千问云「录音文件转写」（docs/developer-guides/speech/asr，qwen3-asr-flash
系）：OpenAI 兼容 ``POST {base}/chat/completions``，user 消息携带
``input_audio``（base64 data URL，≤10MB），识别文本从
``choices[0].message.content`` 取。配置不再是渠道私有字段，而是「设置 →
模型 API」里的 asr 卡片（providers.py CONFIG_DEFAULTS["kind"]）；典型 base：
``https://dashscope.aliyuncs.com/compatible-mode/v1``。

消费方按 id 取配置（:func:`resolve_asr_config`），如后续钉钉语音轨；企微
智能机器人的 voice 消息不带音频文件（服务端已转写），不走本模块。
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
    """音频容器名 → data URL 的 MIME；未知格式按 amr 兜底（企微语音
    传统容器）。"""
    return _MIME_BY_FORMAT.get(fmt.strip().lstrip(".").lower(), "audio/amr")


def resolve_asr_config(config_id: str) -> dict | None:
    """按 id 取一张启用的 asr 模型配置卡；不是 asr/未启用/不存在 → None。

    返回 {id, base_url, api_key, model}（model 取 default_model → models[0]）。
    """
    from . import providers as prov_mod

    for cfg in prov_mod.load_configs():
        if cfg.get("id") == config_id and cfg.get("kind") == "asr":
            if not cfg.get("enabled"):
                return None
            models = cfg.get("models") or []
            return {
                "id": cfg["id"],
                "base_url": str(cfg.get("base_url") or ""),
                "api_key": str(cfg.get("api_key") or ""),
                "model": cfg.get("default_model") or (models[0] if models else ""),
            }
    return None


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

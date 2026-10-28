"""企业微信智能机器人媒体收发（P1 文件上下行）。

- 下行（F6 等价）：消息回调里的 image/file 带 ``{url, aeskey}``，url 5 分钟
  有效；GET 后 AES-256-CBC 解密（key=base64(aeskey) 32B，iv=key[:16]，
  PKCS#7 去填充，失败容忍原文——部分语音转写类内容不加密）。
- 上行（F7 等价）：WS 三段式分片上传 init → chunk×N → finish（512KB/片、
  base64、≤100 片），得 ``media_id``（3 天有效），再用 ``aibot_respond_msg``
  的 file/image 消息被动回复。

解密原语用 ``cryptography``（sidecar 已带，不新增依赖）。
"""

from __future__ import annotations

import base64
import hashlib
import math
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import protocol as p
from .client import WecomClient


def decrypt_media(data: bytes, aes_key_b64: str) -> bytes:
    """AES-256-CBC 解密下载体（key=base64 解码 32B，iv=key 前 16B）。"""
    key = base64.b64decode(aes_key_b64)
    if len(key) != 32:
        raise ValueError(f"invalid aeskey length {len(key)} (expected 32)")
    if not data or len(data) % 16 != 0:
        return data  # 密文不是整块：大概率本来就没加密，原样返回
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    pad = padded[-1]
    if 1 <= pad <= 16 and padded[-pad:] == bytes([pad]) * pad:
        return padded[:-pad]
    return padded  # 填充不合法：按未加密内容兜底


async def download_media(url: str, aes_key: str | None,
                         http: httpx.AsyncClient) -> tuple[bytes, str]:
    """下载并解密一个媒体文件；返回 (bytes, filename)（文件名缺省为空串）。

    文件名从 Content-Disposition 解析（URL 本身不携带）。
    """
    resp = await http.get(url)
    resp.raise_for_status()
    fname = ""
    cd = resp.headers.get("content-disposition") or ""
    if "filename" in cd:
        import re
        from urllib.parse import unquote

        m = re.search(r"filename\*?=[\"']?(?:UTF-8'')?([^\"';]+)", cd)
        if m:
            fname = unquote(m.group(1)).strip()
    data = resp.content
    if aes_key:
        data = decrypt_media(data, aes_key)
    return data, fname


async def upload_media(client: WecomClient, path: Path, *,
                       media_type: str = "file") -> str:
    """三段式分片上传 → media_id（串行分片，简单优先；文件多走并发收益小）。"""
    data = path.read_bytes()
    total_size = len(data)
    total_chunks = max(1, math.ceil(total_size / p.MEDIA_CHUNK_BYTES))
    if total_chunks > p.MEDIA_MAX_CHUNKS:
        raise ValueError(
            f"file too large: {total_size} bytes > {p.MEDIA_MAX_CHUNKS} chunks")

    init_body = await client.request(p.CMD_UPLOAD_MEDIA_INIT, {
        "type": media_type,
        "filename": path.name,
        "total_size": total_size,
        "total_chunks": total_chunks,
        "md5": hashlib.md5(data).hexdigest(),
    })
    upload_id = str(init_body.get("upload_id") or "")
    if not upload_id:
        raise RuntimeError(f"upload init missing upload_id: {init_body}")

    for idx in range(total_chunks):
        chunk = data[idx * p.MEDIA_CHUNK_BYTES:(idx + 1) * p.MEDIA_CHUNK_BYTES]
        await client.request(p.CMD_UPLOAD_MEDIA_CHUNK, {
            "upload_id": upload_id,
            "chunk_index": idx,
            "base64_data": base64.b64encode(chunk).decode("ascii"),
        })

    finish_body = await client.request(p.CMD_UPLOAD_MEDIA_FINISH,
                                       {"upload_id": upload_id})
    media_id = str(finish_body.get("media_id") or "")
    if not media_id:
        raise RuntimeError(f"upload finish missing media_id: {finish_body}")
    return media_id

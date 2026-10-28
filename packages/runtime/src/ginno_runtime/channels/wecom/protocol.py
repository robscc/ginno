"""企业微信智能机器人长连接帧协议（纯本地，可单测）。

协议出处：developer.work.weixin.qq.com/document/path/101463（智能机器人长连接）。
帧统一为 ``{"cmd": str, "headers": {"req_id": str}, "body": {...}}``；

- 订阅：``aibot_subscribe`` body ``{bot_id, secret}`` → 回包 ``{headers, errcode, errmsg}``
  （errcode 在**顶层**，不在 body 里）；
- 消息/事件推送：``aibot_msg_callback`` / ``aibot_event_callback``，回复需透传其
  ``headers.req_id``；
- 回复：``aibot_respond_msg``（流式 stream / markdown / file / image …），
  欢迎语 ``aibot_respond_welcome_msg``，主动推送 ``aibot_send_msg``；
- 心跳：``{"cmd": "ping", "headers": {"req_id"}}`` 每 30s 一次；
- 素材上传：init → chunk×N → finish 三段式（512KB 分片、base64）。

实现对照官方 @wecom/aibot-node-sdk 与社区 wecom_aibot Python SDK 逐字段核验。
"""

from __future__ import annotations

import random
import re
import string
import time

from ginno_runtime import jsonx as json

WSS_URL = "wss://openws.work.weixin.qq.com"

# cmd 常量（官方协议命令名）
CMD_SUBSCRIBE = "aibot_subscribe"
CMD_PING = "ping"
CMD_RESPOND_MSG = "aibot_respond_msg"
CMD_RESPOND_WELCOME = "aibot_respond_welcome_msg"
CMD_SEND_MSG = "aibot_send_msg"
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"
CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"

# 协议硬性时限（官方文档）
CALLBACK_FIRST_REPLY_S = 5          # 收到回调后 5s 内必须发出首条回复
STREAM_MAX_S = 10 * 60              # 流式回复全程上限 10 分钟
STREAM_SAFE_S = int(STREAM_MAX_S * 0.95)  # 安全收尾线：9.5 分钟强制 finish
REPLY_WINDOW_S = 24 * 3600          # 回复窗口：收到回调后 24h 内可回复
HEARTBEAT_S = 30                    # 心跳间隔
MEDIA_CHUNK_BYTES = 512 * 1024      # 上传分片大小（base64 前）
MEDIA_MAX_CHUNKS = 100              # 分片上限（~50MB）

_REQ_ALPHABET = string.ascii_letters + string.digits


def new_req_id(prefix: str) -> str:
    """生成唯一 req_id：``{prefix}_{ms时间戳}_{8位随机}``（官方 SDK 同款格式）。"""
    return f"{prefix}_{int(time.time() * 1000)}_{''.join(random.choices(_REQ_ALPHABET, k=8))}"


# ---- 帧构造 ---------------------------------------------------------------


def frame(cmd: str, body: dict | None = None, req_id: str | None = None) -> dict:
    """构造一帧请求；``req_id`` 缺省自动生成。"""
    out: dict = {"cmd": cmd, "headers": {"req_id": req_id or new_req_id(cmd)}}
    if body is not None:
        out["body"] = body
    return out


def stream_body(stream_id: str, content: str, *, finish: bool = False) -> dict:
    """流式回复 body：``{msgtype: stream, stream: {id, content, finish?}}``。

    企微流式语义是**全量替换**：后续帧的 content 直接覆盖已显示内容；
    首帧必须携带 ``stream.id``，之后同 id 续流，``finish=true`` 定格。
    """
    stream: dict = {"id": stream_id, "content": content}
    if finish:
        stream["finish"] = True
    return {"msgtype": "stream", "stream": stream}


def text_body(content: str) -> dict:
    return {"msgtype": "text", "text": {"content": content}}


def markdown_push_body(chatid: str, content: str) -> dict:
    """主动推送 body：``aibot_send_msg`` 仅支持 markdown / template_card。"""
    return {"chatid": chatid, "msgtype": "markdown",
            "markdown": {"content": content}}


def media_body(media_type: str, media_id: str) -> dict:
    """被动回复媒体 body：``{msgtype: file|image|…, <type>: {media_id}}``。"""
    return {"msgtype": media_type, media_type: {"media_id": media_id}}


def parse_frame(raw: str | bytes) -> dict:
    """解析收到的帧为统一 dict；非法 JSON 返回 ``{}``（绝不抛，recv 循环兜底）。

    返回结构：``{"cmd": str|None, "headers": dict, "body": dict|None,
    "errcode": int|None, "errmsg": str|None}``。errcode 非空即回执帧
    （订阅/心跳/回复的 ack），errcode==0 成功。
    """
    try:
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "cmd": data.get("cmd"),
        "headers": data.get("headers") or {},
        "body": data.get("body"),
        "errcode": data.get("errcode"),
        "errmsg": data.get("errmsg"),
    }


# ---- 消息回调 body 提取 -----------------------------------------------------


def msg_user_id(body: dict) -> str:
    return str(((body.get("from") or {}).get("userid")) or "")


def msg_chattype(body: dict) -> str:
    """``single`` 单聊 / ``group`` 群聊（频道消息 chattype 仍为 group）。"""
    return str(body.get("chattype") or "")


def msg_chat_key(body: dict) -> str:
    """会话定位键：群聊用 chatid，单聊没有 chatid（回退 userid）。"""
    return str(body.get("chatid") or msg_user_id(body))


def msg_is_channel(body: dict) -> bool:
    """频道（话题）消息判定：``compat_info.source == "channel"``。

    频道消息是全量推送（不 @ 也推），需自行按正文是否 @ 决定是否回应；
    群聊则只推被 @ 的消息，无需再判。
    """
    compat = body.get("compat_info")
    return isinstance(compat, dict) and str(compat.get("source")) == "channel"


# 群聊 @提及前缀：企微客户端在 @ 后插入的是 U+2005 等窄空格分隔符，
# 不是 ASCII 空格——分隔符类必须显式覆盖 Unicode 空白。
_MENTION_PREFIX_RE = re.compile(
    r"^(@[^\s@]+)[\s  -   　]*")


def strip_mention_prefix(text: str) -> str:
    """剥掉正文开头的 @提及（如 ``@Ginno 你好`` → ``你好``）。

    群聊/频道消息正文会把 @机器人名 原样带在开头（后跟窄空格分隔符），
    喂给 agent 前应去掉；多个 @ 连排一并剥掉。
    """
    out = text.lstrip()
    while True:
        m = _MENTION_PREFIX_RE.match(out)
        if m is None:
            return out
        out = out[m.end():].lstrip()


def is_mention_text(text: str) -> bool:
    return _MENTION_PREFIX_RE.match(text.lstrip()) is not None


def extract_text(body: dict) -> str:
    """文本类正文（text 消息的 text.content）。"""
    return str(((body.get("text") or {}).get("content")) or "")


def extract_mixed(body: dict) -> tuple[str, dict | None]:
    """mixed 图文混排 → (拼接文本, 第一个图片项)；无图片返回 None。"""
    parts: list[str] = []
    image: dict | None = None
    for item in ((body.get("mixed") or {}).get("msg_item")) or []:
        if not isinstance(item, dict):
            continue
        t = str(((item.get("text") or {}).get("content")) or "").strip()
        if t:
            parts.append(t)
        img = item.get("image")
        if isinstance(img, dict) and img.get("url") and image is None:
            image = img
    return " ".join(parts), image


def parse_answer(text: str, options: list) -> dict:
    """把用户回复解析成 resume 载荷（ask_user 文本作答路径）。

    「跳过/skip」→ skip；纯数字 → option_index（1-based）；其余按自由文本。
    """
    t = text.strip()
    if t.lower() in ("跳过", "skip", "跳过这个问题"):
        return {"kind": "user_answer", "answer": "", "option_index": None, "skip": True}
    if t.isdigit():
        idx = int(t)
        if 1 <= idx <= len(options):
            return {"kind": "user_answer", "answer": str(options[idx - 1]),
                    "option_index": idx - 1, "skip": False}
    return {"kind": "user_answer", "answer": t, "option_index": None, "skip": False}

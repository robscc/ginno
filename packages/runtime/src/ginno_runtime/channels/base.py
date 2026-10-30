"""Channel adapter layer: bridge external chat channels (DingTalk, Lark, …)
to Ginno sessions (docs/dingtalk-connector-design.md §3).

分层（与 connectors/ 的分工）：
- ``connectors/`` 负责配置、安装引导与状态上报（连接器体系契约）；
- ``channels/`` 负责协议对接与消息翻译：收渠道消息 → 路由到映射的 Ginno
  session → headless 驱动 turn（``run_channel_turn``）→ 假 WS tap
  （``ChannelTap``）把引擎帧翻译为渠道侧渲染动作。

核心代码对渠道零感知：唯一例外是 ``api/stream/resume.py`` 的公共恢复入口
（ask_user / permission 停车跨渠道复用，见设计文档 §4 F4/F5）。
"""

from __future__ import annotations

import abc
import base64
import time
import uuid
from typing import Any

from ginno_runtime import jsonx as json

from .. import server_shared as shared

# Headless turn 协议复刻自 api/sessions.py `_run_goal_turn` 与 cli.py 的
# exec 路径；关键差异：渠道侧 **保持 interactive**（有人在渠道里应答
# ask_user），因此不调用 `_set_interactive_turn(False)`。


# 工具名 → 任务种类（进展区文案；OpenClaw 同款归类思想：只按归一化名字
# 派生，绝不渲染参数/URL/完整输出）
_TOOL_TASK_KINDS: list[tuple[tuple[str, ...], str]] = [
    (("read", "view", "find", "list", "glob"), "检查文件"),
    (("write", "edit", "patch", "apply"), "应用修改"),
    (("web_search", "search", "fetch", "open"), "查询资料"),
    (("browser",), "验证页面"),
    (("bash", "exec", "process"), "执行命令"),
    (("sql", "query", "database"), "查询数据"),
]


def _tool_kind(name: str) -> str:
    low = name.lower()
    for keys, kind in _TOOL_TASK_KINDS:
        if any(k in low for k in keys):
            return kind
    return "处理任务"


class TurnProgressTracker:
    """IM 渠道「进展区」（P1 方案）：把 thinking/tool 帧聚合成 markdown 引用
    块，随现有流式通道原地更新（企微/钉钉都是全量 content 重发），finalize
    前清掉——终稿只留正文。

    协议现实约束：钉钉同 block 多行只刷尾行 → 每项单行；单 markdown block
    有 ~3000 字隐性上限 → 行数/行宽截断；思考行只留滚动尾片段。

    dirty 标记：有未落盘的变化（tap 的节流 flush 用它判断「无正文也要更新」），
    实际发出一帧后由 tap 复位。
    """

    def __init__(self, *, max_done: int = 4, max_running: int = 3,
                 width: int = 44) -> None:
        self._max_done = max_done
        self._max_running = max_running
        self._width = width
        self._think = ""
        self._order: list[str] = []       # 进行中 tool id（按开始序）
        self._names: dict[str, str] = {}  # id -> 工具名
        self._done: list[str] = []        # 完成行（最新 max_done 条）
        self.dirty = False

    def on_thinking(self, chunk: str) -> None:
        s = " ".join((chunk or "").split())
        if not s:
            return
        tail = s[-self._width:]
        if tail != self._think:
            self._think = tail
            self.dirty = True

    def on_tool_start(self, name: str, tid: str = "") -> None:
        # 想清楚了才动手：动作出现即收思考行
        if self._think:
            self._think = ""
            self.dirty = True
        tid = tid or name
        if tid in self._names:
            return
        self._names[tid] = name or "tool"
        self._order.append(tid)
        self.dirty = True

    def on_tool_end(self, tid: str, name: str = "", result: str = "") -> None:
        tid = tid or name
        nm = self._names.pop(tid, None) or name or "tool"
        if tid in self._order:
            self._order.remove(tid)
        # 结果只取首行摘要（去空白、截宽）——完整输出是泄密+噪音+长度三杀
        first = " ".join((result or "").split())[: self._width]
        line = f"✔ {nm}" + (f"：{first}" if first else "")
        self._done = [*self._done, line][-self._max_done:]
        self.dirty = True

    def clear_thinking(self) -> None:
        """正文开始流（token.delta）即收思考行——思考区只在正文出现前展示。"""
        if self._think:
            self._think = ""
            self.dirty = True

    def clear(self) -> None:
        """终稿清场：finalize 前调用，最终消息只留正文。"""
        if self._think or self._order or self._done:
            self._think = ""
            self._order = []
            self._names = {}
            self._done = []
            self.dirty = True

    def render(self) -> str:
        lines: list[str] = []
        if self._think:
            lines.append(f"🧠 {self._think}…")
        for tid in self._order[-self._max_running:]:
            nm = self._names.get(tid, "tool")
            lines.append(f"🔧 {_tool_kind(nm)}：{nm}")
        lines.extend(self._done)
        return "\n".join(f"> {ln}" for ln in lines) if lines else ""


class ChannelTap:
    """假 WS：挂 ``server_shared._SESSION_WS[session_id]`` 收引擎 turn 帧。

    ``safe_send``（engine.py）对 socket 的全部要求是 ``send_text(str)``；
    帧格式 ``{"event": name, "turn_id"?: ..., **data}``（``_ev``）。基类解析
    帧并按 ``event`` 分发给 :meth:`on_frame`，永不抛异常（抛异常的 socket
    会被引擎剪除）。
    """

    session_id: str = ""

    async def send_text(self, payload: str) -> None:
        try:
            frame = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return
        ev = frame.get("event")
        if not ev:
            return
        try:
            await self.on_frame(ev, frame)
        except Exception:  # noqa: BLE001 — a bad frame must never kill the turn
            pass

    async def on_frame(self, event: str, data: dict) -> None:
        """Subclass hook: translate one engine frame into channel actions."""

    async def close(self) -> None:
        """Optional teardown when the turn/tap is detached."""


class ChannelAdapter(abc.ABC):
    """外部渠道适配器抽象（钉钉是第一个实现）。

    生命周期由对应 Connector（connectors/ 体系）驱动：``apply_config`` →
    ``start`` → （运行中 ``report_status``）→ ``stop``。
    """

    channel_name: str = "abstract"

    def __init__(self) -> None:
        self._running = False

    @abc.abstractmethod
    async def start(self, config: dict) -> None:
        """Establish the channel connection (idempotent; re-config restarts)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Drop the connection and detach every active tap."""

    def report_status(self, status: str, detail: str = "") -> None:
        """Forward a status transition to the connector registry (debounced)."""
        from ..connectors.registry import registry

        registry().report(self.channel_name, status, detail)


async def run_channel_turn(
    session: dict,
    text: str,
    tap: ChannelTap,
    *,
    attached_files: list[dict] | None = None,
    images: list[dict] | None = None,
    usage_source: str = "channel",
    agent_id: str | None = None,
) -> list[str]:
    """Drive ONE headless turn on ``session`` with ``tap`` attached (F2 §7).

    Mirrors the headless protocol of ``api/sessions.py::_run_goal_turn`` /
    ``cli.py`` exec path — early-armed ``_RUNNING_TURNS``, tap attached for the
    whole turn, running-flag cleanup that KEEPS the registration when the turn
    parked at an interrupt (a pending question must stay answerable).

    Returns the salvaged steer texts: entries enqueued mid-turn (steer_channel_
    turn) that the turn ended before absorbing. The caller (adapter) redelivers
    them as a follow-up turn AFTER the session lock releases — the 「已追加」
    ack must not silently eat the user's words. Empty list in every other case
    (normal turn, parked-at-interrupt — those entries stay for the resumed
    segment to absorb).
    """
    from ..api import stream as _stream_api  # lazy: api.stream imports sessions

    session_id = session["session_id"]
    slug = session.get("project_slug") or "default"
    agent = agent_id or session.get("agent_id")
    turn_id = uuid.uuid4().hex
    config: dict[str, Any] = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": agent,
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": usage_source,
            # 渠道轮次在桌面端没有 composer 的乐观 user 气泡：让 engine 的
            # turn.start 带上用户原文（echo），前端据此回显 IM 侧输入。
            "echo_user": True,
        }
    }
    if attached_files:
        config["configurable"]["attached_files"] = attached_files
    if images:
        # 供 engine 的 turn.start 回显（桌面端实时预览 IM 侧发来的图片；
        # 与 history 的 multimodal 图片块同构，回灌天然去重）。
        config["configurable"]["echo_images"] = images
    # Early-arm: a channel `stop` during the preamble must see a live turn.
    shared._RUNNING_TURNS[session_id] = turn_id
    tap.session_id = session_id
    shared._SESSION_WS.setdefault(session_id, []).append(tap)
    salvaged: list[str] = []
    try:
        # ── builtin 命令短路（/clear /goal /help …，与桌面 WS invoke 同口径）──
        # 渠道路径此前不过 resolve_turn，命令被当纯文本喂给模型（2026-10-28
        # 真机实录：企微发 /clear，模型回了一句解释，上下文没清）。回执走
        # tap 的真实翻译路径（企微流式/钉钉卡片），像一条微型 turn。
        skill_name: str | None = None
        try:
            from ..commands.resolver import resolve_turn

            plan = resolve_turn({"message": text}, session)
            if plan.builtin_reply is not None or plan.builtin_async is not None:
                receipt = plan.builtin_reply
                if plan.builtin_async is not None:
                    from ..commands.registry import BUILTINS

                    receipt = await BUILTINS[plan.builtin_async].async_handler(
                        slug, session, plan.builtin_args)
                await tap.send_text(json.dumps(
                    {"event": "turn.start", "turn_id": turn_id},
                    ensure_ascii=False))
                await tap.send_text(json.dumps(
                    {"event": "token.delta", "content": receipt or "✓"},
                    ensure_ascii=False))
                await tap.send_text(json.dumps(
                    {"event": "message.end", "text": ""}, ensure_ascii=False))
                return []
            text = plan.text or text  # mention/skill 归一化后的正文（同桌面）
            skill_name = plan.skill_name
        except Exception:
            shared._log.exception(
                "channel_cmd_resolve_failed session=%s", session_id)
        await _stream_api._run_stream(
            None, session["graph"], config, text, session, agent,
            images=images, skill_name=skill_name)
    finally:
        # Parked-at-interrupt turns STAY registered (resume hasn't happened).
        if (
            shared._RUNNING_TURNS.get(session_id) == turn_id
            and session_id not in shared._PENDING_RESUME
        ):
            shared._RUNNING_TURNS.pop(session_id, None)
        sockets = shared._SESSION_WS.get(session_id) or []
        shared._SESSION_WS[session_id] = [s for s in sockets if s is not tap]
        await tap.close()
        # 补投排水（engine 段末已保留 channel 来源条目）：未停车且还有未吸
        # 收的 steer = turn 在最终 superstep 后收尾，注入没了下家。停车中不
        # 动——resume 段的 agent_node 还会排水吸收。异常路径（_run_stream 抛
        # 出）同样不动，条目留给下一次排水。
        if session_id not in shared._PENDING_RESUME:
            leftovers = shared.steer_drain(session_id)
            salvaged = [
                str(e.get("text") or "")
                for e in leftovers
                if e.get("origin") == "channel"
            ]
            salvaged = [t for t in salvaged if t.strip()]
            if salvaged:
                shared._log.info(
                    "channel_steer_salvage session=%s turn=%s n=%d",
                    session_id, turn_id, len(salvaged),
                )
    return salvaged


_MIME_BY_EXT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp",
}
# 魔数嗅探优先于扩展名：企微图片默认落名 .png 但实际可能是 JPEG，
# mime 与字节不符会被 vision API 拒收。
_IMAGE_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # 需再核 WEBP 标记
]
# 多模态注入的体积上限：超限只落盘走 attached_files（工具仍可读），
# 不硬塞 vision 上下文（provider 上限不一，8MB 是保守值）。
_IMAGE_MULTIMODAL_MAX_BYTES = 8 * 1024 * 1024


def _sniff_image_mime(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    for magic, mime in _IMAGE_MAGIC:
        if data[:len(magic)] == magic:
            return mime
    return None


def image_payload_from_bytes(data: bytes, fname: str) -> list[dict] | None:
    """入站图片 → composer 同款 multimodal 载荷（``_run_stream(images=…)``），
    让模型直接「看到」像素而不是只拿到文件路径。魔数嗅探 mime（扩展名只做
    兜底）；不可识别格式或超限返回 None——调用方保持纯 attached_files 行为。"""
    if not data or len(data) > _IMAGE_MULTIMODAL_MAX_BYTES:
        return None
    mime = _sniff_image_mime(data)
    if mime is None:
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        mime = _MIME_BY_EXT.get(ext) or ""
    if not mime:
        return None
    return [{"data": base64.b64encode(data).decode("ascii"), "media_type": mime}]


def steer_channel_turn(session: dict, text: str) -> bool:
    """IM 侧 steering：turn 在跑且未停车 → 文本进 steer 队列（与桌面
    composer 的 WS steer 同一队列、同一吸收路径——graph.agent_node 在下一
    个 superstep 排水，与谁入队无关）。返回 False = 无可注入的活 turn
    （调用方回落原路：busy 提示/新 turn）。

    停车（ask_user 等作答）刻意不注入：答案走 IM 卡片回调，不复刻桌面
    steer-as-answer 的双入口竞态。文本-only，与桌面 v1 同口径
    （steering-design.md §3.1）；附件仍走 busy 提示。

    「已追加」不是空头支票：turn 若在下一个 superstep 前收尾（短问答单
    superstep 常见），条目不会被段末清场丢掉（engine 端 steer_keep_channel
    保留 channel 来源），run_channel_turn 尾部排水后由 adapter 补投为后续
    turn（见其返回值）。"""
    session_id = session["session_id"]
    if session_id not in shared._RUNNING_TURNS or session_id in shared._PENDING_RESUME:
        return False
    if not (text or "").strip():
        return False
    # builtin 命令不进 steer（同桌面 ws.py steer 分支）：/clear /compact 这类
    # 状态重写与活 turn 竞态——回落 busy 提示，本轮结束再发。
    try:
        from ..commands.resolver import parse_slash
        from ..commands.registry import BUILTINS

        _cmd = parse_slash(text, session.get("project_slug") or "default")
        if _cmd and _cmd[0] in BUILTINS:
            return False
    except Exception:
        pass
    shared.steer_enqueue(
        session_id,
        {
            # steer_id 即持久化 HumanMessage id：必须全新，复用 turn id 会让
            # checkpointer 的 delta 静默丢内容（见 steer_messages 注释）。
            "steer_id": uuid.uuid4().hex,
            "turn_id": shared._RUNNING_TURNS.get(session_id) or "",
            "text": text,
            "injected_at": time.time(),
            # 渠道来源标记：段末清场保留（steer_keep_channel）、补投识别用。
            "origin": "channel",
            "content_blocks": None,
            "context_text": None,
            "files": [],
            "images": [],
        },
    )
    shared._log.info(
        "channel_steer session=%s turn=%s text=%r",
        session_id, shared._RUNNING_TURNS.get(session_id), text[:120],
    )
    return True


async def ensure_channel_session(
    session_id: str | None,
    *,
    default_agent_id: str | None = None,
    title: str | None = None,
    channel: str | None = None,
) -> tuple[dict | None, str | None]:
    """Return (session, session_id) — rebuild from disk if the runtime restarted.

    ``session_id=None`` creates a brand-new session via the public create API
    (``POST /api/sessions`` 的内部等价路径)；``title`` 让渠道会话在侧栏可认
    （如「企业微信 · 张三」），缺省沿用默认标题。``channel``（如 "wecom"）
    写入 ``type="channel"`` + ``channel`` 标记——桌面端据此渲染只读视图
    （对话在 IM 侧进行，agent/模型/挂载仍可在桌面改）。
    Returns (None, error) on failure.
    """
    from ..api.sessions import (
        CreateSessionRequest,
        _ensure_session,
        create_session,
    )
    from ..session_meta import mark_channel_meta

    if session_id:
        s = _ensure_session(session_id)
        if s:
            if channel:
                mark_channel_meta(s, channel)
            return (s, session_id)
        # 映射指向已删除的会话（桌面端删了渠道会话后映射表仍存留旧 id）：
        # 确认 meta 已不在则落到下方新建路径重开会话，适配器随后 bind 新 id 自愈。
        from ..session_meta import _find_meta

        if not _find_meta(session_id):
            shared._log.info(
                "channel_session_gone session=%s → recreating", session_id)
            session_id = None
        else:
            # meta 还在但重建失败（模型/凭证问题）：维持报错，别静默换新会话
            return (None, session_id)
    # ``workspace`` is superseded by the server (session_files_dir(slug, sid)),
    # so an empty value is fine; the create path mirrors the desktop client's.
    resp = await create_session(
        CreateSessionRequest(project_slug="default", workspace="",
                             agent_id=default_agent_id, title=title,
                             type="channel" if channel else None,
                             channel=channel))
    if not isinstance(resp, dict):
        return None, str((resp or {}).get("error") or "create_session failed")
    # create_session 返回 meta 形状（主键叫 ``id``）；两个键都认，
    # 防止 API 形状再变时渠道全灭。
    sid = str(resp.get("session_id") or resp.get("id") or "")
    if not sid:
        return None, str(resp.get("error") or "create_session failed")
    s = _ensure_session(sid)
    return (s, sid if s else None)


def mark_channel_sessions(channel: str, session_ids) -> int:
    """Backfill the channel mark on existing sessions (adapter startup).

    标记引入前创建的渠道会话没有 type/channel；adapter 启动时拿 mapper
    落盘的映射（chat→ginno_session，可靠事实来源）批量补齐。幂等：已带
    标记的跳过；会话已删除的忽略。返回补写数量（日志用）。
    """
    from ..session_meta import _find_meta, mark_channel_meta

    n = 0
    for sid in session_ids:
        found = _find_meta(str(sid))
        meta = found[0] if found else None
        if not meta or meta.get("type") == "channel":
            continue
        mark_channel_meta(meta, channel)
        n += 1
    if n:
        shared._log.info("channel mark backfilled channel=%s count=%d", channel, n)
    return n


async def notify_channel_session_deleted(channel: str | None, session_id: str) -> None:
    """桌面端删除渠道会话后反向告知 IM 侧（尽力而为，绝不抛出）。

    适配器按各自映射表解绑该会话的全部 chat，并在连接可用时推送一条
    「会话已删除」提示——否则用户在 IM 里继续说话只会看到含糊的
    「会话创建失败」。渠道未启用/适配器未运行时静默跳过（残留映射由
    ensure_channel_session 的死链回落自愈）。
    """
    if not channel:
        return
    try:
        from ..connectors import builtin as _conns

        adapter = _conns.channel_adapter(channel)
        notify = getattr(adapter, "notify_session_deleted", None) if adapter else None
        if notify is None:
            return
        await notify(session_id)
    except Exception:  # noqa: BLE001 — 通知失败不影响删除结果
        shared._log.exception(
            "channel_delete_notice channel=%s session=%s failed", channel, session_id)


async def relay_channel_reply(session_id: str, text: str) -> None:
    """无 tap 的 turn 在渠道会话上收口 → 回复补推 IM 侧（尽力而为，不抛出）。

    2026-10-30 keep_edc84fe53d 事故：调度器开的头 turn（子代理结果回传 /
    worker digest·result / 任务完成注入）不走 run_channel_turn——没有
    ChannelTap 挂 _SESSION_WS，回复只落会话与桌面端，IM 用户永远收不到
    订阅的消息。engine 收口分支对本函数放行：channel 类型会话、流上没有
    tap（有 tap = 适配器驱动的正常轮，回复已由 tap 翻译，重发即重复）、
    正文非空 → 经适配器 notify_turn_reply 主动推送。
    """
    text = (text or "").strip()
    if not text:
        return
    for s in shared._SESSION_WS.get(session_id) or []:
        if isinstance(s, ChannelTap):
            return  # 适配器驱动的轮次：tap 已在转送，不得重发
    from ..session_meta import _find_meta

    found = _find_meta(session_id)
    meta = found[0] if found else None
    if not meta or meta.get("type") != "channel":
        return
    channel = str(meta.get("channel") or "")
    if not channel:
        return
    try:
        from ..connectors import builtin as _conns

        adapter = _conns.channel_adapter(channel)
        notify = getattr(adapter, "notify_turn_reply", None) if adapter else None
        if notify is None:
            return
        await notify(session_id, text)
        shared._log.info(
            "channel_reply_relay channel=%s session=%s len=%d",
            channel, session_id, len(text))
    except Exception:  # noqa: BLE001 — 补推失败不影响 turn 收口
        shared._log.exception(
            "channel_reply_relay channel=%s session=%s failed", channel, session_id)


async def quiesce(session_id: str, slug: str, timeout_s: float = 8.0) -> dict:
    """Wait for subagents/background work to settle (cli.py 同名协议的轻封装)."""
    from ..cli import _await_quiescence

    return await _await_quiescence(session_id, slug, timeout_s)

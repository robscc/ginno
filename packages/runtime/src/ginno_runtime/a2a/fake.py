"""脚本化假 A2A 对端 —— ``GINNO_FAKE_A2A_PEER`` 测试缝（设计 §11.3.1 吸收清单）。

照 ``GINNO_FAKE_LLM`` 先例（testing/fake_model.py）：环境变量启用 + JSON 剧本
驱动，全链路测试零真机、零真网络外联（只绑 127.0.0.1 随机端口，不碰 8787）。

    GINNO_FAKE_A2A_PEER=1                 启用标记（fake_peer_enabled()）
    GINNO_FAKE_A2A_SCRIPTS=<json 路径>    剧本文件（也接受内联 JSON 串）

**只有对端是假的**：A2AClient / PeerStore / ContextMap / build_a2a_tools 全部
走真实现。本模块是独立极简 FastAPI app，刻意不 import 真 server.py 的内部
（A2ADeps/TaskRegistry/审计等）——只复用鉴权与 Card 的**形状**：

- 能力地址 path-token（``POST /a2a/<token>``，地址即凭据，§6.2）；
  也接受根路径 + ``Authorization: Bearer <token>``（bearer 简化落法）；
- 错/缺 token 一律统一 401 + ``WWW-Authenticate: Bearer``（照真 server 姿态）；
- 公开 Card 在 ``GET /.well-known/agent-card.json``（fetch_card 的发现路径）。

剧本 schema（三个独立泳道，按调用顺序逐条消费）::

    {
      "send": [                       # message/send，每次调用弹一条
        {"reply_kind": "task",        # task(默认) | message
         "states": ["working", "completed"],  # 状态序列，默认 ["completed"]
         "result": "done text",       # 终态时作为 artifact 文本
         "status_message": "...",     # 可选 status.message（input-required 提示）
         "error": {"code": -32001, "message": "...", "data": {...}},
         "context_id": "", "task_id": "", "metadata": {}}
      ],
      "get": [                        # tasks/get，可选泳道
        {"state": "completed", "result": "...", "error": {...}}
      ],
      "cancel": {"error": {...}}      # tasks/cancel，单 dict 或 list
    }

语义约定（确定性优先）：

- ``send`` 返回 ``states[0]`` 的 Task；``result`` 文本**只在终态**
  （completed/failed/canceled/rejected）作为 artifact 出现——working 中的
  task 还没有产物，照真实语义。
- ``get`` 泳道非空时逐条弹出覆盖（state/result/error）；为空时沿 send 建好的
  状态序列**前进一步**（终态不再前进，终态不可重启 §8.1）。未知 taskId 一律
  ``-32001 TaskNotFoundError``。
- ``cancel`` 默认把 task 置 canceled 并回 Task；error 条目原样透传 JSON-RPC
  error（取消幂等测试用：purged 后回 -32001，客户端应视为成功，§9.6）。
- ``error`` 条目在任何泳道都优先于正常响应——自愈测试用
  :func:`retention_error` 造 -32001 + ``ginno:retention`` data。
- contextId 透传：请求带 contextId 就复用（不透明，不校验存在性——自愈重发
  依赖这一点）；不带则生成新的。taskId/contextId 一律 server 生成。
- 泳道耗尽：send 回默认 completed task（result 带耗尽标记，断言可见）；
  get/cancel 回落到默认状态机行为。绝不抛异常、绝不静默挂起。

``handle_rpc`` 是纯逻辑（dict → dict，零 IO），单测直接驱动；FastAPI 层只做
鉴权 + JSON 编解码。``start_fake_peer(scripts) -> (base_url, token)`` 供测试
起真 listener；``fake_peer()`` 异步上下文管理器保证收尾。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ginno_runtime import jsonx as json

from .address import build_address
from .auth import generate_token, token_hash, verify_token
from .errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    TASK_NOT_FOUND,
    retention_data,
)
from .models import TERMINAL_STATES, TaskState

__all__ = [
    "FakeA2APeer",
    "fake_peer",
    "fake_peer_enabled",
    "parse_scripts",
    "retention_error",
    "scripts_from_env",
    "start_fake_peer",
    "stop_fake_peers",
]

#: 泳道耗尽时 send 的默认回复——标记文本进结果，测试断言可见，不静默。
_EXHAUSTED_RESULT = "(fake peer script exhausted)"

_STATE_NAMES = {s.value for s in TaskState}
# 用「值集合」而非直接 `in TERMINAL_STATES`：显式表达「这里比的是 wire 字符串」。
# 澄清一个曾被写错的说法：TaskState 是 (str, Enum)，用的是 **str mixin 的
# __hash__（按值哈希）**，所以 `"completed" in TERMINAL_STATES` 其实为 True
# （Python 3.11.14 实测：hash(成员) == hash("completed")）。并不存在
# 「字符串成员判定恒 False」的陷阱。此处仍用值集合，只为可读性与不依赖
# mixin 哈希细节，不是因为原写法有 bug。
_TERMINAL_VALUES = frozenset(s.value for s in TERMINAL_STATES)


def fake_peer_enabled() -> bool:
    """``GINNO_FAKE_A2A_PEER`` 启用标记（照 GINNO_FAKE_LLM 的 truthy 口径）。"""
    return (os.environ.get("GINNO_FAKE_A2A_PEER") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def scripts_from_env() -> dict:
    """解析 ``GINNO_FAKE_A2A_SCRIPTS``：JSON 文件路径或内联 JSON 串。

    照 fake_model._scripts_from_env：未设置/非法 → 空 dict（FakeA2APeer 落到
    默认耗尽回复），绝不抛。
    """
    raw = os.environ.get("GINNO_FAKE_A2A_SCRIPTS", "")
    if not raw:
        return {}
    try:
        if os.path.exists(raw):
            data = json.loads(Path(raw).read_text(encoding="utf-8"))
        else:
            data = json.loads(raw)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def retention_error(reason: str, *, message: str = "Task not found") -> dict:
    """造一个 -32001 + ``ginno:retention`` 的 JSON-RPC error 对象（剧本用）。

    ``reason`` 取 errors.REASON_*（task_purged / context_expired /
    context_purged）——驱动工具层 §9.6 自愈分支的精确判定。
    """
    return {
        "code": TASK_NOT_FOUND,
        "message": message,
        "data": retention_data(reason, obj="task" if reason == "task_purged" else "context"),
    }


# --------------------------------------------------------------------------- #
# 剧本解析（纯逻辑，单测直接驱动）
# --------------------------------------------------------------------------- #

def _as_list(raw: Any) -> list:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]  # 单 dict（cancel 常见形态）包成单条泳道


def _norm_error(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    return {
        "code": code if isinstance(code, int) else INTERNAL_ERROR,
        "message": str(raw.get("message") or "A2A error"),
        "data": raw.get("data"),
    }


def _norm_states(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return ["completed"]
    out = [str(s).strip().lower() for s in raw if str(s or "").strip()]
    return out or ["completed"]


def _norm_send(raw: Any) -> dict:
    if isinstance(raw, str):
        raw = {"result": raw}
    raw = raw if isinstance(raw, dict) else {}
    states = _norm_states(raw.get("states"))
    return {
        "reply_kind": "message" if raw.get("reply_kind") == "message" else "task",
        "states": [s if s in _STATE_NAMES else "unspecified" for s in states],
        "result": str(raw.get("result") or ""),
        "status_message": str(raw.get("status_message") or ""),
        "error": _norm_error(raw.get("error")),
        "context_id": str(raw.get("context_id") or ""),
        "task_id": str(raw.get("task_id") or ""),
        "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
    }


def _norm_get(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out: dict = {"error": _norm_error(raw.get("error"))}
    if raw.get("state"):
        state = str(raw["state"]).strip().lower()
        out["state"] = state if state in _STATE_NAMES else "unspecified"
    if "result" in raw:
        out["result"] = str(raw.get("result") or "")
    return out


def _norm_cancel(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    state = str(raw.get("state") or "canceled").strip().lower()
    return {
        "state": state if state in _STATE_NAMES else "canceled",
        "error": _norm_error(raw.get("error")),
    }


def parse_scripts(raw: Any) -> dict:
    """规整剧本为 ``{"send": [...], "get": [...], "cancel": [...]}``。

    宽容解析（照 models 的姿态）：非 dict 顶层 → 空剧本；字符串 send 条目 →
    ``{"result": 字符串}``；畸形条目归一为默认值。绝不抛。
    """
    raw = raw if isinstance(raw, dict) else {}
    return {
        "send": [_norm_send(e) for e in _as_list(raw.get("send"))],
        "get": [_norm_get(e) for e in _as_list(raw.get("get"))],
        "cancel": [_norm_cancel(e) for e in _as_list(raw.get("cancel"))],
    }


# --------------------------------------------------------------------------- #
# 响应组装（纯逻辑）
# --------------------------------------------------------------------------- #

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rpc_ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _not_found(req_id: Any) -> dict:
    return _rpc_error(req_id, TASK_NOT_FOUND, "Task not found")


@dataclass
class _FakeTask:
    """server 侧任务状态：send 建好的状态序列 + 游标。"""

    task_id: str
    context_id: str
    states: list[str]
    cursor: int = 0
    result: str = ""
    status_message: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.states[min(self.cursor, len(self.states) - 1)]

    def advance(self) -> None:
        """get 轮询前进一步；终态不可重启（§8.1），停在终态。"""
        if self.cursor < len(self.states) - 1 and self.state not in _TERMINAL_VALUES:
            self.cursor += 1

    def snapshot(self) -> dict:
        status: dict = {"state": self.state, "timestamp": _utcnow()}
        if self.status_message:
            status["message"] = {
                "role": "agent",
                "parts": [{"kind": "text", "text": self.status_message}],
                "messageId": uuid.uuid4().hex,
            }
        artifacts = []
        # result 只在终态作为 artifact 出现——working 中还没有产物。
        if self.result and self.state in _TERMINAL_VALUES:
            artifacts.append({
                "artifactId": f"art-{self.task_id}",
                "name": "result",
                "parts": [{"kind": "text", "text": self.result}],
            })
        out: dict = {
            "id": self.task_id,
            "contextId": self.context_id,
            "status": status,
            "artifacts": artifacts,
        }
        if self.metadata:
            out["metadata"] = self.metadata
        return out


# --------------------------------------------------------------------------- #
# FakeA2APeer
# --------------------------------------------------------------------------- #

class _NoSignalServer(uvicorn.Server):
    """测试进程拥有信号处理——fake listener 不抢（照 inbound._NoSignalServer）。"""

    def install_signal_handlers(self) -> None:
        pass


class FakeA2APeer:
    """脚本化假对端：纯逻辑 ``handle_rpc`` + 可选的真 127.0.0.1 listener。

    不 start() 也能用 handle_rpc 做纯逻辑单测；start() 后 base_url/address
    可用，stop() 幂等收尾。
    """

    def __init__(self, scripts: Any = None) -> None:
        self.scripts = parse_scripts(scripts if scripts is not None else scripts_from_env())
        self.token = generate_token()
        self._token_hash = token_hash(self.token)
        self.base_url = ""
        self.requests: list[dict] = []  # 收到的 JSON-RPC（method+params），供断言
        self._tasks: dict[str, _FakeTask] = {}
        self._cursors = {"send": 0, "get": 0, "cancel": 0}
        self._server: _NoSignalServer | None = None
        self._serve_task: asyncio.Task | None = None
        self._app: FastAPI | None = None

    # ---- 剧本消费 ----------------------------------------------------------

    def _pop(self, lane: str) -> dict | None:
        entries = self.scripts[lane]
        i = self._cursors[lane]
        if i >= len(entries):
            return None
        self._cursors[lane] = i + 1
        return entries[i]

    # ---- 纯逻辑 JSON-RPC 分发 ----------------------------------------------

    def handle_rpc(self, body: Any) -> dict:
        """JSON-RPC 请求 dict → 响应 dict。零 IO，单测直接驱动。"""
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return _rpc_error(None, INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
        req_id = body.get("id")
        method = body.get("method")
        if not isinstance(method, str):
            return _rpc_error(req_id, INVALID_REQUEST, "Missing method")
        params = body.get("params")
        params = params if isinstance(params, dict) else {}
        self.requests.append({"method": method, "params": params})
        if method == "message/send":
            return self._handle_send(req_id, params)
        if method == "tasks/get":
            return self._handle_get(req_id, params)
        if method == "tasks/cancel":
            return self._handle_cancel(req_id, params)
        return _rpc_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def _handle_send(self, req_id: Any, params: dict) -> dict:
        message = params.get("message")
        if not isinstance(message, dict):
            return _rpc_error(req_id, INVALID_PARAMS, "params.message is required")
        entry = self._pop("send") or _norm_send({"result": _EXHAUSTED_RESULT})
        if entry["error"]:
            err = entry["error"]
            return _rpc_error(req_id, err["code"], err["message"], err["data"])
        # contextId 透传：带就复用（不校验存在性），不带则 server 生成。
        cid = entry["context_id"] or str(message.get("contextId") or "") or f"ctx-{uuid.uuid4().hex[:12]}"
        if entry["reply_kind"] == "message":
            result: dict = {
                "kind": "message",
                "role": "agent",
                "parts": [{"kind": "text", "text": entry["result"]}],
                "messageId": uuid.uuid4().hex,
                "contextId": cid,
            }
            if entry["metadata"]:
                result["metadata"] = entry["metadata"]
            return _rpc_ok(req_id, result)
        tid = entry["task_id"] or f"task-{uuid.uuid4().hex[:12]}"
        task = _FakeTask(
            task_id=tid,
            context_id=cid,
            states=list(entry["states"]),
            result=entry["result"],
            status_message=entry["status_message"],
            metadata=dict(entry["metadata"]),
        )
        self._tasks[tid] = task
        return _rpc_ok(req_id, task.snapshot())

    def _handle_get(self, req_id: Any, params: dict) -> dict:
        tid = str(params.get("id") or "")
        entry = self._pop("get")
        if entry and entry["error"]:
            err = entry["error"]
            return _rpc_error(req_id, err["code"], err["message"], err["data"])
        task = self._tasks.get(tid)
        if task is None:
            return _not_found(req_id)
        if entry:
            if entry.get("state"):
                task.states = [entry["state"]]
                task.cursor = 0
            if "result" in entry:
                task.result = entry["result"]
        else:
            task.advance()
        return _rpc_ok(req_id, task.snapshot())

    def _handle_cancel(self, req_id: Any, params: dict) -> dict:
        tid = str(params.get("id") or "")
        entry = self._pop("cancel")
        if entry and entry["error"]:
            err = entry["error"]
            return _rpc_error(req_id, err["code"], err["message"], err["data"])
        task = self._tasks.get(tid)
        if task is None:
            return _not_found(req_id)
        task.states = [entry["state"] if entry else "canceled"]
        task.cursor = 0
        return _rpc_ok(req_id, task.snapshot())

    # ---- Card / 地址 --------------------------------------------------------

    def card(self) -> dict:
        """公开 Agent Card（形状照真 server 的最小子集；validate_card 全过）。"""
        return {
            "name": "Fake A2A Peer",
            "description": "Scripted fake A2A peer for Ginno loopback tests.",
            "url": f"{self.base_url}/a2a/{self.token}",
            "version": "0.0.0-fake",
            "protocolVersion": "1.0",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [{
                "id": "fake-scripted",
                "name": "Scripted replies",
                "description": "Replies are driven by GINNO_FAKE_A2A_SCRIPTS.",
                "tags": ["test"],
            }],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "security": [{"bearer": []}],
        }

    @property
    def address(self) -> str:
        """能力地址（复制→粘贴→连接 的输入，§6.1）。start() 后可用。"""
        if not self.base_url:
            raise RuntimeError("fake peer not started")
        return build_address(self.base_url, self.token, "")

    # ---- HTTP 层（鉴权 + 编解码，逻辑全在 handle_rpc） ----------------------

    def _check_token(self, candidate: str) -> bool:
        return verify_token(candidate or "", self._token_hash)

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="ginno-fake-a2a-peer")

        def unauthorized() -> JSONResponse:
            # 统一 401 形态（照真 server）：不泄露存在性差异。
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        async def rpc_response(request: Request) -> JSONResponse:
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (ValueError, TypeError):
                return JSONResponse(_rpc_error(None, PARSE_ERROR, "Parse error"))
            return JSONResponse(self.handle_rpc(body))

        @app.get("/.well-known/agent-card.json")
        async def card_public() -> JSONResponse:
            return JSONResponse(self.card())

        @app.get("/a2a/{token}/.well-known/agent-card.json")
        async def card_scoped(token: str) -> JSONResponse:
            if not self._check_token(token):
                return unauthorized()
            return JSONResponse(self.card())

        @app.post("/a2a/{token}")
        @app.post("/a2a/{token}/")
        async def rpc_capability(token: str, request: Request) -> JSONResponse:
            if not self._check_token(token):
                return unauthorized()
            return await rpc_response(request)

        @app.post("/")
        @app.post("/a2a")
        async def rpc_bearer(request: Request) -> JSONResponse:
            auth = request.headers.get("authorization", "")
            candidate = auth[7:] if auth.lower().startswith("bearer ") else ""
            if not self._check_token(candidate):
                return unauthorized()
            return await rpc_response(request)

        return app

    # ---- 生命周期 -----------------------------------------------------------

    async def start(self) -> None:
        """起真 listener：127.0.0.1 随机端口（绝不碰 8787）。幂等。"""
        if self._server is not None:
            return
        cfg = uvicorn.Config(
            self._build_app(), host="127.0.0.1", port=0, log_level="warning",
        )
        self._server = _NoSignalServer(cfg)
        self._serve_task = asyncio.create_task(self._server.serve())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while not self._server.started:
            if self._serve_task.done():
                self._serve_task.result()  # 抛出启动失败原因
                raise RuntimeError("fake peer server exited before startup")
            if loop.time() > deadline:
                raise TimeoutError("fake peer failed to start within 10s")
            await asyncio.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def stop(self) -> None:
        """停 listener。幂等；超时兜底 cancel，不留孤儿 task。"""
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            try:
                await asyncio.wait_for(self._serve_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._serve_task.cancel()
        self._server = None
        self._serve_task = None


# --------------------------------------------------------------------------- #
# 测试入口
# --------------------------------------------------------------------------- #

_LIVE_PEERS: list[FakeA2APeer] = []


async def start_fake_peer(scripts: Any = None) -> tuple[str, str]:
    """起一个 FakeA2APeer，返回 ``(base_url, token)``。

    ``scripts=None`` 时读 ``GINNO_FAKE_A2A_SCRIPTS``（环境缝）。收尾用
    :func:`stop_fake_peers`（或改用 :func:`fake_peer` 上下文管理器）。
    """
    peer = FakeA2APeer(scripts)
    await peer.start()
    _LIVE_PEERS.append(peer)
    return peer.base_url, peer.token


async def stop_fake_peers() -> None:
    """停掉所有经 :func:`start_fake_peer` 起的对端。幂等。"""
    while _LIVE_PEERS:
        peer = _LIVE_PEERS.pop()
        await peer.stop()


@asynccontextmanager
async def fake_peer(scripts: Any = None):
    """``async with fake_peer({...}) as peer:`` —— 保证 stop 的上下文管理器。

    yield 的是 FakeA2APeer 本体（.base_url / .token / .address / .requests）。
    """
    peer = FakeA2APeer(scripts)
    await peer.start()
    try:
        yield peer
    finally:
        await peer.stop()

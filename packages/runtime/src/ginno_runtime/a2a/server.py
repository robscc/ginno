"""A2A 入站 server —— 独立 FastAPI app（设计 §2.2 / §10.1 / §10.2 / §11.2）。

**本 app 绝不挂进 8787 主 server**——它是独立 uvicorn listener（默认 :8790，
P0 仅环回）的独立应用。物理隔离即不继承主 API 的无鉴权问题（§10.1）。

这是对 external-agents「不暴露被调入接口」裁定的显式突破（§2.2），六项控制
手段在本模块与 inbound.py 的落实位置：
  ① 独立 listener，不复用 8787 —— inbound.py 起独立 uvicorn.Server；
  ② 默认关 —— 门控在连接器层（a2a_inbound_enabled 默认 false，P1 接线）；
  ③ 能力子集白名单 —— P0 只有下方四个逻辑路由，别无其它；
  ④ 权限请求 fail-closed 转人工 —— P1 执行层（P0 不跑 agent，无权限面）；
  ⑤ R3 类路由**根本不注册**——settings/mcp/skills/agents/providers 等路径
     不存在，探测得 404 而非 403（§11.2 实现约束）；
  ⑥ 全量审计且脱敏 —— 只记 token hash 前 8 位 / method / 时间 / 结果，
     peer 消息内容与明文 token 绝不落审计。

路由清单（恰好四个逻辑端点；POST 主端点注册了带/不带尾斜杠两个形态，
因为本仓 a2a.client 与标准 A2A client 都 POST 到无尾斜杠的能力端点，
且 httpx 默认不跟随 307）：
  GET  /a2a/<token>/.well-known/agent-card.json  分级披露的公开 Card
  POST /a2a/<token>[/]                         JSON-RPC 主端点：message/send，
                                               兼分发 tasks/get、tasks/cancel
  POST /a2a/<token>/tasks/get                  JSON-RPC：tasks/get
  POST /a2a/<token>/tasks/cancel               JSON-RPC：tasks/cancel（幂等）

鉴权：能力地址 path-token（地址即凭据，§6.2），TokenStore.verify。未带/错
token 一律**立即**回统一 401 + WWW-Authenticate: Bearer——任何失败形态
（缺 token / 错 token / 路径不存在）响应体逐字节相同，不泄露存在性差异；
拒绝在 AUTH_REJECT_DEADLINE_S（3s）预算内完成，实际是即时响应，不挂连接。

N2b（入站执行层）：message/send 不再返回 submitted 占位——它把任务派给共享执行层
``a2a/inbound_exec.py``（桌面端入站与 ginno node 共用），真跑 agent（会话映射照
channels/base.py ensure_channel_session 范式，type="a2a"/独立 slug/不挂用户目录），
执行侧强制 trust/caps/exec_root（guard），权限 fail-closed 转人工。任务先回 working，
客户端 tasks/get 轮询至终态（SSE 流式属其他 P1，不在本期）。执行层**懒加载**——
本模块 import 不拖入 graph（node 无 GUI 依赖断言）。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ginno_runtime import jsonx as json

from .auth import TokenStore, token_hash
from .errors import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    A2AError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from .models import AgentSkill, Message, Part, Task, TaskState, TaskStatus

__all__ = [
    "A2ADeps",
    "AuditLog",
    "TaskRegistry",
    "TokenBucketLimiter",
    "build_a2a_app",
]

#: 设计 §12.A 默认值。
DEFAULT_RATE_PER_MIN = 30
DEFAULT_MAX_CONCURRENT = 2

#: 无 token 连接必须在此预算内被踢掉。中间件即时响应（不 hold 连接），
#: 常量仅作契约锚点，测试断言实际耗时远小于它。
AUTH_REJECT_DEADLINE_S = 3.0

#: 能力地址 path-token 形态：/a2a/<token> 及其任意子路径。
_TOKEN_PATH = re.compile(r"^/a2a/([^/]+)(?:/.*)?$")

#: 统一 401 响应体——缺 token / 错 token / 任意路径，逐字节一致。
_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'

_RPC_METHODS = ("message/send", "tasks/get", "tasks/cancel")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TokenBucketLimiter:
    """进程内令牌桶（设计 §10.2 ④ 配额限流）。

    每分钟补 ``rate_per_min`` 个令牌（连续补充），桶容量默认等于速率
    （允许一分钟的突发）。allow() 同步且只在事件循环内调用，无需锁。
    ``clock`` 可注入，测试用确定性时钟。
    """

    def __init__(
        self,
        rate_per_min: int = DEFAULT_RATE_PER_MIN,
        *,
        capacity: int | None = None,
        clock: Any = None,
    ):
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be positive")
        self.rate_per_s = rate_per_min / 60.0
        self.capacity = float(capacity if capacity is not None else rate_per_min)
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        self._tokens = self.capacity
        self._clock = clock or time.monotonic
        self._last = self._clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(
            self.capacity, self._tokens + (now - self._last) * self.rate_per_s
        )
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def retry_after_s(self) -> int:
        """距下一个可用令牌的整秒数（429 的 Retry-After 头）。"""
        deficit = max(0.0, 1.0 - self._tokens)
        return max(1, int(deficit / self.rate_per_s) + 1)


class AuditLog:
    """追加式 JSONL 审计（设计 §2.2 ⑥ / §10.2 ⑤）。

    脱敏契约：只记 token **hash 前 8 位**、method、时间、结果、task id。
    peer 消息内容、明文 token、技能描述等一律不落这里。
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self, *, method: str, token: str = "", outcome: str = "", task_id: str = ""
    ) -> None:
        entry: dict[str, Any] = {
            "ts": _utcnow(),
            "method": method,
            "token": token_hash(token)[:8] if token else "",
            "outcome": outcome,
        }
        if task_id:
            entry["task_id"] = task_id
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")


class TaskRegistry:
    """P0 内存任务表。持久化 / TTL / 重启恢复属 P1（设计 §9）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def create(self, message: Message, *, context_id: str = "") -> Task:
        task = Task(
            id=uuid.uuid4().hex,
            context_id=context_id or uuid.uuid4().hex,
            status=TaskStatus(state=TaskState.SUBMITTED, timestamp=_utcnow()),
            history=[message],
        )
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def active_count(self) -> int:
        """非终态任务数（并发上限的分子）。"""
        return sum(1 for t in self._tasks.values() if not t.is_terminal)

    def cancel(self, task_id: str) -> Task:
        """幂等取消：已 canceled 原样返回；其它终态不可取消；不存在 NotFound。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError()
        if task.state == TaskState.CANCELED:
            return task
        if task.is_terminal:
            raise TaskNotCancelableError()
        task.status = TaskStatus(state=TaskState.CANCELED, timestamp=_utcnow())
        return task


@dataclass
class A2ADeps:
    """build_a2a_app 的全部依赖——注入而非全局，测试可整体替换。

    ``executor``：入站执行器（a2a/inbound_exec.InboundExecutor）。缺省 None →
    message/send 首次派活时从 settings 枢纽块懒构建（executor_from_settings）并缓存。
    测试可注入真执行器（自环 e2e）或桩（仅测 server 状态机 plumbing）。
    ``background``：在跑的执行协程集合（停服/收尾清理用）。
    """

    tokens: TokenStore
    audit: AuditLog
    limiter: TokenBucketLimiter = field(default_factory=TokenBucketLimiter)
    tasks: TaskRegistry = field(default_factory=TaskRegistry)
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    agent_name: str = "Ginno"
    agent_version: str = "0.0.0"
    skills: Sequence[AgentSkill] = ()
    executor: Any = None
    background: set = field(default_factory=set)


# --------------------------------------------------------------------------- #
# ASGI 中间件（纯 ASGI，照 server.py SEC-02 的写法，不用 BaseHTTPMiddleware）
# --------------------------------------------------------------------------- #


async def _send_json(
    send: Any, status: int, body: bytes, *, extra_headers: Sequence = ()
) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class _AuthGate:
    """鉴权 + 限流闸，跑在路由**之前**。

    未鉴权调用者无法探测哪些路径存在：任何失败形态都回逐字节相同的 401，
    且即时响应（远在 AUTH_REJECT_DEADLINE_S 预算内，不挂连接）。
    通过鉴权后才轮到路由——未注册的路径（含全部 R3）自然 404。
    """

    def __init__(self, app: Any, deps: A2ADeps):
        self.app = app
        self.deps = deps

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "websocket":
            # P0 无 WS 面——fail-closed 直接关，绝不裸 accept()。
            await send({"type": "websocket.close", "code": 4401})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        match = _TOKEN_PATH.match(path)
        token = match.group(1) if match else ""
        if not token or not self.deps.tokens.verify(token):
            self.deps.audit.record(
                method=f"http:{scope.get('method', '')}",
                token=token,
                outcome="auth_failed",
            )
            await _send_json(
                send,
                401,
                _UNAUTHORIZED_BODY,
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return
        if not self.deps.limiter.allow():
            self.deps.audit.record(
                method=f"http:{scope.get('method', '')}",
                token=token,
                outcome="rate_limited",
            )
            await _send_json(
                send,
                429,
                b'{"error":"rate_limited"}',
                extra_headers=[
                    (b"retry-after", str(self.deps.limiter.retry_after_s()).encode("ascii"))
                ],
            )
            return
        scope["a2a_token"] = token
        await self.app(scope, receive, send)


class _SecurityHeaders:
    """SEC-02 基线安全头（与 server.py 同一组；刻意本地复制而非 import——
    import 主 server 会把整个主 app 拖进这个隔离 listener）。"""

    HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"SAMEORIGIN"),
    )

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in self.HEADERS if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


# --------------------------------------------------------------------------- #
# JSON-RPC 处理
# --------------------------------------------------------------------------- #


def _rpc_result(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})


def _rpc_error(req_id: Any, err: A2AError) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": req_id, "error": err.to_rpc()})


def _task_dict(task: Task) -> dict:
    """Task 的 wire 形态（models.Task 只有 from_dict，序列化在本层自包含）。"""
    out: dict[str, Any] = {
        "id": task.id,
        "contextId": task.context_id,
        "status": task.status.to_dict(),
    }
    if task.artifacts:
        out["artifacts"] = [a.to_dict() for a in task.artifacts]
    if task.history:
        out["history"] = [m.to_dict() for m in task.history]
    if task.metadata:
        out["metadata"] = task.metadata
    return out


def _public_card(deps: A2ADeps) -> dict:
    """分级披露（§10.5）：skills 只回 id+name。description/examples/tags 既是
    敏感业务信息又是注入载体，P0 绝不出本 app（认证扩展 Card 属 P1）。
    构造全新 dict 而非过滤完整 Card——结构上杜绝漏字段。"""
    return {
        "name": deps.agent_name,
        "version": deps.agent_version,
        "protocolVersion": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {
            "bearer": {
                "type": "http",
                "scheme": "bearer",
                "description": (
                    "Capability token rides in the URL path /a2a/<token>/ "
                    "(address-as-credential, design 6.2)."
                ),
            }
        },
        "security": [{"bearer": []}],
        "skills": [{"id": s.id, "name": s.name} for s in deps.skills],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
    }


def _resolve_executor(deps: A2ADeps) -> Any:
    """取入站执行器；返回 None 表示入站门控关闭（保留 P0 submitted 占位）。

    判定（设计 §12.A / 验收 #14 入站默认关闭）：
    - 显式注入 ``deps.executor`` → 直接用（测试/桌面端）。
    - 否则读 settings 枢纽块 ``inbound_enabled``：开启 → 懒构建真执行器并缓存
      （node resolve_config 会写 inbound_enabled=True）；关闭 → None（P0 占位）。

    **懒加载**：import inbound_exec 只发生在首次派活时——本模块 import 不拖入
    graph（node 无 GUI 依赖断言，test_a2a_node::test_no_gui_import_chain）。
    exec_root 非法 → fail-loud 抛 ExecRootError（调用方转 RPC error，不静默降级）。
    """
    if deps.executor is not None:
        return deps.executor
    from .inbound_exec import _load_hub, executor_from_settings

    if not _load_hub().get("inbound_enabled"):
        return None
    deps.executor = executor_from_settings()
    return deps.executor


async def _run_inbound_task(
    task: Task, message: Message, deps: A2ADeps, executor: Any, token: str
) -> None:
    """后台跑一条入站任务至终态，原地更新 Task（tasks/get 读同一对象）。

    执行层返回 ExecResult（state/text/artifacts/denials/session_id）→ 映射到 Task。
    权限挂起 → input-required（#7 转人工，远端无自批路径）；guard 拒绝记入
    metadata.guard_denials（实证可查）；产物以 Artifact 回路径+元信息（§2.5）。
    """
    try:
        result = await executor.run(
            instruction=message.text(),
            context_id=task.context_id,
            peer_name=str(message.metadata.get("peer_name") or "") or "peer",
            task_id=task.id,
        )
        task.artifacts = list(result.artifacts)
        if result.session_id:
            task.metadata["session_id"] = result.session_id
        if result.denials:
            task.metadata["guard_denials"] = result.denials
        body = result.text or result.error or ""
        task.status = TaskStatus(
            state=result.state,
            message=Message(
                role="agent",
                parts=[Part(text=body)] if body else [],
                task_id=task.id,
                context_id=task.context_id,
            ),
            timestamp=_utcnow(),
        )
        deps.audit.record(
            method="message/send", token=token, outcome=result.state.value, task_id=task.id
        )
    except Exception as exc:  # noqa: BLE001 — 执行异常转 failed，绝不悬空 working
        task.status = TaskStatus(
            state=TaskState.FAILED,
            message=Message(role="agent", parts=[Part(text=f"{type(exc).__name__}: {exc}")]),
            timestamp=_utcnow(),
        )
        deps.audit.record(
            method="message/send", token=token, outcome="failed", task_id=task.id
        )


def _message_send(req_id: Any, params: dict, deps: A2ADeps, token: str) -> JSONResponse:
    raw_msg = params.get("message")
    if not isinstance(raw_msg, dict):
        return _rpc_error(
            req_id, A2AError("params.message (object) required", code=INVALID_PARAMS)
        )
    message = Message.from_dict(raw_msg)
    if not message.parts:
        return _rpc_error(
            req_id, A2AError("message.parts must not be empty", code=INVALID_PARAMS)
        )
    if deps.tasks.active_count() >= deps.max_concurrent:
        deps.audit.record(
            method="message/send", token=token, outcome="concurrency_limited"
        )
        return JSONResponse(
            {"error": "too_many_concurrent_tasks"},
            status_code=429,
            headers={"Retry-After": "5"},
        )
    # 执行器先解析（exec_root 非法 fail-loud → RPC error，不留悬空 task）。
    try:
        executor = _resolve_executor(deps)
    except A2AError as exc:
        deps.audit.record(method="message/send", token=token, outcome=exc.name)
        return _rpc_error(req_id, exc)
    except Exception as exc:  # noqa: BLE001 — ExecRootError 等配置非法
        deps.audit.record(method="message/send", token=token, outcome="exec_config_error")
        return _rpc_error(
            req_id, A2AError(f"inbound exec config error: {exc}", code=INTERNAL_ERROR)
        )
    # contextId 客户端给了就接受（规范允许），没给则 server 生成；映射 Ginno
    # session 由执行层照渠道范式落实（type="a2a"/独立 slug/不挂用户目录）。
    task = deps.tasks.create(message, context_id=message.context_id)
    if executor is None:
        # 入站门控关闭（inbound_enabled=false，P0 默认）：保留 submitted 占位。
        deps.audit.record(
            method="message/send", token=token, outcome="submitted", task_id=task.id
        )
        return _rpc_result(req_id, _task_dict(task))
    # 真执行：后台协程跑 turn 链，先回 working（客户端 tasks/get 轮询至终态）。
    task.status = TaskStatus(state=TaskState.WORKING, timestamp=_utcnow())
    bg = asyncio.create_task(_run_inbound_task(task, message, deps, executor, token))
    deps.background.add(bg)
    bg.add_done_callback(deps.background.discard)
    deps.audit.record(
        method="message/send", token=token, outcome="working", task_id=task.id
    )
    return _rpc_result(req_id, _task_dict(task))


def _tasks_get(req_id: Any, params: dict, deps: A2ADeps, token: str) -> JSONResponse:
    task_id = str(params.get("id") or "")
    if not task_id:
        return _rpc_error(req_id, A2AError("params.id required", code=INVALID_PARAMS))
    task = deps.tasks.get(task_id)
    if task is None:
        raise TaskNotFoundError()
    deps.audit.record(method="tasks/get", token=token, outcome="ok", task_id=task_id)
    return _rpc_result(req_id, _task_dict(task))


def _tasks_cancel(req_id: Any, params: dict, deps: A2ADeps, token: str) -> JSONResponse:
    task_id = str(params.get("id") or "")
    if not task_id:
        return _rpc_error(req_id, A2AError("params.id required", code=INVALID_PARAMS))
    task = deps.tasks.cancel(task_id)  # 幂等；NotFound/NotCancelable 抛 A2AError
    deps.audit.record(method="tasks/cancel", token=token, outcome="canceled", task_id=task_id)
    return _rpc_result(req_id, _task_dict(task))


async def _dispatch_rpc(
    request: Request, deps: A2ADeps, *, only: str | None = None
) -> JSONResponse:
    """JSON-RPC 2.0 分发。``only`` 限定专用路由只认自己的 method。"""
    token = request.scope.get("a2a_token", "")
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return _rpc_error(None, A2AError("Parse error", code=PARSE_ERROR))
    if (
        not isinstance(body, dict)
        or body.get("jsonrpc") != "2.0"
        or not isinstance(body.get("method"), str)
        or "id" not in body
    ):
        req_id = body.get("id") if isinstance(body, dict) else None
        return _rpc_error(req_id, A2AError("Invalid Request", code=INVALID_REQUEST))
    req_id, method = body["id"], body["method"]
    params = body.get("params")
    params = params if isinstance(params, dict) else {}
    if method not in _RPC_METHODS or (only is not None and method != only):
        return _rpc_error(
            req_id, A2AError(f"Method not found: {method}", code=METHOD_NOT_FOUND)
        )
    try:
        if method == "message/send":
            return _message_send(req_id, params, deps, token)
        if method == "tasks/get":
            return _tasks_get(req_id, params, deps, token)
        return _tasks_cancel(req_id, params, deps, token)
    except A2AError as exc:
        deps.audit.record(method=method, token=token, outcome=exc.name)
        return _rpc_error(req_id, exc)


# --------------------------------------------------------------------------- #
# App 组装
# --------------------------------------------------------------------------- #


def build_a2a_app(deps: A2ADeps) -> FastAPI:
    """组独立入站 app。**绝不 mount 进主 server**；由 inbound.py 单独起服。

    路由白名单即下方四个逻辑端点——R3 类路径（settings/mcp/skills/agents/
    providers/hooks…）根本不注册，探测得 404（§11.2）。docs/openapi 一并
    关掉，不留额外注册面。无 CORS 中间件：浏览器跨域调用默认被拦。
    """
    app = FastAPI(
        title="Ginno A2A Inbound",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.a2a_deps = deps

    @app.get("/a2a/{token}/.well-known/agent-card.json")
    async def agent_card(token: str, request: Request) -> JSONResponse:
        deps.audit.record(method="agent-card", token=token, outcome="ok")
        return JSONResponse(_public_card(deps))

    # 同一处理函数注册带/不带尾斜杠两个形态（本仓 client POST 无尾斜杠端点，
    # httpx 默认不跟随 307）。逻辑上仍是同一个路由。
    @app.post("/a2a/{token}")
    @app.post("/a2a/{token}/")
    async def rpc_root(token: str, request: Request) -> JSONResponse:
        return await _dispatch_rpc(request, deps)

    @app.post("/a2a/{token}/tasks/get")
    async def rpc_tasks_get(token: str, request: Request) -> JSONResponse:
        return await _dispatch_rpc(request, deps, only="tasks/get")

    @app.post("/a2a/{token}/tasks/cancel")
    async def rpc_tasks_cancel(token: str, request: Request) -> JSONResponse:
        return await _dispatch_rpc(request, deps, only="tasks/cancel")

    # 后加的包在外层：安全头覆盖一切响应（含 401/429），鉴权在路由之前。
    app.add_middleware(_AuthGate, deps=deps)
    app.add_middleware(_SecurityHeaders)
    return app

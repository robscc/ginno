"""A2A 入站执行层（N2b）—— 桌面端入站与 ``ginno node`` 共用的真执行模块。

权威设计：
- ``docs/design/ginno-node-headless-design.md`` §2.3（任务执行）/§2.4（trust 强制）/§2.5（产物）
- ``docs/design/a2a-connector-design.md`` §8（Session 管理）/§10.2（五层 fail-closed）
- 验收 #7/#8（权限 fail-closed）、#17/#18/#19（会话隔离/自愈）、#31/#32/#33（trust 强制）、#40（killpg）

**为什么存在**：P0 的 ``server.py`` 只返回 ``submitted`` 占位（不跑 agent）。本模块把
``message/send`` 接到真执行，一次实现两处受益（桌面端入站 + node，设计 §1 推论）。

执行链（与 ``ginno exec`` 同一条 turn 链，docs/headless.md）：
``create_session → _run_stream(ws=None)``，完全进程内（无远端 attach、无 WS）。

进程模型与 #40 的 reconciliation（重要，显式声明）：
- turn 链本身**进程内**跑（create_session → _run_stream），但整条 turn 跑在一个
  **专用 worker 子进程**里（``start_new_session=True`` → 独立进程组）。
- 这样超时才能 ``killpg``（SIGTERM→2s→SIGKILL）干净清理 turn 及其 bash 孙进程
  （bash 工具 Popen 不带 start_new_session，孙进程落在 worker 组内 → killpg 一锅端）。
  若在 server 进程内直接跑 turn，killpg 会杀掉 server 自身——故必须子进程隔离。
- 「完全进程内」指 turn 链在 worker 内进程内执行（非远端 attach），与子进程隔离不矛盾。

三重强制（§2.4，执行侧，不能只存储）：
- 经 :class:`GuardHookDispatcher` 注入 graph 的 permission 节点（PreToolUse hook，
  bypass 与否都跑）→ 每个工具调用执行前调 ``guard.check_tool``（trust×caps×exec_root）。
  拒绝 → blocked bubble（模型可自愈），并记录 denial 供审计/回报。
- 权限 fail-closed（#7）：worker 钉死 ``bypass_permissions=False``；guard 放行但策略
  判 ask 的工具 → interrupt 挂起 → 任务转 ``input-required``（等本机人工批准）。
  远端**无协议路径自批**（本期不做 resume 闭环）→ 无法自批。

会话隔离（#17/#18/#19）：``type="a2a"`` / ``channel="a2a"`` / 独立 slug
``a2a-inbound`` / **不挂用户目录**（只挂 exec_root 专用工作区）/ 读不到用户其他会话。
映射照 ``channels/base.ensure_channel_session`` 范式：contextId→session_id 落
``a2a/inbound-contexts.json``；会话被删 → 自愈重开（#19）。

产物（§2.5）：留在 exec_root，Task 以 ``Artifact`` 回**路径+摘要+元信息**，
**不传文件内容**（L2 文件传取属独立 P1，不在本期）。

不做（设计 §5 / 任务边界）：SSE 流式、input-required resume 闭环、TTL 两态、重启恢复。
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ginno_runtime import jsonx as json
from ginno_runtime import paths

from . import guard
from .models import Artifact, Part, TaskState

__all__ = [
    "ExecPolicy",
    "ExecResult",
    "GuardHookDispatcher",
    "InboundExecutor",
    "executor_from_settings",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_SLUG",
    "DEFAULT_TITLE_PREFIX",
]

#: 设计 §12.A 默认值。
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_SLUG = "a2a-inbound"
DEFAULT_TITLE_PREFIX = "A2A · "

#: worker 子进程入口（不经过 cli.py —— 该文件属其他子代理，禁改）。
_WORKER_BOOT = "from ginno_runtime.a2a.inbound_exec import _worker_main; _worker_main()"

#: 产物扫描上限（避免巨量文件撑爆 JSON-RPC 响应）。
_MAX_ARTIFACTS = 50
#: 产物元信息里读取的「摘要」字节上限（仅用于判定文本/二进制，不回传内容）。
_SNIFF_BYTES = 256


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 策略（trust / caps / exec_root / slug / timeout）
# --------------------------------------------------------------------------- #
@dataclass
class ExecPolicy:
    """入站执行策略。默认最严（readonly / L0），与 connectors/a2a 一致。"""

    trust: str = "readonly"
    caps: list[str] = field(default_factory=lambda: ["L0"])
    exec_root: str = guard.DEFAULT_EXEC_ROOT
    slug: str = DEFAULT_SLUG
    title_prefix: str = DEFAULT_TITLE_PREFIX
    timeout_s: float = DEFAULT_TIMEOUT_S

    def to_dict(self) -> dict:
        return {
            "trust": self.trust,
            "caps": list(self.caps),
            "exec_root": self.exec_root,
            "slug": self.slug,
            "title_prefix": self.title_prefix,
            "timeout_s": self.timeout_s,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ExecPolicy":
        raw = raw or {}
        return cls(
            trust=str(raw.get("trust") or "readonly"),
            caps=[str(c) for c in (raw.get("caps") or ["L0"])],
            exec_root=str(raw.get("exec_root") or guard.DEFAULT_EXEC_ROOT),
            slug=str(raw.get("slug") or DEFAULT_SLUG),
            title_prefix=str(raw.get("title_prefix") or DEFAULT_TITLE_PREFIX),
            timeout_s=float(raw.get("timeout_s") or DEFAULT_TIMEOUT_S),
        )


def _load_hub() -> dict:
    """读 settings.connectors.a2a 枢纽块（照 node._load_hub 同款读法）。"""
    try:
        settings = json.loads(paths.settings_path().read_text() or "{}")
    except (OSError, ValueError):
        return {}
    hub = (settings.get("connectors") or {}).get("a2a") or {}
    return dict(hub) if isinstance(hub, dict) else {}


def executor_from_settings() -> "InboundExecutor":
    """从 settings 枢纽块解析策略 → 执行器。桌面端入站与 node 共用同一存储位。

    exec_root 非法 → fail-loud（resolve_exec_root 抛 ExecRootError），不静默降级。
    """
    hub = _load_hub()
    trust = str(hub.get("trust") or "").strip()
    if trust not in guard.TRUST_LEVELS:
        trust = "readonly"
    caps_raw = hub.get("caps")
    caps = (
        [str(c) for c in caps_raw if str(c) in guard.CAP_LEVELS]
        if isinstance(caps_raw, list) and caps_raw
        else []
    ) or ["L0"]
    timeout = hub.get("inbound_task_timeout_s")
    policy = ExecPolicy(
        trust=trust,
        caps=caps,
        exec_root=guard.resolve_exec_root(hub.get("inbound_exec_root")),
        slug=str(hub.get("inbound_project_slug") or DEFAULT_SLUG),
        title_prefix=str(hub.get("inbound_session_title_prefix") or DEFAULT_TITLE_PREFIX),
        timeout_s=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else DEFAULT_TIMEOUT_S,
    )
    return InboundExecutor(policy)


# --------------------------------------------------------------------------- #
# 执行侧 guard 强制（PreToolUse hook，注入 permission 节点）
# --------------------------------------------------------------------------- #
def _guard_kwargs(tool_name: str, args: dict, exec_root: str) -> dict:
    """从工具参数提取 guard 所需的 target_path / command（按工具名映射）。

    bash/repl 的 cwd 绑定到 exec_root（primary folder），故 target_path=root；
    文件工具取 path；glob/grep 取 root/path。无路径参数的工具省略 target_path
    （guard 仍做 trust×caps 判定，只是不校验边界）。
    """
    kw: dict[str, Any] = {"exec_root": exec_root}
    name = (tool_name or "").lower()
    if name in ("bash", "repl"):
        kw["command"] = str(args.get("command") or "")
        kw["target_path"] = exec_root
    elif name in ("read_file", "write_file", "edit_file", "apply_patch", "parse_document"):
        p = args.get("path") or args.get("file_path")
        if p:
            kw["target_path"] = str(p)
    elif name in ("glob_files", "grep_files"):
        p = args.get("root") or args.get("path")
        if p:
            kw["target_path"] = str(p)
    return kw


class GuardHookDispatcher:
    """把 ``guard.check_tool`` 接进 graph permission 节点的 PreToolUse hook 链。

    permission 节点对每个工具调用 dispatch PreToolUse（**bypass 与否都跑**，hooks
    是用户权威规则）→ 本 dispatcher 先做 trust×caps×exec_root 三重判定：
    拒绝 → ``HookResult(block=True)`` → permission 节点回 blocked bubble（工具绝不
    执行），并记录 denial。放行 → 透传底层 dispatcher（用户 settings hooks 照跑）。

    与派发侧共用 ``guard.check_tool`` 同一判定逻辑 → 双侧一致，无 accept-then-ignore。
    """

    def __init__(self, policy: ExecPolicy, underlying: Any = None):
        self.policy = policy
        self.underlying = underlying
        self.denials: list[dict] = []

    async def dispatch(
        self, event: Any, matcher: str | None = None, *, plugin_hooks: bool = True
    ) -> list:
        from ..hooks.dispatcher import HookResult

        results: list = []
        if getattr(event, "name", "") == "PreToolUse":
            ctx = getattr(event, "context", None) or {}
            name = str(ctx.get("tool") or matcher or "")
            args = ctx.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            decision = guard.check_tool(
                self.policy.trust,
                self.policy.caps,
                name,
                **_guard_kwargs(name, args, self.policy.exec_root),
            )
            if not decision.allowed:
                self.denials.append(
                    {"tool": name, "code": decision.code, "reason": decision.reason}
                )
                results.append(
                    HookResult(
                        block=True,
                        reason=f"[a2a-guard:{decision.code}] {decision.reason}",
                    )
                )
                # guard 拒绝是权威判定：短路，不再跑底层 hooks（工具已被拦）。
                return results
        if self.underlying is not None:
            results.extend(
                await self.underlying.dispatch(event, matcher, plugin_hooks=plugin_hooks)
            )
        return results


# --------------------------------------------------------------------------- #
# 会话映射（contextId → session_id，照渠道范式 + 自愈）
# --------------------------------------------------------------------------- #
def _context_map_path() -> Path:
    return paths.a2a_dir() / "inbound-contexts.json"


def _load_context_map() -> dict:
    try:
        data = json.loads(_context_map_path().read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_context_map(mapping: dict) -> None:
    p = _context_map_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def _bind_guard_graph(s: dict, guard_dispatcher: GuardHookDispatcher) -> None:
    """用当前 guard dispatcher 重建会话 graph（create/rebuild/cache 三态统一）。

    显式传 hook_dispatcher 而非依赖全局 —— 保证 follow-up 复用缓存会话时 guard
    仍是当前策略（不会绑到上一轮的旧 dispatcher）。同时把全局 _hooks 也设为 guard
    （worker 是隔离进程，安全），兜住 MCP drift 重建（_maybe_refresh_session_graph
    用 shared._hooks）不丢 guard。
    """
    from .. import server_shared as shared
    from ..classifier.middlewares import build_middlewares
    from ..graph import build_all_tools, build_graph
    from ..tool_policy import filter_mcp_tools

    slug = s["project_slug"]
    sid = s["session_id"]
    mcp_tools = filter_mcp_tools(
        shared._mcp.all_langchain_tools() if shared._mcp else [], s
    )
    all_tools = build_all_tools(
        mcp_tools,
        workspace=str(s.get("workspace") or ""),
        project_slug=slug,
        session_id=sid,
        context_dirs=s.get("context_dirs") or [],
        primary_path=s.get("primary_path") or "",
    )
    router, tool_selector = build_middlewares(sid)
    s["graph"] = build_graph(
        model=s["model"],
        project_slug=slug,
        workspace=str(s.get("workspace") or ""),
        mcp_tools=mcp_tools,
        hook_dispatcher=guard_dispatcher,
        all_tools=all_tools,
        model_getter=shared.make_session_model_getter(sid, s["model"]),
        router=router,
        tool_selector=tool_selector,
    )
    s["all_tool_names"] = [t.name for t in all_tools]


async def _prepare_session(
    policy: ExecPolicy,
    guard_dispatcher: GuardHookDispatcher,
    *,
    session_id: str | None,
    peer_name: str,
) -> tuple[dict, str]:
    """照 ensure_channel_session 范式：有值从磁盘重建，被删自愈重开，为空新建。

    新建：``type="a2a"`` / ``channel="a2a"`` / 独立 slug / **只挂 exec_root**（不挂
    用户目录，#17）。返回 (session, session_id)。重建失败且 meta 仍在 → 抛错（不静默
    换新会话，照渠道范式）。
    """
    from .. import context_folders as cf
    from ..api.sessions import CreateSessionRequest, _ensure_session, create_session
    from ..session_meta import _find_meta

    exec_root = guard.resolve_exec_root(policy.exec_root)
    Path(exec_root).mkdir(parents=True, exist_ok=True)

    if session_id:
        s = _ensure_session(session_id)
        if s:
            _bind_guard_graph(s, guard_dispatcher)
            return s, session_id
        if not _find_meta(session_id):
            # 映射指向已删除的会话 → 落新建路径重开（#19 自愈）。
            session_id = None
        else:
            raise RuntimeError(
                f"a2a inbound session {session_id} rebuild failed (meta present); refusing silent switch"
            )

    folder = cf.add_folder(exec_root, name="a2a inbound workspace", auto_mount=False)
    req = CreateSessionRequest(
        project_slug=policy.slug,
        workspace="",
        title=f"{policy.title_prefix}{peer_name or 'peer'}",
        type="a2a",
        channel="a2a",
        context_folders=[folder["id"]],
        primary_folder=folder["id"],
    )
    meta = await create_session(req)
    if not isinstance(meta, dict) or meta.get("ok") is False or not meta.get("id"):
        raise RuntimeError(str((meta or {}).get("error") or "create_session failed"))
    sid = str(meta["id"])
    s = _ensure_session(sid)
    if not s:
        raise RuntimeError("a2a inbound session not ready after create")
    _bind_guard_graph(s, guard_dispatcher)
    return s, sid


# --------------------------------------------------------------------------- #
# worker 内的 settings 钉死（fail-closed + 隔离）
# --------------------------------------------------------------------------- #
def _write_inbound_settings(policy: ExecPolicy) -> None:
    """worker 内打补丁：bypass_permissions=False（#7 fail-closed）+ 上下文隔离。

    与 cli._write_exec_settings 的 headless 放行相反——入站**绝不** bypass。

    **权限放行面按 trust 派生**（2026-11-16 修）：
    原实现不论 trust 一律只放行 3 个读工具，写/shell 一律 ask。但 node 是
    headless 无 UI、A2A 的 input-required 远程批准闭环尚未实现，于是
    trust=normal 的节点**连 `pwd && ls -la` 都会挂起等一个永远不来的人工批准**
    ——实测远端 VPS 派活即卡死在此（会话 jsonl 里的 __interrupt__ 是
    permission_request/tool=bash/command="pwd && ls -la"）。

    改为：readonly 仍只放行读工具；normal/full 额外放行写与 shell。
    **这不削弱安全**——guard 在每个工具调用前跑（PreToolUse hook，bypass 与否
    都跑），强制 trust×caps×exec_root 三重判定，比权限层更严：
      · readonly 即使被误放行写工具，guard 仍拒（readonly_write_forbidden）
      · exec_root 越界 guard fail-loud 拒绝（exec_root_violation）
      · full 仍拒改配置（guard 对 update_config 恒 False）
    权限层与 guard 对齐后，语义才是「trust=normal 能在 exec_root 内真干活」。
    尊重已有 deny/ask（操作员显式收紧优先）。
    """
    sp = paths.settings_path()
    try:
        cfg = json.loads(sp.read_text() or "{}") if sp.exists() else {}
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["bypass_permissions"] = False
    ctx = dict(cfg.get("context") or {})
    ctx["world_state"] = False
    ctx["external_agents_enabled"] = False
    cfg["context"] = ctx
    perms = cfg.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
    existing = " ".join(
        str(x) for bucket in ("allow", "deny", "ask") for x in perms.get(bucket, []) or []
    ).lower()
    allow = list(perms.get("allow") or [])
    # L0 只读观测工具默认放行（guard 仍限 exec_root 内）。
    wanted = ["read_file", "glob_files", "grep_files"]
    if policy.trust in ("normal", "full"):
        # 写与 shell：guard 已按 exec_root 边界 + caps 阶梯强制，此处对齐放行，
        # 否则 headless 节点会挂起等一个不存在的人工批准。
        wanted += ["write_file", "edit_file", "apply_patch", "bash", "repl"]
    for name in wanted:
        if name not in existing and name not in allow:
            allow.append(name)
    perms["allow"] = allow
    cfg["permissions"] = perms
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 产物扫描（路径 + 摘要 + 元信息，不传内容，§2.5）
# --------------------------------------------------------------------------- #
def _snapshot(root: str) -> dict[str, tuple[int, int]]:
    """exec_root 递归快照：relpath → (size, mtime_ns)。忽略隐藏目录与扫描失败。"""
    out: dict[str, tuple[int, int]] = {}
    base = Path(root)
    if not base.is_dir():
        return out
    for p in base.rglob("*"):
        try:
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(base).as_posix()
            if any(part.startswith(".") for part in p.relative_to(base).parts):
                continue
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns)
        except OSError:
            continue
    return out


def _collect_artifacts(root: str, before: dict, after: dict) -> list[Artifact]:
    """新增/变更文件 → Artifact（路径+元信息；**不读内容回传**）。"""
    arts: list[Artifact] = []
    changed = sorted(
        rel for rel, meta in after.items() if before.get(rel) != meta
    )
    for rel in changed[:_MAX_ARTIFACTS]:
        size, mtime_ns = after[rel]
        abs_path = str(Path(root) / rel)
        kind = _sniff_kind(Path(root) / rel)
        mtime_iso = datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).isoformat(timespec="seconds")
        summary = f"{rel} ({kind}, {size} bytes, modified {mtime_iso})"
        arts.append(
            Artifact(
                artifact_id=uuid.uuid4().hex,
                name=rel,
                description="Inbound artifact: path + metadata only (file content not transferred; L2 fetch out of scope).",
                parts=[Part(text=f"path: {abs_path}\n{summary}")],
                metadata={
                    "path": abs_path,
                    "relpath": rel,
                    "size_bytes": size,
                    "modified": mtime_iso,
                    "kind": kind,
                    "exec_root": str(root),
                },
            )
        )
    return arts


def _sniff_kind(p: Path) -> str:
    """仅读首 _SNIFF_BYTES 判定文本/二进制（不回传内容）。

    ⚠️ 必须用**增量解码器**而非 head.decode()：截断点可能正好落在多字节
    字符中间（中文 3 字节、emoji 4 字节），裸 decode 会抛
    "unexpected end of data" 把**纯文本误判成 binary**。
    实测：一份 6996 字节的中文 markdown 报告，第 256 字节是 0xe8
    （某汉字首字节）→ 旧实现判 binary，全文 decode 其实完全正常。

    incremental decoder 配 final=False 会把尾部不完整序列缓存而非报错，
    因此：尾部截断 → text；中间出现非法字节 / NUL → binary（真二进制仍被拒）。
    """
    try:
        with p.open("rb") as fh:
            head = fh.read(_SNIFF_BYTES)
        if b"\x00" in head:
            return "binary"
        # final=False：容忍尾部被截断的多字节序列，不代表整文件合法——
        # 这里只判「开头看起来是不是文本」，与原语义一致。
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
        return "text"
    except (OSError, UnicodeDecodeError):
        return "binary"


# --------------------------------------------------------------------------- #
# 进程内 turn 链（worker 核心；单测可直接 await）
# --------------------------------------------------------------------------- #
async def _run_job(job: dict) -> dict:
    """在**当前进程内**跑一条入站 turn（create_session → _run_stream，ws=None）。

    worker 子进程与单测共用本函数。job 字段：instruction / session_id / context_id /
    peer_name / policy(dict)。返回 result dict（state/text/error/session_id/
    artifacts/denials/parked）。任何异常向上抛（_worker_main 兜底转 failed）。
    """
    from .. import server_shared as shared
    from .. import subagent_types
    from ..agents import registry as agents_reg
    from ..api import stream as stream_api
    from ..checkpointer import FileCheckpointer
    from ..hooks.dispatcher import HookDispatcher
    from ..scheduler import _last_assistant_text
    from ..session_meta import _find_meta
    from ..tools.ask_tools import reset_interactive, set_interactive

    policy = ExecPolicy.from_dict(job.get("policy") or {})
    instruction = str(job.get("instruction") or "")
    context_id = str(job.get("context_id") or "")
    peer_name = str(job.get("peer_name") or "")

    paths.ensure_layout()
    _write_inbound_settings(policy)
    subagent_types.ensure_seeded()
    agents_reg.ensure_seeded()

    guard_dispatcher = GuardHookDispatcher(policy, underlying=HookDispatcher.from_settings())
    # worker 是隔离进程：全局 _hooks 设 guard 兜住 MCP drift 重建（见 _bind_guard_graph）。
    shared._hooks = guard_dispatcher

    exec_root = guard.resolve_exec_root(policy.exec_root)
    Path(exec_root).mkdir(parents=True, exist_ok=True)
    before = _snapshot(exec_root)

    # 会话映射：contextId → session_id（落盘，跨 worker 进程稳定）。
    mapping = _load_context_map()
    mapped_sid = job.get("session_id") or mapping.get(context_id) or None
    session, session_id = await _prepare_session(
        policy, guard_dispatcher, session_id=mapped_sid, peer_name=peer_name
    )
    if context_id:
        mapping[context_id] = session_id
        _save_context_map(mapping)

    slug = session["project_slug"]
    turn_id = uuid.uuid4().hex
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": session.get("agent_id"),
            "turn_id": turn_id,
            "user_text": instruction,
            "usage_source": "a2a-inbound",
        }
    }
    shared._RUNNING_TURNS[session_id] = turn_id
    ask_tok = set_interactive(False)  # 入站无人应答 ask_user → 模型自决
    error: str | None = None
    try:
        await stream_api._run_stream(
            None, session["graph"], config, instruction, session, session.get("agent_id"),
            origin="schedule",
        )
    except Exception as exc:  # noqa: BLE001 — turn 崩溃转 failed，不带走 worker
        error = f"{type(exc).__name__}: {exc}"
    finally:
        reset_interactive(ask_tok)
        if shared._RUNNING_TURNS.get(session_id) == turn_id:
            shared._RUNNING_TURNS.pop(session_id, None)

    # 权限挂起检测（#7）：guard 放行但策略 ask → interrupt 挂起 → input-required。
    parked = False
    try:
        pending = FileCheckpointer(slug).get_pending_interrupt(
            {"configurable": {"thread_id": session_id}}
        )
        if pending:
            parked = True
    except Exception:  # noqa: BLE001
        parked = False

    text = await _last_assistant_text(session_id, slug)
    if error is None:
        found = _find_meta(session_id)
        if found:
            last_err = found[0].get("last_error")
            if isinstance(last_err, dict) and last_err.get("message"):
                error = str(last_err["message"])
            elif last_err:
                error = str(last_err)

    after = _snapshot(exec_root)
    artifacts = _collect_artifacts(exec_root, before, after)

    if parked:
        state = TaskState.INPUT_REQUIRED
    elif error:
        state = TaskState.FAILED
    else:
        state = TaskState.COMPLETED

    return {
        "state": state.value,
        "text": text,
        "error": error,
        "session_id": session_id,
        "context_id": context_id,
        "artifacts": [a.to_dict() for a in artifacts],
        "denials": list(guard_dispatcher.denials),
        "parked": parked,
    }


def _worker_main() -> None:
    """worker 子进程入口：stdin 读 job → 进程内跑 turn → result 落 result_path。

    崩溃必落 failed result（harness/server 永远拿得到终态，不靠超时兜底）。
    """
    try:
        job = json.load(sys.stdin)
    except BaseException as exc:  # noqa: BLE001
        print(f"a2a worker: bad job spec: {exc}", file=sys.stderr)
        sys.exit(2)
    result_path = job.get("result_path")
    try:
        result = asyncio.run(_run_job(job))
    except BaseException as exc:  # noqa: BLE001 — 含 KeyboardInterrupt/SystemExit
        result = {
            "state": TaskState.FAILED.value,
            "text": "",
            "error": f"{type(exc).__name__}: {exc}",
            "session_id": str(job.get("session_id") or ""),
            "context_id": str(job.get("context_id") or ""),
            "artifacts": [],
            "denials": [],
            "parked": False,
        }
    if result_path:
        try:
            Path(result_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            print(f"a2a worker: cannot write result: {exc}", file=sys.stderr)
            sys.exit(3)


# --------------------------------------------------------------------------- #
# 结果对象 + 监督器（子进程隔离 + killpg 超时）
# --------------------------------------------------------------------------- #
@dataclass
class ExecResult:
    """一条入站任务的执行结果（server 据此更新 Task）。"""

    state: TaskState = TaskState.FAILED
    text: str = ""
    error: str | None = None
    session_id: str = ""
    context_id: str = ""
    artifacts: list[Artifact] = field(default_factory=list)
    denials: list[dict] = field(default_factory=list)
    parked: bool = False

    @classmethod
    def from_dict(cls, raw: dict) -> "ExecResult":
        raw = raw or {}
        return cls(
            state=TaskState.parse(raw.get("state")),
            text=str(raw.get("text") or ""),
            error=raw.get("error") or None,
            session_id=str(raw.get("session_id") or ""),
            context_id=str(raw.get("context_id") or ""),
            artifacts=[Artifact.from_dict(a) for a in (raw.get("artifacts") or [])],
            denials=list(raw.get("denials") or []),
            parked=bool(raw.get("parked")),
        )


async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
    """killpg 清理整个 worker 进程组：SIGTERM → 2s 宽限 → SIGKILL（#40）。

    worker 以 start_new_session 起 → 自成进程组；bash 孙进程落在组内 → 一锅端，
    孤儿进程不留存。进程已退则静默（幂等）。
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        return
    except asyncio.TimeoutError:
        pass
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()


class InboundExecutor:
    """入站执行监督器：每条任务起一个 start_new_session worker 子进程跑 turn 链，

    超时 killpg 清理。桌面端入站与 node 共用（策略来自 settings 枢纽块）。
    """

    def __init__(self, policy: ExecPolicy):
        self.policy = policy
        self._live: set[asyncio.subprocess.Process] = set()

    async def run(
        self,
        *,
        instruction: str,
        context_id: str = "",
        session_id: str | None = None,
        peer_name: str = "",
        task_id: str = "",
    ) -> ExecResult:
        """派一条任务到 worker 子进程，监督至终态/超时。返回 ExecResult。"""
        data_dir = paths.a2a_dir()
        data_dir.mkdir(parents=True, exist_ok=True)
        result_path = data_dir / f"inbound-result-{task_id or uuid.uuid4().hex}.json"
        job = {
            "instruction": instruction,
            "context_id": context_id,
            "session_id": session_id or "",
            "peer_name": peer_name,
            "policy": self.policy.to_dict(),
            "result_path": str(result_path),
        }
        env = dict(os.environ)
        env["GINNO_HOME"] = str(paths.home())
        # worker 日志落 home（排障用），不污染 server stdout。
        log_path = data_dir / f"inbound-worker-{task_id or 'x'}.log"
        proc: asyncio.subprocess.Process | None = None
        raw: dict | None = None
        timed_out = False
        try:
            with log_path.open("ab") as logfh:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", _WORKER_BOOT,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=logfh,
                    start_new_session=True,  # 独立进程组 → killpg 可一锅端（#40）
                    env=env,
                )
                self._live.add(proc)
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(job, ensure_ascii=False).encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=self.policy.timeout_s)
                except asyncio.TimeoutError:
                    timed_out = True
                    await _terminate_group(proc)
            # 读 worker 落盘的 result（在 finally unlink 之前）。
            if not timed_out:
                with contextlib.suppress(OSError, ValueError):
                    raw = json.loads(result_path.read_text(encoding="utf-8"))
        finally:
            if proc is not None:
                self._live.discard(proc)
                # 兜底：任何异常路径都不留孤儿。
                if proc.returncode is None:
                    await _terminate_group(proc)
            with contextlib.suppress(OSError):
                result_path.unlink()
        if timed_out:
            return ExecResult(
                state=TaskState.FAILED,
                error=f"inbound task timed out after {self.policy.timeout_s:g}s; process group killed (killpg)",
                context_id=context_id,
                session_id=session_id or "",
            )
        if raw is None:
            return ExecResult(
                state=TaskState.FAILED,
                error="worker produced no result (crashed before writing); see inbound-worker log",
                context_id=context_id,
                session_id=session_id or "",
            )
        return ExecResult.from_dict(raw)

    async def aclose(self) -> None:
        """清理所有在跑 worker（server 停 / 测试收尾）。幂等。"""
        for proc in list(self._live):
            await _terminate_group(proc)
        self._live.clear()

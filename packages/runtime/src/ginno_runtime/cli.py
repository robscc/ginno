"""ginno exec — headless 一次性 CLI（benchmark harness 入口）。

与桌面版共享同一条 turn 执行链（create_session → _run_stream，ws=None），
但完全进程内：不起 uvicorn、不开 WS 服务。协议照抄 scheduler 的
``_run_prompt_target``（headless 消费者的既有先例），外加三件事：

1. **隔离 home**：每次调用取一个临时 GINNO_HOME（除非 --home / GINNO_EXEC_HOME
   显式指定）——个人记忆 / 桌面状态天然不加载。注意不能默认放 <cwd> 下：
   benchmark 的 diff 捕获会把整个 home 当改动（首次 office 跑分的教训）。任务
   仓库自己的 AGENTS.md / CLAUDE.md 经 context folder 挂载仍生效（--no-world-state
   可整层关掉）。
2. **事件转录**：把一个 duck-typed 假 socket 挂进 ``_SESSION_WS[session_id]``
   （``safe_send`` 只要求 ``send_text``），将 turn 帧（token.delta / tool.* /
   usage / message.end）翻译成 claude-code stream-json 兼容的 JSONL 打到
   stdout，供 workbuddy-bench 等外部 harness 解析轨迹与 token。
3. **等待静默**：主 turn 结束不等于跑完——subagent 是同进程 asyncio task，
   结果会 wake 父会话再开新 turn。退出前等 subagent 全部 terminal、注入
   队列清空、无 open plan、父会话无 running turn。

寻址走 env（等价 claude 的 ANTHROPIC_* 三件套）::

    GINNO_EXEC_BASE_URL    OpenAI 兼容端点（bench local_proxy 或直连后端）
    GINNO_EXEC_API_KEY     API key
    GINNO_EXEC_MODEL       模型 id（同时钉死内部一切模型路由）
    GINNO_EXEC_MAX_OUTPUT_TOKENS / GINNO_EXEC_TIMEOUT_S  可选

外部代理委托（delegate_agent，claude-code/codex/pi 后端）默认禁用
（context.external_agents_enabled=False），headless 下只跑 Ginno 内置的
subagent 机制——benchmark 度量的是 Ginno 自己。

两种运行模式（启动时自动选择）：

- **远端（attach）**：本地有 Ginno app 在跑（127.0.0.1:8787 探活）就连它，
  走 REST + WS 驱动——用 app 现成的 provider/会话状态（含个人记忆，这是
  本地便利特性）。--no-attach / GINNO_EXEC_NO_ATTACH=1 强制禁用。
- **进程内（隔离）**：探不到 sidecar 时的默认——隔离 home、env 合成
  provider、无个人记忆。benchmark harness 走的是这条路。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

# stream-json 输出里作为 result 文本预览的长度（完整文本在 result.text）。
_PREVIEW_CHARS = 200
# 静默判定的复检间隔 / 稳定窗口（连续 N 次满足才算静默，规避注入合并的竞态）。
_SETTLE_POLL_S = 0.3
_SETTLE_STABLE_ROUNDS = 3
# 远端（attach 本地 Ginno app）模式的空闲判定窗口：须大于注入合并窗口
# INJECTION_COALESCE_S=10s，否则会在「最后一个 subagent 结果还没 wake 父会话」
# 时提前收尾。远端没有精确静默探针（无子代理 REST 端点），这是尽力而为；
# 进程内模式（benchmark 路径）不受此限制。
_REMOTE_IDLE_S = 20.0


def _emit(line: dict) -> None:
    """一行 JSONL 到 stdout，立刻 flush（tee 消费方依赖行完整性）。"""
    sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class _StreamTap:
    """假 WS：挂在 ``_SESSION_WS[session_id]`` 上收 turn 帧，翻译成 cc stream-json。

    ``safe_send`` 对 socket 的全部要求是 ``send_text(str)``；发送失败的对象会被
    剪除，因此这里永不抛异常。帧格式见 ``server_shared._ev``：
    ``{"event": name, "turn_id": ..., **data}``。
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self.session_id = ""
        self.subagents: list[dict] = []
        self._reset_turn()

    def _reset_turn(self) -> None:
        self._text: list[str] = []
        self._tools: list[dict] = []
        self._usage: dict | None = None

    async def send_text(self, payload: str) -> str | None:
        """收一帧，翻译成 stream-json 事件；返回帧名供驱动方做终止判定。"""
        try:
            frame = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
        ev = frame.get("event")
        if ev == "turn.start":
            self._reset_turn()
        elif ev == "token.delta":
            self._text.append(str(frame.get("content") or ""))
        elif ev == "tool.start":
            self._tools.append(
                {
                    "type": "tool_use",
                    "id": str(frame.get("id") or ""),
                    "name": str(frame.get("name") or ""),
                    "input": {},
                }
            )
        elif ev == "usage":
            turn = frame.get("turn")
            if isinstance(turn, dict):
                self._usage = turn
        elif ev == "subagent.spawned":
            self.subagents.append(
                {"session_id": frame.get("session_id"), "goal": frame.get("goal")}
            )
            _emit(
                {
                    "type": "system",
                    "subtype": "subagent_spawned",
                    **{k: frame.get(k) for k in ("session_id", "goal")},
                }
            )
        elif ev == "subagent.status":
            _emit(
                {
                    "type": "system",
                    "subtype": "subagent_status",
                    **{k: frame.get(k) for k in ("session_id", "status", "title")},
                }
            )
        elif ev == "message.end":
            # 一个 turn 落一条 assistant 消息：文本 + 工具块 + 本 turn usage。
            blocks: list[dict] = []
            text = "".join(self._text).strip()
            if text:
                blocks.append({"type": "text", "text": text})
            blocks.extend(self._tools)
            usage = self._usage or {}
            _emit(
                {
                    "type": "assistant",
                    "session_id": self.session_id,
                    "message": {
                        "model": self._model,
                        "content": blocks,
                        "usage": {
                            "input_tokens": usage.get("input_tokens"),
                            "output_tokens": usage.get("output_tokens"),
                            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
                            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
                        },
                    },
                }
            )
            self._reset_turn()
        elif ev == "error":
            _emit(
                {
                    "type": "system",
                    "subtype": "error",
                    "error": frame.get("error") or frame.get("message"),
                }
            )
        return ev


def _write_exec_settings(world_state: bool) -> None:
    """ensure_layout 之后打补丁：env 三件套 → 唯一启用的 model_config。

    写完整个 settings 只有 exec 一个 provider 且 enabled，所有内部模型路由
    （agent 绑定、摘要、分类器）都解析到它——外部 harness 钉死模型的等价物。
    """
    from . import paths
    from .providers import CONFIG_DEFAULTS

    settings_path = paths.home() / "settings.json"
    cfg: dict = {}
    if settings_path.is_file():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cfg = {}
    entry = dict(CONFIG_DEFAULTS)
    entry.update(
        {
            "id": "exec",
            "name": "Exec",
            "protocol": "openai-compatible",
            "base_url": os.environ.get("GINNO_EXEC_BASE_URL", ""),
            "api_key": os.environ.get("GINNO_EXEC_API_KEY", ""),
            "models": [os.environ.get("GINNO_EXEC_MODEL", "")],
            "default_model": os.environ.get("GINNO_EXEC_MODEL", ""),
            "enabled": True,
        }
    )
    # max_tokens 默认 0 = 不传(models._sampling 对 falsy 直接省略),由端点
    # 自决。旧默认 8192 是 office 首跑 75/150 空 final text 的根因:思考模型的
    # reasoning 计入输出预算,重任务在思考阶段耗尽预算、content 一个字不吐,
    # turn 静默结束。GINNO_EXEC_MAX_OUTPUT_TOKENS 可显式覆盖。
    max_out = os.environ.get("GINNO_EXEC_MAX_OUTPUT_TOKENS", "").strip()
    entry["max_tokens"] = int(max_out) if max_out.isdigit() else 0
    cfg["model_configs"] = [entry]
    cfg["default_config"] = "exec"
    cfg["default_provider"] = "custom"
    cfg["language"] = "en"
    cfg["bypass_permissions"] = True  # headless 无人应答 permission park，必须放行
    ctx = dict(cfg.get("context") or {})
    ctx["world_state"] = world_state
    ctx["external_agents_enabled"] = False  # 只跑内置 subagent，不委托外部 CLI
    cfg["context"] = ctx
    settings_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


async def _await_quiescence(session_id: str, slug: str, timeout_s: float) -> dict:
    """主 turn 返回后等整棵 subagent 树 + wake turn 静默。

    条件（连续 _SETTLE_STABLE_ROUNDS 次成立）：无 live 后代、本会话不在
    running/injection/waking 状态、无 open plan。超时返回 partial 标记。
    """
    from . import server_shared
    from .plans.store import all_active_plans
    from .subagent_scheduler import collect_subagent_rows
    from .subagent_scheduler._state import _INJECTION_WAKING, _PENDING_INJECTIONS, TERMINAL_STATUSES

    deadline = time.monotonic() + max(0.0, timeout_s)
    stable = 0
    while True:
        rows = collect_subagent_rows(slug, session_id, include_descendants=True)
        live = [r for r in rows if r.get("status") not in TERMINAL_STATUSES]
        tree = {session_id, *(r["session_id"] for r in rows)}
        running = any(sid in server_shared._RUNNING_TURNS for sid in tree)
        injecting = any(sid in _PENDING_INJECTIONS or sid in _INJECTION_WAKING for sid in tree)
        plans_open = any(sid in tree for _, sid, _ in all_active_plans())
        if not (live or running or injecting or plans_open):
            stable += 1
            if stable >= _SETTLE_STABLE_ROUNDS:
                return {"partial": False, "live": 0}
        else:
            stable = 0
        if time.monotonic() >= deadline:
            return {
                "partial": True,
                "live": len(live),
                "statuses": {r["session_id"]: r.get("status") for r in rows},
            }
        await asyncio.sleep(_SETTLE_POLL_S)


def _tree_usage_tokens(session_ids: set[str], since: float) -> dict:
    """汇总主会话 + 全部后代会话自 since 起的 usage（账本真值在 usage_store）。"""
    from . import usage_store

    out = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    try:
        today = time.strftime("%Y-%m-%d")
        for e in usage_store.iter_range(time.strftime("%Y-%m-%d", time.localtime(since)), today):
            if e.get("session_id") in session_ids:
                out["input_tokens"] += int(e.get("input_tokens") or 0)
                out["output_tokens"] += int(e.get("output_tokens") or 0)
                out["cache_read_input_tokens"] += int(e.get("cache_read_tokens") or 0)
                out["cache_creation_input_tokens"] += int(e.get("cache_creation_tokens") or 0)
    except Exception:  # noqa: BLE001 — usage 统计失败不致命
        pass
    return out


# --------------------------------------------------------------------------- #
# 远端模式：attach 本地正在跑的 Ginno app（sidecar），走它的 REST + WS。
# 本地便利特性——用 app 现成的 provider/会话状态；Docker 容器里探不到
# sidecar，自动落到进程内模式（benchmark 路径，隔离 home + 精确静默）。
# --------------------------------------------------------------------------- #


def _probe_sidecar(port: int) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 — 探不到就是没有
        return False


def _rest_json(port: int, method: str, path: str, body: dict | None = None) -> dict:
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


async def _run_remote(instruction: str, *, cwd: str, port: int, settle_timeout_s: float) -> int:
    from websockets.asyncio.client import connect

    started = time.time()
    # 远端模式下文件工具跑在 sidecar 进程里（cwd 不同）——把工作目录写进指令。
    task = f"Working directory for this task: {os.path.abspath(cwd)}\n\n{instruction}"
    body: dict = {"project_slug": "default", "workspace": cwd, "title": "ginno exec"}
    if os.environ.get("GINNO_EXEC_MODEL", "").strip():
        body["model"] = os.environ["GINNO_EXEC_MODEL"]
    meta = _rest_json(port, "POST", "/api/sessions", body)
    if not meta.get("id"):
        _emit(
            {
                "type": "result",
                "subtype": "error_deterministic",
                "is_error": True,
                "error": str(meta.get("error") or "remote session creation failed"),
            }
        )
        return 1
    session_id = meta["id"]
    model = str(meta.get("model_name") or os.environ.get("GINNO_EXEC_MODEL", ""))
    tap = _StreamTap(model)
    tap.session_id = session_id
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "model": model,
            "cwd": cwd,
            "attached": True,
            "port": port,
        }
    )

    deadline = time.monotonic() + max(settle_timeout_s, 60.0)
    saw_turn_end = False
    last_event: str | None = None
    try:
        async with connect(f"ws://127.0.0.1:{port}/api/ws/sessions/{session_id}") as ws:
            await ws.send(json.dumps({"type": "invoke", "message": task}))
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_REMOTE_IDLE_S)
                except TimeoutError:
                    break  # 空闲超窗口：无后续 wake/subagent 活动，收尾
                last_event = await tap.send_text(raw)
                if last_event in ("message.end", "error", "turn.stopped"):
                    saw_turn_end = True  # 但继续收：subagent 结果会 wake 新 turn
            if not saw_turn_end:
                _emit({"type": "system", "subtype": "remote_timeout", "last_event": last_event})
    except Exception as exc:  # noqa: BLE001 — 远端断连等：带着已有转录收尾
        _emit({"type": "system", "subtype": "remote_error", "error": str(exc)})

    hist = _rest_json(port, "GET", f"/api/sessions/{session_id}/history")
    text = ""
    for m in reversed(hist.get("messages") or []):
        if m.get("role") != "assistant":
            continue
        for b in m.get("blocks") or []:
            if (b.get("kind") or b.get("type")) == "text" and str(b.get("text") or "").strip():
                text = str(b["text"]).strip()
        if text:
            break
    usage = {}
    try:
        u = _rest_json(port, "GET", f"/api/usage/sessions/{session_id}").get("usage")
        if isinstance(u, dict):
            usage = {
                "input_tokens": u.get("input_tokens") or 0,
                "output_tokens": u.get("output_tokens") or 0,
                # 远端返回 store 键名，归一成 cc stream-json 形状。
                "cache_read_input_tokens": u.get("cache_read_tokens") or 0,
                "cache_creation_input_tokens": u.get("cache_creation_tokens") or 0,
            }
    except Exception:  # noqa: BLE001
        pass
    err = hist.get("last_error") or None
    err = str(err.get("message") or "") if isinstance(err, dict) else (str(err) if err else "")
    _emit(
        {
            "type": "result",
            "subtype": "error_deterministic" if err else "success",
            "is_error": bool(err),
            "duration_ms": int((time.time() - started) * 1000),
            "duration_api_ms": int((time.time() - started) * 1000),
            "num_turns": 1 + len(tap.subagents),
            "session_id": session_id,
            "text": text,
            "preview": text[:_PREVIEW_CHARS],
            "error": err,
            "usage": usage,
        }
    )
    return 1 if err else 0


async def _run(
    instruction: str, *, cwd: str, max_turns: int | None, world_state: bool, settle_timeout_s: float
) -> int:
    from . import paths, server_shared, subagent_types
    from .agents import registry as agents_reg
    from .api import sessions as sessions_api
    from .api import stream as stream_api
    from .scheduler import _last_assistant_text
    from .session_meta import _find_meta
    from .subagent_scheduler import meta as subagent_meta
    from .tools.ask_tools import reset_interactive as _reset_interactive_turn
    from .tools.ask_tools import set_interactive as _set_interactive_turn

    paths.ensure_layout()
    _write_exec_settings(world_state)
    # subagent 类型表 + agent 种子（create_session 惰性 ensure，显式调更稳）。
    subagent_types.ensure_seeded()
    agents_reg.ensure_seeded()

    # 把 --cwd 注册为 context folder 并设为 primary：内置工具的相对路径与
    # bash cwd 都解析到 primary（无 primary 时回退 session 文件目录——
    # benchmark 产物会全写进隔离 home 里导致 verifier 找不到文件）。注册按
    # resolved path 幂等，重复 exec 同一目录不膨胀 folder 库。
    from . import context_folders as cf

    cwd_abs = os.path.abspath(cwd)
    folder = cf.add_folder(cwd_abs, name="ginno exec workspace", auto_mount=False)

    model = os.environ.get("GINNO_EXEC_MODEL", "")
    req = sessions_api.CreateSessionRequest(
        project_slug="default",
        workspace="",
        title="ginno exec",
        provider="exec",
        model=model,
        context_folders=[folder["id"]],
        primary_folder=folder["id"],
    )
    meta = await sessions_api.create_session(req)
    if meta.get("ok") is False or not meta.get("id"):
        _emit(
            {
                "type": "result",
                "subtype": "error_deterministic",
                "is_error": True,
                "error": str(meta.get("error") or "session creation failed"),
            }
        )
        return 1
    session_id = meta["id"]
    session = server_shared._SESSIONS.get(session_id)
    if not session:
        _emit(
            {
                "type": "result",
                "subtype": "error_deterministic",
                "is_error": True,
                "error": "session not ready",
            }
        )
        return 1

    started = time.time()
    tap = _StreamTap(model)
    tap.session_id = session_id
    server_shared._SESSION_WS.setdefault(session_id, []).append(tap)
    _emit(
        {"type": "system", "subtype": "init", "session_id": session_id, "model": model, "cwd": cwd}
    )

    turn_id = uuid.uuid4().hex
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": "default",
            "agent_id": session.get("agent_id"),
            "turn_id": turn_id,
            "user_text": instruction,
            "usage_source": "exec",
        }
    }
    # 早置 running 标志 + 关 ask_user——与 _run_prompt_target 同协议：
    # headless 无人应答，ask_user 直接拒绝让模型自决。
    server_shared._RUNNING_TURNS[session_id] = turn_id
    ask_tok = _set_interactive_turn(False)
    slug = "default"
    try:
        await stream_api._run_stream(
            None, session["graph"], config, instruction, session, session.get("agent_id")
        )
        settle = await _await_quiescence(session_id, slug, settle_timeout_s)
        if settle.get("partial"):
            _emit(
                {
                    "type": "system",
                    "subtype": "settle_timeout",
                    **{k: settle[k] for k in ("live", "statuses") if k in settle},
                }
            )
    finally:
        _reset_interactive_turn(ask_tok)
        if server_shared._RUNNING_TURNS.get(session_id) == turn_id:
            server_shared._RUNNING_TURNS.pop(session_id, None)
        sockets = server_shared._SESSION_WS.get(session_id) or []
        server_shared._SESSION_WS[session_id] = [s for s in sockets if s is not tap]

    err = None
    found = _find_meta(session_id)
    if found:
        last_err = found[0].get("last_error")
        err = str(last_err.get("message") or "") if isinstance(last_err, dict) else None
    text = await _last_assistant_text(session_id, slug)
    debug = None
    if not text.strip():
        # 诊断:空 final text 时把最后一条 AI 消息的形态带出去(内容 repr、
        # additional_kwargs 键、reasoning 长度),供 harness 侧归因。
        try:
            from .checkpointer import FileCheckpointer

            tup = await FileCheckpointer(slug).aget_tuple(
                {"configurable": {"thread_id": session_id}}
            )
            msgs = (
                (tup.checkpoint.get("channel_values") or {}).get("messages")
                if tup and tup.checkpoint
                else []
            ) or []
            for m in reversed(msgs):
                if getattr(m, "type", None) == "ai":
                    kw = getattr(m, "additional_kwargs", {}) or {}
                    debug = {
                        "content_repr": repr(getattr(m, "content", ""))[:300],
                        "kwargs": sorted(kw.keys()),
                        "tool_calls": len(getattr(m, "tool_calls", None) or []),
                        "reasoning_len": len(
                            str(kw.get("reasoning_content") or kw.get("reasoning") or "")
                        ),
                    }
                    break
        except Exception:  # noqa: BLE001 — 诊断失败不致命
            pass
    rows = subagent_meta.collect_subagent_rows(slug, session_id, include_descendants=True)
    usage = _tree_usage_tokens({session_id, *(r["session_id"] for r in rows)}, started)
    _emit(
        {
            "type": "result",
            "subtype": "error_deterministic" if err else "success",
            "is_error": bool(err),
            "duration_ms": int((time.time() - started) * 1000),
            "duration_api_ms": int((time.time() - started) * 1000),
            "num_turns": 1 + len(rows),
            "session_id": session_id,
            "text": text,
            "preview": text[:_PREVIEW_CHARS],
            "error": err,
            "usage": usage,
            **({"last_ai_debug": debug} if debug else {}),
        }
    )
    return 1 if err else 0


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `ginno exec -- <instruction>`（console script / frozen 入口都带子命令名）。
    if argv and argv[0] == "exec":
        argv = argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        from ._version import __version__

        print(f"ginno {__version__}")
        raise SystemExit(0)
    instruction = ""
    if "--" in argv:
        idx = argv.index("--")
        instruction = " ".join(argv[idx + 1 :]).strip()
        argv = argv[:idx]
    parser = argparse.ArgumentParser(
        prog="ginno exec",
        description="Headless one-shot turn (benchmark harness entry). "
        "Prompt goes after `--`; endpoint/model via GINNO_EXEC_* env.",
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="workspace directory (default: $PWD)")
    parser.add_argument(
        "--home", default="", help="GINNO_HOME override (default: fresh temp dir per run)"
    )
    parser.add_argument("--max-turns", type=int, default=None, help="recursion limit for the turn")
    parser.add_argument(
        "--no-world-state", action="store_true", help="disable world-state injection (rules/memory)"
    )
    parser.add_argument(
        "--no-attach",
        action="store_true",
        help="never attach to a locally running Ginno app; force in-process isolated mode",
    )
    parser.add_argument(
        "--settle-timeout",
        type=float,
        default=1800.0,
        help="seconds to wait for subagents/wake-turns after the main turn (default: 1800)",
    )
    args = parser.parse_args(argv)

    if not instruction:
        parser.error("instruction required after `--`")

    os.chdir(args.cwd)
    # 远端模式优先：本地有 Ginno app 在跑就连它（用 app 现成的 provider/状态）。
    # 容器/CI 里探不到 sidecar，自然落到进程内隔离模式。显式 --no-attach 或
    # GINNO_EXEC_NO_ATTACH=1（bench harness 注入）可强制进程内。
    if not (args.no_attach or os.environ.get("GINNO_EXEC_NO_ATTACH") == "1"):
        port = int(os.environ.get("GINNO_RUNTIME_PORT") or 8787)
        if _probe_sidecar(port):
            raise SystemExit(
                asyncio.run(
                    _run_remote(
                        instruction, cwd=args.cwd, port=port, settle_timeout_s=args.settle_timeout
                    )
                )
            )
    # 隔离 home 必须在任何 ginno_runtime 模块级 paths 读取之前设置——而且要
    # 覆盖环境里残留的 GINNO_HOME（桌面启动上下文/测试进程都会带）,否则
    # "默认隔离"会被 ambient env 悄悄废掉。显式指定只认 --home / GINNO_EXEC_HOME。
    import tempfile

    # 限流退避链(SDK 6 次 + 代理 7 次 + 引擎 30-480s 指数退避)可能让一次
    # 调用数分钟无 chunk——180s 的流看门狗会误杀,exec 模式抬到 1200s。
    os.environ.setdefault("GINNO_CHUNK_TIMEOUT_S", "1200")
    os.environ["GINNO_HOME"] = (
        args.home
        or os.environ.get("GINNO_EXEC_HOME")
        # 临时目录而不是 <cwd>/.ginno-exec-home：benchmark 的 diff 捕获会把
        # home 里的 session/agent 状态全记成工作区改动。
        or tempfile.mkdtemp(prefix="ginno-exec-")
    )
    if args.max_turns is not None:
        os.environ["GINNO_RECURSION_LIMIT"] = str(args.max_turns)

    if os.environ.get("GINNO_FAKE_LLM") != "1":
        missing = [
            k
            for k in ("GINNO_EXEC_BASE_URL", "GINNO_EXEC_API_KEY", "GINNO_EXEC_MODEL")
            if not os.environ.get(k, "").strip()
        ]
        if missing:
            print(f"ginno exec: missing env: {', '.join(missing)}", file=sys.stderr)
            raise SystemExit(2)

    raise SystemExit(
        asyncio.run(
            _run(
                instruction,
                cwd=args.cwd,
                max_turns=args.max_turns,
                world_state=not args.no_world_state,
                settle_timeout_s=args.settle_timeout,
            )
        )
    )


if __name__ == "__main__":
    main()

"""DSH adapter — ``dsh --profile headless --json "<prompt>"``（NDJSON 事件流）。

DSH（DeepSeek Harness）的非交互入口：stdout 是 NDJSON 事件流，但会**夹杂
非 JSON 诊断行**（如 ``[llm-deepseek-account] model listing …``），stderr 是
``dsh: <code>: <message>``，退出码 0/1。事件 schema：

* ``{"type":"session","sessionId","cwd"}``
* ``{"type":"status","phase":"turn_start|step_start|step_end|turn_end",
  "turn":N,"step":N}``——step_end 可带 usage；turn_end 带 ``reason``，
  ``kind="completed"`` 才算成功，其余（aborted/blocked/error/…）都是失败。
* ``{"type":"text"|"thinking","text"}``——按消息提交点整段发出，非逐 token。
* ``{"type":"tool_call","callId","tool","input"}``
* ``{"type":"tool_result","callId","status","result"}``——**没有**工具名，
  用 callId 从 tool_call 事件反查。
* ``{"type":"final","text"}``——权威最终答复（失败时可能为空串）。
* ``{"type":"error","message"}``——runner 级失败（无 final）。

权限模式不进 argv：dsh-base 的 sandbox-policy 与 approval 都从环境变量
``DSH_PERMISSION_MODE`` 推导，所以本 adapter 额外实现 ``build_env``
（run_delegation 用 getattr 探测，其它后端不受影响）：

* read-only → ``DSH_PERMISSION_MODE=read-only``
* edit      → ``DSH_PERMISSION_MODE=workspace-write``

续跑：``supports_resume=True``，``build_argv(..., session_id="<id>")`` 会追加
``--session-id <id>``（在 prompt 之前）。实测语义：目标会话已落盘时
session 事件的 sessionId 不变、turn 从 2 继续（上下文保留）；未知 id 时
dsh 自己返回 rc=1 + ``{"type":"error", ...}``。
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import (
    DelegationResult,
    _dsh_usage,
    _iter_jsonl,
    _num0,
    _resolve_cli,
    _tail,
    _tool_result_text,
)


def _accumulate_usage(acc: dict, piece: dict) -> None:
    """step_end 的 usage 逐条累加（一次委托多步 = 多段账单）。"""
    for key, val in piece.items():
        acc[key] = _num0(acc.get(key)) + _num0(val)


def _reason_text(reason: dict) -> str:
    """turn_end.reason（非 completed）→ 可读失败原因（error 带 code 前缀）。"""
    if not isinstance(reason, dict):
        return str(reason or "")
    err = reason.get("error")
    if isinstance(err, dict):
        msg = str(err.get("message") or "").strip()
        code = str(err.get("code") or "").strip()
        if msg:
            return f"{code}: {msg}" if code else msg
    for key in ("message", "reason", "detail"):
        val = reason.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            m = val.get("message")
            if isinstance(m, str) and m.strip():
                return m.strip()
    kind = str(reason.get("kind") or "").strip()
    return f"turn ended with reason {kind}" if kind else "turn ended without completion"


class DshBackend:
    """DeepSeek Harness headless delegation (``dsh --profile headless --json``).

    权限模式经 ``DSH_PERMISSION_MODE`` 环境变量传递（见模块 docstring）——
    与 claude/codex/pi 的 argv 映射不同，这是 dsh 的唯一权限开关。
    """

    name = "dsh"
    cli_name = "dsh"
    # 装了但 headless 缺凭据 → 前端按 needsAuth 引导（与 pi 的 needsInit
    # 区分开：pi 要跑一次初始化，dsh 要配 DEEPSEEK_API_KEY）。
    setup_hint_code = "needsAuth"
    # 支持 ``--session-id`` 续跑：把新 prompt 接到之前某次委托的外部会话上。
    supports_resume = True

    def available(self) -> str | None:
        return _resolve_cli("dsh")

    def configured(self) -> bool:
        """headless 能否拿到模型凭据（三条来源任一命中即视为已配置）。

        * 启动环境里的 ``DEEPSEEK_API_KEY``（非空）；
        * ``$DSH_HOME/.env`` 里的 ``DEEPSEEK_API_KEY`` 行（非空值）；
        * ``$DSH_HOME/profiles/headless/cordis.patch.yml`` 里出现
          ``deepseek-account``（凭据走 profile patch 的账号路由形态）。

        ``DSH_HOME`` 未设时默认 ``~/.dsh``（尊重隔离 DSH_HOME 的 e2e/用户）。
        检测绝不抛异常：读不了文件 = 没配好，让设置页给引导。
        """
        try:
            if (os.environ.get("DEEPSEEK_API_KEY") or "").strip():
                return True
            dsh_home = Path(
                os.environ.get("DSH_HOME") or (Path.home() / ".dsh")
            )
            env_file = dsh_home / ".env"
            try:
                env_text = env_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                env_text = ""
            for line in env_text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, sep, val = line.partition("=")
                if sep and key.strip() == "DEEPSEEK_API_KEY" and val.strip().strip("'\""):
                    return True
            patch_file = (
                dsh_home / "profiles" / "headless" / "cordis.patch.yml"
            )
            try:
                patch_text = patch_file.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                patch_text = ""
            return "deepseek-account" in patch_text
        except Exception:  # noqa: BLE001 — 检测绝不 500
            return False

    def build_argv(
        self,
        prompt: str,
        mode: str,
        cwd: str,
        out_file: str | None,
        session_id: str | None = None,
    ) -> list[str]:
        # cwd 由 run_delegation 的 Popen(cwd=...) 绑定；out_file 不适用
        # （最终答复走事件流的 final 事件，不落文件）。
        # 续跑：``--session-id <sid>`` 把新 prompt 作为该会话的下一轮
        # （DSH 要求目标会话已落盘且 cwd/预设一致；失效 id 由 dsh 自己报错）。
        argv = [
            self.available() or "dsh",
            "--profile",
            "headless",
            "--json",
        ]
        if session_id:
            argv += ["--session-id", session_id]
        return argv + [prompt]

    def build_env(self, mode: str, cwd: str) -> dict[str, str]:
        """dsh 权限模式（可选钩子；见模块 docstring）。"""
        return {
            "DSH_PERMISSION_MODE": (
                "workspace-write" if mode == "edit" else "read-only"
            )
        }

    def parse_result(
        self, stdout: str, stderr: str, rc: int, out_file: str | None
    ) -> DelegationResult:
        transcript: list[dict] = []
        meta: dict = {"model": "dsh"}
        usage: dict = {}
        texts: list[str] = []
        final_text = ""
        call_names: dict[str, str] = {}
        failure = ""          # turn_end.reason 非 completed / runner error
        saw_turn_end = False
        completed = False
        # num_turns 口径 = **本次调用**里不同 turn_start 的 turn 数量（不是
        # max(turn)）：dsh 的 turn 是会话级递增，续跑流从 turn:2 开始，max
        # 会把「本次只跑了 1 轮」误报成 2。
        turns_seen: set[int] = set()
        for ev in _iter_jsonl(stdout):
            et = ev.get("type")
            if et == "session":
                meta["session_id"] = ev.get("sessionId") or ev.get("session_id")
            elif et == "status":
                phase = ev.get("phase")
                turn = ev.get("turn")
                if phase == "turn_start" and isinstance(turn, int):
                    turns_seen.add(turn)
                if phase == "step_end":
                    raw = ev.get("usage")
                    if isinstance(raw, dict) and raw:
                        _accumulate_usage(usage, _dsh_usage(raw))
                elif phase == "turn_end":
                    saw_turn_end = True
                    reason = ev.get("reason")
                    kind = (
                        reason.get("kind")
                        if isinstance(reason, dict)
                        else None
                    )
                    if kind == "completed":
                        completed = True
                    else:
                        failure = failure or _reason_text(reason or {})
                        if isinstance(reason, dict):
                            err = reason.get("error")
                            if isinstance(err, dict) and err.get("code"):
                                meta.setdefault(
                                    "error_code", str(err.get("code"))
                                )
                            if kind:
                                meta.setdefault("terminal_reason", str(kind))
            elif et == "text":
                text = ev.get("text")
                if isinstance(text, str) and text:
                    # text 事件按消息提交点整段发出：一条事件 = 一条 assistant
                    # 条目（空文本跳过；thinking 不进转录，IR 没有该角色）。
                    transcript.append(
                        {"role": "assistant", "text": text, "tool_calls": []}
                    )
                    texts.append(text)
            elif et == "tool_call":
                cid = ev.get("callId") or ev.get("call_id") or ""
                name = str(ev.get("tool") or "tool")
                if cid:
                    call_names[str(cid)] = name
                transcript.append(
                    {
                        "role": "assistant",
                        "text": "",
                        "tool_calls": [
                            {
                                "id": cid,
                                "name": name,
                                "args": ev.get("input") or {},
                            }
                        ],
                    }
                )
            elif et == "tool_result":
                cid = ev.get("callId") or ev.get("call_id") or ""
                transcript.append(
                    {
                        "role": "tool_result",
                        "id": cid,
                        # dsh 的 tool_result 没有工具名——用 callId 从
                        # tool_call 事件反查（配对不上时退化为 "tool"）。
                        "name": call_names.get(str(cid)) or "tool",
                        "content": _tool_result_text(ev.get("result")),
                        # status 只有 completed/error；非 completed（含缺省/
                        # 未知态）一律按错误渲染，fail-loud 不粉饰。
                        "is_error": ev.get("status") != "completed"
                        or bool(ev.get("isError")),
                    }
                )
            elif et == "final":
                text = ev.get("text")
                if isinstance(text, str):
                    final_text = text
            elif et == "error":
                msg = str(ev.get("message") or "runner error").strip()
                if msg:
                    failure = failure or msg
                    meta.setdefault("terminal_reason", "error")
        if turns_seen:
            meta["num_turns"] = len(turns_seen)
        # 输出通道：final 是权威答复；空 final（失败流常见）退最后一条非空
        # text。**只有成功**才把 stdout 尾部当降级答复——失败流的 stdout 是
        # 事件流/诊断行，不能当正文回给模型。
        answer = final_text if final_text.strip() else (texts[-1] if texts else "")
        # 终态判定：rc!=0 / turn_end 非 completed / 根本没等到 turn_end
        # （流被截断或 runner 级失败）→ error。没有显式 completed 绝不算成功
        # （与 claude 的 fail-closed 终态纪律一致）。
        is_error = bool(rc != 0 or failure or not completed)
        if is_error:
            if failure:
                diagnostic = failure
            elif rc != 0:
                diagnostic = f"rc={rc}" + (
                    f"; stderr: {stderr}" if (stderr or "").strip() else ""
                )
            elif not saw_turn_end:
                diagnostic = "stream ended without a turn_end event" + (
                    f"; stderr: {stderr}" if (stderr or "").strip() else ""
                )
            else:
                diagnostic = "delegation failed"
            return DelegationResult(
                output=answer,
                stop_reason="error",
                diagnostic=_tail(diagnostic),
                usage=usage,
                meta=meta,
                transcript=transcript,
            )
        output = answer
        degraded = ""
        if not output:
            tail = _tail(stdout or "", 4000)
            if tail.strip():
                output = (
                    "[note] no final message captured; showing stdout tail:\n"
                    + tail
                )
                degraded = "no final message captured; showing stdout tail"
        return DelegationResult(
            output=output,
            stop_reason="success",
            diagnostic=degraded,
            usage=usage,
            meta=meta,
            transcript=transcript,
        )

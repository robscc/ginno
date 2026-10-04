"""Claude Code adapter — ``claude -p --output-format stream-json --verbose``.

stream-json 事件流（system/assistant/user/result）→ IR；解析不出事件时
退回旧的单 JSON 路径（老 CLI / 异常输出）。
"""
from __future__ import annotations

import json

from .base import (
    DelegationResult,
    _claude_usage,
    _resolve_cli,
    _iter_jsonl,
    _parse_json_lenient,
    _tail,
    _tool_result_text,
)


class ClaudeCodeBackend:
    """Claude Code headless with a full event stream
    (``claude -p --output-format stream-json --verbose``).

    Mode mapping (both modes deny Bash via ``--settings`` permissions.deny —
    read-only additionally denies Edit/Write/NotebookEdit; edit adds
    ``--permission-mode=acceptEdits``):
    it only removes Bash/PowerShell/REPL/WebFetch while Edit/Write remain):

    * read-only → ``--disallowedTools Bash --permission-mode plan`` (plans, never edits)
    * edit      → ``--disallowedTools Bash --permission-mode acceptEdits`` (file edits
      auto-approved inside the workspace; Bash is gone from the tool table
      entirely, settings/git writes still need approval)
    """

    name = "claude-code"

    def available(self) -> str | None:
        return _resolve_cli("claude")

    def build_argv(
        self, prompt: str, mode: str, cwd: str, out_file: str | None
    ) -> list[str]:
        # 2026-10-04 真机矩阵（V1-V10）：--allowedTools / --strict-mcp-config /
        # --disallowedTools=Bash 都会炸掉 claude CLI 2.1.x headless 的延迟
        # 工具系统（会话里只剩 ToolSearch）；settings 形式的 permissions.deny
        # 是唯一既保留核心工具又能禁 Bash/编辑的路。--settings 用等号紧凑
        # JSON 单值形式（variadic 的 --disallowedTools 会把 prompt 吞掉）。
        deny = (
            ["Bash", "Edit", "Write", "NotebookEdit"]
            if mode == "read-only"
            else ["Bash"]
        )
        perm_settings = json.dumps(
            {"permissions": {"deny": deny}}, separators=(",", ":")
        )
        argv = [
            self.available() or "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            # headless 无人应答权限提示——默认跳过（用户决策 2026-10-05；
            # settings 里的 deny 清单保留作为兜底防线）。
            "--dangerously-skip-permissions",
            f"--settings={perm_settings}",
        ]
        if mode == "edit":
            argv.append("--permission-mode=acceptEdits")
        argv.append(prompt)
        return argv

    def parse_result(
        self, stdout: str, stderr: str, rc: int, out_file: str | None
    ) -> DelegationResult:
        # stream-json：逐行事件（system/assistant/user/tool_result/result）。
        # 解析不出任何事件时退回旧的单 JSON 路径（老 CLI / 异常输出）。
        transcript: list[dict] = []
        meta: dict = {}
        texts: list[str] = []
        final: dict | None = None
        for ev in _iter_jsonl(stdout):
            et = ev.get("type")
            if et == "system":
                if ev.get("subtype") == "init":
                    meta["session_id"] = ev.get("session_id")
                    meta["model"] = ev.get("model") or "claude-code"
            elif et == "assistant":
                msg = ev.get("message") or {}
                text = ""
                calls = []
                for blk in msg.get("content") or []:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "text":
                        text += blk.get("text") or ""
                    elif blk.get("type") == "tool_use":
                        calls.append(
                            {
                                "id": blk.get("id"),
                                "name": blk.get("name") or "tool",
                                "args": blk.get("input") or {},
                            }
                        )
                if text or calls:
                    transcript.append(
                        {"role": "assistant", "text": text, "tool_calls": calls}
                    )
                if text:
                    texts.append(text)
            elif et == "user":
                msg = ev.get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    for blk in content:
                        if (
                            isinstance(blk, dict)
                            and blk.get("type") == "tool_result"
                        ):
                            transcript.append(
                                {
                                    "role": "tool_result",
                                    "id": blk.get("tool_use_id"),
                                    "name": "",
                                    "content": _tool_result_text(
                                        blk.get("content")
                                    ),
                                    "is_error": bool(blk.get("is_error")),
                                }
                            )
            elif et == "result":
                final = ev
        if final is None and not transcript:
            return _parse_claude_legacy_json(stdout, stderr, rc)
        output = ""
        usage: dict = {}
        is_error = rc != 0
        if final is not None:
            output = str(final.get("result") or "")
            is_error = bool(final.get("is_error")) or rc != 0
            usage = _claude_usage(final.get("usage") or {})
            meta.update(
                {
                    "session_id": final.get("session_id") or meta.get("session_id"),
                    "num_turns": final.get("num_turns"),
                    "duration_ms": final.get("duration_ms"),
                    "cost_usd": final.get("cost_usd")
                    or final.get("total_cost_usd"),
                }
            )
        if not output and texts:
            # 流被截断（无 result 事件）——拼接 assistant 文本兜底。
            output = "\n".join(texts)
        if is_error:
            return DelegationResult(
                output=output,
                stop_reason="error",
                diagnostic=_tail(f"rc={rc}; stderr: {stderr}") or "is_error",
                usage=usage,
                meta=meta,
                transcript=transcript,
            )
        return DelegationResult(
            output=output,
            stop_reason="success",
            usage=usage,
            meta=meta,
            transcript=transcript,
        )


def _parse_claude_legacy_json(stdout: str, stderr: str, rc: int) -> DelegationResult:
    """旧 ``--output-format json`` 的单对象解析（stream-json 不可用时的兜底）。"""
    data = _parse_json_lenient(stdout or "")
    if data is None:
        return DelegationResult(
            stop_reason="error",
            diagnostic=_tail(f"rc={rc}; stderr: {stderr}\nstdout tail: {stdout}"),
        )
    output = str(data.get("result") or "")
    is_error = bool(data.get("is_error")) or rc != 0
    usage = _claude_usage(data.get("usage") or {})
    meta = {
        "session_id": data.get("session_id"),
        "num_turns": data.get("num_turns"),
        "duration_ms": data.get("duration_ms"),
        "cost_usd": data.get("cost_usd") or data.get("total_cost_usd"),
        "model": data.get("model") or "claude-code",
    }
    if is_error:
        return DelegationResult(
            output=output,
            stop_reason="error",
            diagnostic=_tail(f"rc={rc}; stderr: {stderr}") or "is_error",
            usage=usage,
            meta=meta,
        )
    return DelegationResult(
        output=output, stop_reason="success", usage=usage, meta=meta
    )

"""Claude Code adapter — ``claude -p --output-format stream-json --verbose``.

stream-json 事件流（system/assistant/user/result）→ IR；解析不出事件时
退回旧的单 JSON 路径（老 CLI / 异常输出）。
"""
from __future__ import annotations

import re

from .base import (
    DelegationResult,
    _claude_usage,
    _resolve_cli,
    _iter_jsonl,
    _parse_json_lenient,
    _tail,
    _tool_result_text,
)

# 流戛然而止（无 result 事件）时的上下文溢出指纹：命中 ⇒ 判 error 而非兜底
# 成功——父模型拿半截回答还以为任务完成是委托最危险的假成功形态（借鉴
# Multica context_exhausted：结构化 terminal_reason 优先，措辞指纹兜底）。
# 只在 result 事件缺席时应用——正常收尾流里模型讨论到这些词不算。
_CONTEXT_EXHAUSTED_RE = re.compile(
    r"prompt is too long"
    r"|prompt: \d+(?:\.\d+)?[km]? ?tokens? > \d+"
    r"|(?:context|conversation).{0,40}(?:limit|window).{0,30}(?:exceed|full|overflow|reached)"
    r"|exceeds?.{0,30}(?:maximum )?context"
    r"|too many (?:input )?tokens"
    r"|request.?too.?large"
    r"|(?:input|context).{0,10}length.{0,20}exceed"
    r"|(?:context|conversation).{0,30}exhaust",
    re.IGNORECASE,
)


class ClaudeCodeBackend:
    """Claude Code headless with a full event stream
    (``claude -p --output-format stream-json --verbose``).

    Mode mapping (2026-10-05 用户决策：真实委托需要完整工具表，不禁任何
    工具；两种模式都跑 ``--dangerously-skip-permissions``，如需只读语义
    只能靠 prompt 层约束):

    * read-only → 与 edit 同一张完整工具表（名义保留，供调用方语义区分）
    * edit      → 追加 ``--permission-mode=acceptEdits``（skip-permissions
      之下主要是显式声明编辑意图）
    """

    name = "claude-code"
    cli_name = "claude"

    def available(self) -> str | None:
        return _resolve_cli("claude")

    def build_argv(
        self, prompt: str, mode: str, cwd: str, out_file: str | None
    ) -> list[str]:
        # 2026-10-04 真机矩阵（V1-V10）：--allowedTools / --strict-mcp-config /
        # --disallowedTools 都会炸掉 claude CLI 2.1.x headless 的延迟工具系统
        # （会话里只剩 ToolSearch）——CLI 层禁工具的路全堵死了。
        # 2026-10-05 用户决策：委托面向真实工作环境，Bash/Edit 等被禁工具
        # 都是要用的——彻底移除 permissions.deny / --settings，保留完整
        # 工具表，配合 --dangerously-skip-permissions 直跑。
        argv = [
            self.available() or "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            # headless 无人应答权限提示——默认跳过（用户决策 2026-10-05）。
            "--dangerously-skip-permissions",
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
        if final is None and transcript:
            # fail-closed 终态分类（Multica stream_json_result 同思想）：没有
            # 显式 result 事件就不算完整成功。先查上下文溢出指纹——必须报
            # error + terminal_reason，供将来的 resume 层决策；其余情况保留
            # assistant 文本兜底，但带 [note] 显式标注这是截断抢救件。
            probe = "\n".join(texts[-2:]) + "\n" + (stderr or "")
            hit = _CONTEXT_EXHAUSTED_RE.search(probe)
            if hit:
                return DelegationResult(
                    output="\n".join(texts),
                    stop_reason="error",
                    diagnostic=_tail(
                        "context exhausted (stream ended without result "
                        f"event; matched {hit.group(0)!r}); stderr: {stderr}"
                    ),
                    meta={**meta, "terminal_reason": "context_exhausted"},
                    transcript=transcript,
                )
            salvage_note = (
                "stream ended without a result event — output salvaged from "
                "the last assistant text; the run may be incomplete"
            )
            output = "\n".join(texts) if texts else ""
            if not output:
                # 截断且连文本都没有（全是 tool_use 中途断掉）：无可抢救，
                # 按失败报，别返回空成功。
                return DelegationResult(
                    stop_reason="error",
                    diagnostic=_tail(
                        "stream ended without result event and without any "
                        f"assistant text; stderr: {stderr}"
                    )
                    or "truncated without output",
                    meta={**meta, "terminal_reason": "stream_truncated"},
                    transcript=transcript,
                )
            return DelegationResult(
                output=output,
                stop_reason="success",
                diagnostic=salvage_note,
                meta={**meta, "terminal_reason": "stream_truncated"},
                transcript=transcript,
            )
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

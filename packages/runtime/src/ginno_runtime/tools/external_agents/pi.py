"""pi adapter — ``pi --mode json``（JSONL v3 事件流）。

pi 无内置权限系统，工具表是唯一静态边界：mode → ``--tools`` 裁剪，
两种模式都无 bash（no-shell 原则）。provider/model 尊重 pi 自己的配置。
"""
from __future__ import annotations

from .base import (
    DelegationResult,
    _iter_jsonl,
    _pi_usage,
    _resolve_cli,
    _tail,
    _tool_result_text,
)


class PiBackend:
    """pi coding agent in JSON mode (``pi --mode json``).

    pi 没有内置权限系统——工具表是唯一静态边界，mode 映射到 ``--tools``：
    * read-only → ``--tools read,grep,find,ls``（无 bash/edit/write）
    * edit      → ``--tools read,edit,write,grep,find,ls``——依旧无 bash，
      与另外两个后端的 no-shell 原则一致。

    ``--mode json``：stdout 输出 JSONL 协议事件（v3 schema：session 头 /
    message_update / message_end / tool_execution_* / agent_end）；
    ``--no-session`` 一次性运行不落 pi 自己的会话库；``--no-extensions``
    钉死确定性（用户级坏扩展不能 destabilize 子进程）；provider/model
    尊重 pi 自己的配置——外部代理自带模型账号。
    """

    name = "pi"

    def available(self) -> str | None:
        return _resolve_cli("pi")

    def build_argv(
        self, prompt: str, mode: str, cwd: str, out_file: str | None
    ) -> list[str]:
        # cwd 由 run_delegation 的 Popen(cwd=...) 绑定（pi 无 -C 标志）。
        tools = (
            "read,grep,find,ls"
            if mode == "read-only"
            else "read,edit,write,grep,find,ls"
        )
        return [
            self.available() or "pi",
            "--mode",
            "json",
            "--no-session",
            "--no-extensions",
            "--tools",
            tools,
            prompt,
        ]

    def parse_result(
        self, stdout: str, stderr: str, rc: int, out_file: str | None
    ) -> DelegationResult:
        transcript: list[dict] = []
        meta: dict = {"model": "pi"}
        usage: dict = {}
        texts: list[str] = []
        final_error = ""
        for ev in _iter_jsonl(stdout):
            et = ev.get("type")
            if et == "session":
                meta["session_id"] = ev.get("id")
            elif et == "message_end":
                msg = ev.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                text = ""
                calls = []
                for blk in msg.get("content") or []:
                    if not isinstance(blk, dict):
                        continue
                    btype = blk.get("type")
                    if btype == "text":
                        text += blk.get("text") or ""
                    elif btype in ("toolCall", "tool_call"):
                        calls.append(
                            {
                                "id": blk.get("id") or blk.get("toolCallId"),
                                "name": blk.get("name") or "tool",
                                "args": blk.get("arguments")
                                or blk.get("args")
                                or {},
                            }
                        )
                if text or calls:
                    transcript.append(
                        {"role": "assistant", "text": text, "tool_calls": calls}
                    )
                if text:
                    texts.append(text)
            elif et == "message_update":
                raw = ev.get("usage") or {}
                if raw:
                    # 累计值：最新一条覆盖旧值。
                    usage = _pi_usage(raw)
            elif et == "tool_execution_end":
                transcript.append(
                    {
                        "role": "tool_result",
                        "id": ev.get("toolCallId") or ev.get("tool_call_id"),
                        "name": ev.get("toolName") or ev.get("tool_name") or "",
                        "content": _tool_result_text(ev.get("result")),
                        "is_error": bool(
                            ev.get("isError") or ev.get("is_error")
                        ),
                    }
                )
            elif et == "auto_retry_end":
                if ev.get("success") is False:
                    final_error = str(
                        ev.get("finalError") or "provider retries exhausted"
                    )
            elif et == "agent_end":
                if ev.get("willRetry"):
                    final_error = final_error or "agent ended with willRetry"
        # 权威最终消息 = 最后一条 assistant message_end 的文本。
        output = texts[-1] if texts else ""
        if final_error or rc != 0 or (not texts and not transcript):
            return DelegationResult(
                output=output,
                stop_reason="error",
                diagnostic=final_error
                or (_tail(f"rc={rc}; stderr: {stderr}") if (rc != 0 or stderr) else "")
                or "no assistant output",
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

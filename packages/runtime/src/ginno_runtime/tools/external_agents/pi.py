"""pi adapter — ``pi --mode json``（JSONL v3 事件流）。

pi 无内置权限系统，工具表是唯一静态边界：read-only 走 ``--tools`` 只读
白名单；edit 不传 ``--tools``，用 pi 默认全部内置工具(含 bash，2026-10-05
用户决策：真实委托需要 shell)。provider/model 尊重 pi 自己的配置。
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


def _pi_tool_result(res) -> str:
    """pi 的 tool_execution_end.result 是 LLM content-block 形状:
    {'content': [{'type':'text','text':...}], 'structuredContent':
    {'output':..., 'exit_code':..., 'wall_time_seconds':...}}。通用
    _tool_result_text 不认这个形状会整只 repr 出来(2026-10-05 用户反馈:
    回放里 toolcall 显示原始 dict)。优先 structuredContent.output 并附
    exit_code/耗时摘要行;退化 content[].text;再退化通用提取。"""
    if isinstance(res, dict):
        sc = res.get("structuredContent")
        if isinstance(sc, dict):
            out = sc.get("output")
            if isinstance(out, str) and out.strip():
                extras = []
                if sc.get("exit_code") is not None:
                    extras.append(f"exit_code={sc.get('exit_code')}")
                wt = sc.get("wall_time_seconds")
                if isinstance(wt, (int, float)):
                    extras.append(f"{wt:.1f}s")
                head = f"[{' '.join(extras)}]" if extras else ""
                return f"{head}\n{out}".strip() if head else out
        content = res.get("content")
        if isinstance(content, list):
            parts = [
                b.get("text")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ]
            if parts:
                return "\n".join(parts)
    return _tool_result_text(res)


class PiBackend:
    """pi coding agent in JSON mode (``pi --mode json``).

    pi 没有内置权限系统——工具表是唯一静态边界，mode 映射到 ``--tools``：
    * read-only → ``--tools read,grep,find,ls``（无 bash/edit/write）
    * edit      → 不传 ``--tools``，pi 默认全部内置工具(含 bash)——与
      claude-code 移除 deny 同批的 2026-10-05 用户决策：真实委托需要 shell。

    ``--mode json``：stdout 输出 JSONL 协议事件（v3 schema：session 头 /
    message_update / message_end / tool_execution_* / agent_end）；
    ``--no-session`` 一次性运行不落 pi 自己的会话库；``--no-extensions``
    钉死确定性（用户级坏扩展不能 destabilize 子进程）；provider/model
    尊重 pi 自己的配置——外部代理自带模型账号。
    """

    name = "pi"

    def available(self) -> str | None:
        return _resolve_cli("pi")

    def configured(self) -> bool:
        """pi 装好后还需首次运行完成 provider/model 配置(~/.pi 生成);
        未初始化时设置页给「先在终端跑一次 pi」的提示。"""
        from pathlib import Path

        return Path.home().joinpath(".pi").exists()

    def build_argv(
        self, prompt: str, mode: str, cwd: str, out_file: str | None
    ) -> list[str]:
        # cwd 由 run_delegation 的 Popen(cwd=...) 绑定（pi 无 -C 标志）。
        argv = [
            self.available() or "pi",
            "--mode",
            "json",
            "--no-session",
            "--no-extensions",
        ]
        # 工具表:read-only 维持只读白名单(无 bash/edit/write);edit 不传
        # --tools,走 pi 默认全部内置工具(read/bash/edit/write/...)。pi 无
        # 权限系统,工具表是唯一静态边界(2026-10-05 用户决策,同批移除了
        # claude-code 的 permissions.deny)。
        if mode == "read-only":
            argv += ["--tools", "read,grep,find,ls"]
        return argv + [prompt]

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
                        "content": _pi_tool_result(ev.get("result")),
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

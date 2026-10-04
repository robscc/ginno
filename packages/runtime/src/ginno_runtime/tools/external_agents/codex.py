"""Codex adapter — ``codex exec --json``（实验事件流 + last-message 双通道）。

--json 的 JSONL 宽松解析进转录（字段跨版本漂移，解析失败自动降级）；
最终答复仍以 ``--output-last-message`` 文件为主通道。
"""
from __future__ import annotations

from .base import (
    DelegationResult,
    _iter_jsonl,
    _num0,
    _resolve_cli,
    _tail,
)


class CodexBackend:
    """Codex CLI non-interactive mode (``codex exec``).

    The final agent message is captured via ``--output-last-message`` (a temp
    file) because stdout is human-oriented progress text.

    * read-only → ``-s read-only`` (blocks shell AND apply_patch)
    * edit      → ``--full-auto`` (workspace-write sandbox + auto-approval)
    """

    name = "codex"

    def available(self) -> str | None:
        return _resolve_cli("codex")

    def build_argv(
        self, prompt: str, mode: str, cwd: str, out_file: str | None
    ) -> list[str]:
        argv = [
            self.available() or "codex",
            "exec",
            "-C",
            cwd,
            "--skip-git-repo-check",
        ]
        if out_file:
            argv += ["--output-last-message", out_file]
        # --json：实验性 JSONL 事件流（agent_message / command_execution /
        # file_change / turn.completed），宽松解析进转录；最终答复仍以
        # last-message 文件为主通道。
        argv += ["--json"]
        if mode == "read-only":
            argv += ["-s", "read-only"]
        else:
            argv += ["--full-auto"]
        argv.append(prompt)
        return argv

    def parse_result(
        self, stdout: str, stderr: str, rc: int, out_file: str | None
    ) -> DelegationResult:
        transcript: list[dict] = []
        usage: dict = {}
        event_texts: list[str] = []
        meta: dict = {"model": "codex"}
        turn_failed = ""
        for ev in _iter_jsonl(stdout):
            et = ev.get("type")
            if et == "thread.started":
                meta["session_id"] = ev.get("thread_id")
            elif et == "item.completed":
                item = ev.get("item") or {}
                it = item.get("type")
                if it == "agent_message":
                    text = item.get("text") or ""
                    if text:
                        transcript.append(
                            {"role": "assistant", "text": text, "tool_calls": []}
                        )
                        event_texts.append(text)
                elif it == "command_execution":
                    transcript.append(
                        {
                            "role": "tool_result",
                            "id": "",
                            "name": "bash",
                            "content": (item.get("command") or "")
                            + "\n"
                            + (item.get("aggregated_output") or ""),
                            "is_error": item.get("status") == "failed",
                        }
                    )
                elif it == "file_change":
                    changes = item.get("changes") or []
                    names = ", ".join(
                        c.get("path", "?")
                        for c in changes
                        if isinstance(c, dict)
                    )
                    transcript.append(
                        {
                            "role": "tool_result",
                            "id": "",
                            "name": "apply_patch",
                            "content": names or "(file changes)",
                            "is_error": False,
                        }
                    )
            elif et == "turn.completed":
                raw = ev.get("usage") or {}
                if raw:
                    usage = {
                        "input_tokens": _num0(raw.get("input_tokens"))
                        + _num0(raw.get("cached_input_tokens")),
                        "output_tokens": _num0(raw.get("output_tokens")),
                        "cache_read_tokens": _num0(
                            raw.get("cached_input_tokens")
                        ),
                        "cache_creation_tokens": 0,
                    }
            elif et == "turn.failed":
                err = ev.get("error") or {}
                turn_failed = str(err.get("message") or "turn failed")
        output = ""
        if out_file:
            try:
                with open(out_file, encoding="utf-8", errors="replace") as f:
                    output = f.read().strip()
            except OSError:
                output = ""
        if not output and event_texts:
            # --json 有事件但没拿到 last-message 文件：最后一条
            # agent_message 就是最终答复。
            output = event_texts[-1]
        if rc != 0 or turn_failed:
            return DelegationResult(
                output=output,
                stop_reason="error",
                diagnostic=turn_failed
                or _tail(f"rc={rc}; stderr: {stderr}\nstdout: {stdout}"),
                usage=usage,
                meta=meta,
                transcript=transcript,
            )
        if not output:
            # Degraded: no last-message file and no events — fall back to
            # the stdout tail. The [note] travels in the OUTPUT (not just
            # the diagnostic): the model must know this is a progress-stream
            # tail, not a final answer.
            output = (
                "[note] no last-message file captured; showing stdout tail:\n"
                + _tail(stdout or "", 4000)
            )
            return DelegationResult(
                output=output,
                stop_reason="success",
                diagnostic="no last-message file; showing stdout tail",
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

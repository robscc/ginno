"""Dev domain — /dev-swap and /think.

/dev-swap runs the runtime hot-swap build in the background (admin-only
from channels, fail-closed); /think reads/switches the session thinking
effort. The background task set (_DEV_SWAP_TASKS) is also consumed by
api/devswap.py so the cancel endpoint can see command-launched runs.
"""

from __future__ import annotations

import asyncio
import re

from ..lang import t
from ._base import BuiltinCommand


def _think_usage() -> str:
    return t(
        "**Thinking effort usage**\n"
        "- `/think <off|low|medium|high>` — set this session's thinking effort\n"
        "- `/think auto` — follow the provider config (default)\n"
        "- `/think` — show the current effort",
        "**/think 用法**\n"
        "- `/think <off|low|medium|high>` — 设置本会话思考强度\n"
        "- `/think auto` — 跟随 provider 配置（缺省）\n"
        "- `/think` — 查看当前强度",
    )

def _think_handler(project_slug: str | None = None, session=None, args=None) -> str:
    """/think — 查询走同步路径;换档走 async(busy-gated,不与运行中 turn 抢)。"""
    if not session:
        return _think_usage()
    arg = (args or "").strip().lower()
    if arg not in ("", "auto", "off", "low", "medium", "high"):
        return _think_usage()
    if arg:
        return ""  # 动作交给 async_handler
    cur = str(session.get("thinking_effort") or "auto")
    return (
        t("Current thinking effort", "当前思考强度")
        + f": **{cur}**\n\n"
        + _think_usage()
    )

async def _think_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    if not session:
        return _think_usage()
    arg = (args or "").strip().lower()
    if arg not in ("auto", "off", "low", "medium", "high"):
        return _think_usage()
    sid = session.get("session_id") or session.get("id") or ""
    # 懒导入防环:api.sessions 经 ws/stream 链路最终引用本模块。
    from ..api.sessions import PatchSessionRequest, patch_session

    r = await patch_session(sid, PatchSessionRequest(thinking_effort=arg))
    if not r.get("ok"):
        return t(
            f"Thinking effort change failed: {r.get('error')}",
            f"思考强度切换失败：{r.get('error')}",
        )
    return (
        t("Thinking effort set to", "思考强度已设为")
        + f" **{arg}** — "
        + t("effective from the next model call.", "下一次模型调用起生效。")
    )

_DEV_SWAP_TASKS: set[asyncio.Task] = set()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

async def _dev_swap_background(sid: str, repo: str) -> None:
    """后台跑 make dev-swap（构建 3-6 分钟）；完成即广播 finish 信号。

    成功 → ``runtime.dev_swap_pending``（前端 invoke 壳 finish_dev_swap：
    重启+指纹验证+失败回滚，之后本进程被重启换掉——这就是闭环）。
    失败 → notice 带构建尾部输出（当前运行不受任何影响，未切换）。

    进度可视化（docs/dev-swap-progress-design.md §2.3）：逐行读 stdout，
    解析 dev_swap.sh 的 ``[devswap-progress] {json}`` 行直调
    ``api.devswap.update_progress`` 回填同一进度通道（与 shell 的 curl
    上报以签名去重）；notice 尾 600 字符逻辑不变。
    """
    import os as _os

    from ..server_shared import _push_global_event, _push_session_event

    # G1 无头授权（channel-devswap-unattended-design.md §1.4）：channel 会话
    # 发起时带 confirm 标记 + chat_key——构建完成后经企微 y/n 确认 finish。
    _unattended_env: dict[str, str] = {}
    if sid:
        try:
            from ..session_meta import _find_meta

            _found = _find_meta(sid)
            _meta = _found[0] if _found else None
            if _meta and _meta.get("type") == "channel":
                _ck = ""
                try:
                    from ..connectors.builtin import _wecom_adapter

                    if _wecom_adapter is not None and _wecom_adapter._mapper:
                        _chats = _wecom_adapter._mapper.chats_for(sid)
                        _ck = str((_chats[0] or {}).get("chat_id") or "") if _chats else ""
                except Exception:  # noqa: BLE001
                    _ck = ""
                if _ck:
                    _unattended_env = {
                        "DEV_SWAP_UNATTENDED": "confirm",
                        "DEV_SWAP_CHAT_KEY": _ck,
                    }
        except Exception:  # noqa: BLE001 — 判定失败按桌面语义跑
            _unattended_env = {}

    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "scripts/dev_swap.sh",
            cwd=repo,
            env={
                **_os.environ,
                **_unattended_env,
                "DEV_SWAP_REQUESTED_BY": "agent",
                # 发起会话随 env 传给 shell → state.json → 重启后新进程的
                # finish 终态上报（api/devswap.watch_swap_finish）——前端
                # §4.5 会话门控与结果落块都靠它。
                "DEV_SWAP_SESSION_ID": sid,
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        await _push_session_event(
            sid, "notice", {"message": "/dev-swap: repo path invalid: " + repo}
        )
        return

    from ..api.devswap import ProgressReport, update_progress

    tail_buf: list[bytes] = []
    tail_len = 0
    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            tail_buf.append(line)
            tail_len += len(line)
            while tail_len > 4096 and len(tail_buf) > 1:  # 只留尾 4KB 供 notice
                tail_len -= len(tail_buf.pop(0))
            text = line.decode(errors="replace").strip()
            if not text.startswith("[devswap-progress]"):
                continue
            payload = text[len("[devswap-progress]"):].strip()
            try:
                import json as _json

                data = _json.loads(payload)
                if not data.get("run_id"):
                    continue
                update_progress(
                    ProgressReport(
                        run_id=str(data["run_id"]),
                        stage=str(data["stage"]),
                        state=str(data["state"]),
                        detail=data.get("detail"),
                        pct=data.get("pct"),
                        log_tail=data.get("log_tail"),
                        sid=sid or None,
                    )
                )
            except Exception:
                continue  # 进度行解析失败绝不影响构建结果
    except asyncio.CancelledError:
        from ..server_shared import _DEVSWAP_PROGRESS as _DEVSWAP_SNAPSHOT

        # 用户取消（2026-11-05：左上热更新按钮二击 → /api/devswap/cancel）。
        # kill 构建子进程树（start_new_session 语义，kill 整组），进度标
        # cancelled；未切 symlink 前取消零风险。
        try:
            _os.killpg(_os.getpgid(proc.pid), _os.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        update_progress(
            ProgressReport(
                run_id=str(
                    (_DEVSWAP_SNAPSHOT().get("snapshot") or {}).get("run_id") or "cancelled"
                ),
                stage="build_runtime",
                state="fail",
                detail="cancelled by user",
                sid=sid or None,
            )
        )
        raise
    await proc.wait()
    tail = _ANSI_RE.sub("", b"".join(tail_buf)[-600:].decode(errors="replace"))
    if proc.returncode == 0:
        # finish 阶段由 shell 已报 running；壳收尾终态由重启后的新进程接管。
        # 空 sid（侧栏热更新按钮等无会话发起方，2026-11-05）→ 全局广播，
        # 保证任意会话的前端都能收到确认门事件。
        if sid:
            await _push_session_event(
                sid,
                "runtime.dev_swap_pending",
                {"message": "dev-swap build ready; finishing swap (restart + verify)"},
            )
        else:
            await _push_global_event(
                "runtime.dev_swap_pending",
                {"message": "dev-swap build ready; finishing swap (restart + verify)"},
            )
    else:
        if sid:
            await _push_session_event(
                sid, "notice",
                {"message": f"/dev-swap failed (exit {proc.returncode}), nothing swapped:\n{tail}"},
            )
        else:
            await _push_global_event(
                "notice",
                {"message": f"dev-swap failed (exit {proc.returncode}), nothing swapped:\n{tail}"},
            )

async def _dev_swap_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/dev-swap — 开发热切换（docs/runtime-hot-swap-design.md）。

    产线闭环：本命令跑 build 段（quiesce 检查 → 单测 → make runtime →
    blue-green 预检 → symlink 原子切），随后广播 finish 信号由壳完成
    「重启 + 指纹验证 + 失败回滚」。**发起本命令的 turn 会在重启中被
    换掉**（发起者即旧进程）——回复只到「构建启动」，重启后由 TopBar
    dev 指纹黄条/回滚 notice 给出结局。

    repo 发现顺序：/dev-swap <repo路径> 显式参数 > build-info.repo
    （打包时写入的源码树路径）。
    """
    sid = (session or {}).get("session_id") or ""
    repo = (args or "").strip()
    if repo.startswith(("http://", "https://", "git@", "ssh://", "git://")):
        # 传 git URL 是最高频误用——单独给提示，别落到泛泛的 usage 文案。
        return t(
            "(/dev-swap needs a *local* ginno checkout — a git URL won't work. "
            "Clone it first (`git clone <url>`), then `/dev-swap /path/to/ginno`.)",
            "（/dev-swap 需要**本地**源码树——git URL 不行。先 `git clone <url>`，"
            "再 `/dev-swap /本地路径/ginno`。）",
        )
    if not repo:
        from ..world_state import context_settings

        repo = str(context_settings().get("dev_swap_repo") or "")
    if not repo:
        from .._build import build_info

        repo = str(build_info().get("repo") or "")
    from pathlib import Path as _P

    if not repo or not (_P(repo) / "scripts" / "dev_swap.sh").exists():
        return t(
            "(/dev-swap needs the ginno repo path — usage: /dev-swap /path/to/ginno)",
            "（/dev-swap 需要 ginno 源码树路径——用法：/dev-swap /path/to/ginno）",
        )
    task = asyncio.create_task(_dev_swap_background(sid, repo))
    # 命令路径也注册到进度快照的 task 位（2026-11-11 补齐：此前只有
    # /api/devswap/start 记 task，/dev-swap 命令发起的 run 对 cancel
    # 端点不可见——「取消热更新」对命令路径失效）。
    from ..server_shared import _DEVSWAP_PROGRESS as _DS_PROGRESS

    _DS_PROGRESS["task"] = task
    _DEV_SWAP_TASKS.add(task)
    task.add_done_callback(_DEV_SWAP_TASKS.discard)
    return t(
        "Dev-swap build started (~3-6 min: tests → PyInstaller → preflight → "
        "symlink swap). I'll restart onto the new bundle when it's ready.",
        "热切换构建已启动（约 3-6 分钟：单测 → PyInstaller → 预检 → symlink 切换）。"
        "构建完成后我会自动重启到新 bundle。",
    )


# 本域注册块：registry.py 装配 BUILTINS 时收集（命令清单契约快照钉住）。
COMMANDS: dict[str, BuiltinCommand] = {
    "dev-swap": BuiltinCommand(
        name="dev-swap",
        description=(
            "Developer hot-swap: rebuild the runtime bundle and restart onto it "
            "(tests → build → preflight → swap → verify, auto-rollback on failure)",
            "开发热切换：重建 runtime bundle 并热重启到新版本"
            "（单测 → 构建 → 预检 → 切换 → 验证，失败自动回滚）",
        ),
        handler=lambda *a, **k: t(
            "(/dev-swap runs asynchronously — just send it plain)",
            "（/dev-swap 异步执行——直接发送即可）",
        ),
        async_handler=_dev_swap_async_handler,
        # Rebuilds and restarts the runtime — channel-side requires an
        # admin userid (fail-closed: empty admin_users rejects everyone).
        channel_policy="admin",
    ),
    "think": BuiltinCommand(
        name="think",
        description=(
            "Set or show this session's thinking effort (/think <off|low|medium|high|auto>)",
            "设置或查看本会话思考强度（/think <off|low|medium|high|auto>）",
        ),
        handler=_think_handler,
        async_handler=_think_async_handler,
    ),
}

"""Workflow domain — the /workflow-{action} command family.

Thin wrappers over the api/workflows.py endpoints (docs/at-session-and-
mention-scope-design.md §六): name/argument parsing + receipt copy only —
zero business logic at the slash layer.
"""

from __future__ import annotations

from ..lang import t
from ._base import BuiltinCommand


_WORKFLOW_FAMILY_USAGE_EN = (
    "**Workflow command family**\n"
    "- `/workflow-run <name|id>` — start a run (fuzzy name match; lists candidates "
    "when ambiguous)\n"
    "- `/workflow-status [run_id]` — status card of the latest run (or a given one)\n"
    "- `/workflow-pause [run_id]` — pause the latest (or given) running run\n"
    "- `/workflow-resume [run_id]` — resume a paused run\n"
    "- `/workflow-cancel [run_id]` — cancel the latest (or given) run\n"
    "- `/workflow-list` — list workflow definitions (name · version · steps · last run)\n"
    "- `/workflow-delete <name|id>` — delete a definition (run history is kept)"
)

_WORKFLOW_FAMILY_USAGE_ZH = (
    "**/workflow 命令族**\n"
    "- `/workflow-run <名称|id>` — 运行工作流（名称模糊匹配；多命中列出候选）\n"
    "- `/workflow-status [run_id]` — 最近一次 run（或指定 run）的状态卡\n"
    "- `/workflow-pause [run_id]` — 暂停最近（或指定）正在运行的 run\n"
    "- `/workflow-resume [run_id]` — 恢复已暂停的 run\n"
    "- `/workflow-cancel [run_id]` — 取消最近（或指定）run\n"
    "- `/workflow-list` — 工作流定义简表（名称 · 版本 · 步骤数 · 最近 run）\n"
    "- `/workflow-delete <名称|id>` — 删除定义（历史 run 保留）"
)

def _wf_family_usage() -> str:
    return t(_WORKFLOW_FAMILY_USAGE_EN, _WORKFLOW_FAMILY_USAGE_ZH)

def _wf_async_only_hint(cmd: str):
    def _hint(project_slug: str | None = None, session=None, args=None) -> str:
        return t(
            f"(/{cmd} runs asynchronously — just send it plain)",
            f"（/{cmd} 异步执行——直接发送即可）",
        )

    return _hint

async def _wf_call(coro):
    """Await an api/workflows endpoint coroutine; HTTPException → error str.

    Returns ``(ok, data-or-error)`` so handlers stay flat — the slash layer
    never lets a FastAPI exception escape into a turn reply."""
    from fastapi import HTTPException

    try:
        return True, await coro
    except HTTPException as e:
        return False, str(e.detail)

_WF_RUN_STATUS_LABELS = {
    "running": ("running", "运行中"),
    "paused": ("paused", "已暂停"),
    "done": ("done", "已完成"),
    "failed": ("failed", "已失败"),
    "cancelled": ("cancelled", "已取消"),
    "interrupted": ("interrupted", "已中断"),
}

def _wf_status_label(status: object) -> str:
    pair = _WF_RUN_STATUS_LABELS.get(str(status or ""))
    return t(*pair) if pair else str(status or "?")

def _wf_run_line(run: dict) -> str:
    name = run.get("name") or run.get("workflow_id") or "?"
    return f"- `{run.get('id')}` — {name} · {_wf_status_label(run.get('status'))}"

def _wf_match_defs(token: str) -> list[dict]:
    """name|id 匹配梯子：精确 id > 精确 name > name 子串（大小写不敏感）。"""
    from .. import workflows as wf_store

    token = (token or "").strip()
    defs = wf_store.list_defs()
    by_id = [d for d in defs if d.get("id") == token]
    if by_id:
        return by_id
    low = token.lower()
    exact = [d for d in defs if (d.get("name") or "").lower() == low]
    if exact:
        return exact
    return [d for d in defs if low in (d.get("name") or "").lower()]

def _wf_defs_table(defs: list[dict]) -> str:
    lines = []
    for d in defs:
        n = len(d.get("steps") or [])
        lines.append(
            f"- **{d.get('name')}** `{d.get('id')}` · "
            + t(f"v{d.get('version')} · {n} steps", f"v{d.get('version')} · {n} 步")
        )
    return "\n".join(lines)

async def _wf_resolve_run(args: str | None) -> tuple[dict | None, str]:
    """`[run_id]` 缺省解析（status/pause/resume/cancel 共用）。

    显式 id → 存在性校验；缺省 → 多个 running 时不猜、列候选让用户补参
    数，否则取全局最近 run（list_runs newest-first）。
    返回 ``(run, error_reply)``，二者互斥（run 为 None 时 error 非空）。"""
    from .. import workflows as wf_store

    token = (args or "").strip()
    if token:
        run = wf_store.get_run(token)
        if not run:
            return None, t(
                f"Run `{token}` not found — send `/workflow-status` to see the latest run.",
                f"未找到 run `{token}`——发送 `/workflow-status` 查看最近一次 run。",
            )
        return run, ""
    running = wf_store.list_runs(status="running")
    if len(running) > 1:
        lines = [
            t(
                "**Multiple runs are in progress — pass a run_id:**",
                "**有多个正在运行的 run——请带上 run_id 指定：**",
            ),
            "",
        ]
        lines += [_wf_run_line(r) for r in running[:10]]
        return None, "\n".join(lines)
    all_runs = wf_store.list_runs()
    if not all_runs:
        return None, t(
            "No workflow runs yet — start one with `/workflow-run <name|id>`.",
            "还没有任何工作流运行——用 `/workflow-run <名称|id>` 启动一次。",
        )
    return all_runs[0], ""

def _wf_no_defs_reply() -> str:
    return t(
        "No workflows yet — create one on the Workflows page (or synthesize "
        "one from a session), then run it here.",
        "还没有工作流——先在 Workflows 页创建（或从会话总结生成），再在这里运行。",
    )

async def _workflow_run_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    token = (args or "").strip()
    if not token:
        return _wf_family_usage()
    from .. import workflows as wf_store

    if not wf_store.list_defs():
        return _wf_no_defs_reply()
    cands = _wf_match_defs(token)
    if not cands:
        return (
            t(
                f"No workflow matches `{token}`. Existing workflows:",
                f"没有匹配 `{token}` 的工作流。现有：",
            )
            + "\n"
            + _wf_defs_table(wf_store.list_defs())
        )
    if len(cands) > 1:
        return (
            t(
                f"`{token}` matches {len(cands)} workflows — pass the exact name or id:",
                f"`{token}` 匹配到 {len(cands)} 个工作流——请用完整名称或 id 指定：",
            )
            + "\n"
            + _wf_defs_table(cands)
        )
    wf = cands[0]
    sid = (session or {}).get("session_id") or None
    ok, data = await _wf_call(
        wf_api.create_workflow_run_endpoint({"workflow_id": wf["id"], "session_id": sid})
    )
    if not ok:
        return t(f"❌ Failed to start the run: {data}", f"❌ 启动失败：{data}")
    run = (data or {}).get("run") or {}
    return t(
        f"✅ Workflow **{wf.get('name')}** started — run `{run.get('id')}` "
        f"(v{run.get('dsl_version')}).\n"
        "You'll be notified when it finishes; `/workflow-status` shows progress.",
        f"✅ 工作流 **{wf.get('name')}** 已启动 —— run `{run.get('id')}`"
        f"（v{run.get('dsl_version')}）。\n"
        "完成时会收到通知；`/workflow-status` 可查进度。",
    )

def _wf_status_card(run: dict) -> str:
    steps = run.get("steps") or []
    done = sum(1 for s in steps if s.get("status") in ("done", "skipped"))
    lines = [
        t(
            f"**Workflow run** {run.get('name') or run.get('workflow_id')} "
            f"`{run.get('id')}` — {_wf_status_label(run.get('status'))}",
            f"**工作流运行** {run.get('name') or run.get('workflow_id')} "
            f"`{run.get('id')}` —— {_wf_status_label(run.get('status'))}",
        ),
        t(f"Progress: {done}/{len(steps)} steps", f"进度：{done}/{len(steps)} 步"),
    ]
    pi = run.get("pending_interrupt")
    if isinstance(pi, dict) and pi.get("kind"):
        q = pi.get("question") or pi.get("node") or pi.get("kind")
        lines.append(
            t(
                f"⏸ Waiting for a human decision ({q}) — answer it on the "
                "Workflows page; a plain `/workflow-resume` cannot supply the answer.",
                f"⏸ 正在等待人工决定（{q}）——请到 Workflows 页回答；"
                "直接 `/workflow-resume` 无法代答。",
            )
        )
    if run.get("status") == "failed" and run.get("error"):
        lines.append(t(f"Error: {run['error']}", f"错误：{run['error']}"))
    return "\n".join(lines)

async def _workflow_status_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    run, err = await _wf_resolve_run(args)
    return err if run is None else _wf_status_card(run)

async def _workflow_pause_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    run, err = await _wf_resolve_run(args)
    if run is None:
        return err
    ok, data = await _wf_call(wf_api.pause_workflow_run_endpoint(run["id"]))
    if not ok:
        return t(f"❌ Pause failed: {data}", f"❌ 暂停失败：{data}")
    return t(
        f"⏸ Pause requested for run `{run['id']}` — it takes effect at the "
        "next safe node boundary (`/workflow-resume` continues it).",
        f"⏸ 已请求暂停 run `{run['id']}`——将在下一个安全节点边界生效"
        "（`/workflow-resume` 恢复）。",
    )

async def _workflow_resume_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    run, err = await _wf_resolve_run(args)
    if run is None:
        return err
    pi = run.get("pending_interrupt") or {}
    if run.get("status") == "paused" and pi.get("kind") in ("human", "supervisor"):
        return t(
            "This run is waiting for a human answer — reply on the Workflows "
            "page resume card (`/workflow-resume` cannot answer it for you).",
            "该 run 正在等待人工回答——请到 Workflows 页的恢复卡回答"
            "（`/workflow-resume` 无法代答）。",
        )
    ok, data = await _wf_call(wf_api.resume_workflow_run_endpoint(run["id"], {}))
    if not ok:
        return t(f"❌ Resume failed: {data}", f"❌ 恢复失败：{data}")
    return t(
        f"▶ Run `{run['id']}` resumed — `/workflow-status` follows the progress.",
        f"▶ run `{run['id']}` 已恢复——`/workflow-status` 可查进度。",
    )

async def _workflow_cancel_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from ..api import workflows as wf_api

    run, err = await _wf_resolve_run(args)
    if run is None:
        return err
    ok, data = await _wf_call(wf_api.cancel_workflow_run_endpoint(run["id"]))
    if not ok:
        return t(f"❌ Cancel failed: {data}", f"❌ 取消失败：{data}")
    return t(
        f"🛑 Run `{run['id']}` cancelled.",
        f"🛑 run `{run['id']}` 已取消。",
    )

async def _workflow_list_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    import time as _time

    from .. import workflows as wf_store

    defs = wf_store.list_defs()
    if not defs:
        return _wf_no_defs_reply()
    lines = [
        t(f"**Workflows** ({len(defs)})", f"**工作流**（{len(defs)} 个）"),
        "",
    ]
    for d in defs:
        n = len(d.get("steps") or [])
        runs = wf_store.list_runs(workflow_id=d.get("id"))
        if runs:
            r = runs[0]
            stamp = _time.strftime(
                "%m-%d %H:%M", _time.localtime(r.get("updated") or r.get("started") or 0)
            )
            tail = t(
                f"last run {_wf_status_label(r.get('status'))} ({stamp})",
                f"最近 run {_wf_status_label(r.get('status'))}（{stamp}）",
            )
        else:
            tail = t("never run", "未运行过")
        lock = " 🔒" if d.get("system") else ""
        lines.append(
            f"- **{d.get('name')}**{lock} `"
            + t(f"v{d.get('version')} · {n} steps", f"v{d.get('version')} · {n} 步")
            + "` · "
            + tail
        )
    return "\n".join(lines)

async def _workflow_delete_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    from .. import workflows as wf_store
    from ..api import workflows as wf_api

    token = (args or "").strip()
    if not token:
        return _wf_family_usage()
    if not wf_store.list_defs():
        return _wf_no_defs_reply()
    cands = _wf_match_defs(token)
    if not cands:
        return (
            t(
                f"No workflow matches `{token}`. Existing workflows:",
                f"没有匹配 `{token}` 的工作流。现有：",
            )
            + "\n"
            + _wf_defs_table(wf_store.list_defs())
        )
    if len(cands) > 1:
        return (
            t(
                f"`{token}` matches {len(cands)} workflows — pass the exact name or id:",
                f"`{token}` 匹配到 {len(cands)} 个工作流——请用完整名称或 id 指定：",
            )
            + "\n"
            + _wf_defs_table(cands)
        )
    wf = cands[0]
    ok, data = await _wf_call(wf_api.delete_workflow_endpoint(wf["id"]))
    # delete 端点有两种失败形态：HTTPException（404）与 {"ok": False,
    # "error": "Built-in workflow…"}（is_system_def 保护）——都要摊平。
    if not ok or (isinstance(data, dict) and data.get("ok") is False):
        err = data if isinstance(data, str) else (data or {}).get("error") or "unknown"
        return t(f"❌ Delete failed: {err}", f"❌ 删除失败：{err}")
    return t(
        f"🗑 Deleted workflow **{wf.get('name')}** (`{wf['id']}`). Its run "
        "history is kept (visible on the Workflows page); the definition "
        "itself can no longer be run.",
        f"🗑 已删除工作流 **{wf.get('name')}**（`{wf['id']}`）。历史 run 记录"
        "保留（Workflows 页仍可见），该定义不可再运行。",
    )


# 本域注册块：registry.py 装配 BUILTINS 时收集（命令清单契约快照钉住）。
COMMANDS: dict[str, BuiltinCommand] = {
    "workflow-run": BuiltinCommand(
        name="workflow-run",
        description=(
            "Start a workflow run (/workflow-run <name|id>, fuzzy name match)",
            "运行一个工作流（/workflow-run <名称|id>，名称模糊匹配）",
        ),
        handler=_wf_async_only_hint("workflow-run"),
        async_handler=_workflow_run_async_handler,
    ),
    "workflow-status": BuiltinCommand(
        name="workflow-status",
        description=(
            "Show the status of the latest (or a given) workflow run",
            "查看最近一次（或指定）工作流运行的状态",
        ),
        handler=_wf_async_only_hint("workflow-status"),
        async_handler=_workflow_status_async_handler,
    ),
    "workflow-pause": BuiltinCommand(
        name="workflow-pause",
        description=(
            "Pause the latest (or a given) running workflow run",
            "暂停最近（或指定）正在运行的工作流",
        ),
        handler=_wf_async_only_hint("workflow-pause"),
        async_handler=_workflow_pause_async_handler,
    ),
    "workflow-resume": BuiltinCommand(
        name="workflow-resume",
        description=(
            "Resume a paused workflow run (human answers still go via the Workflows page)",
            "恢复已暂停的工作流运行（人工回答仍走 Workflows 页）",
        ),
        handler=_wf_async_only_hint("workflow-resume"),
        async_handler=_workflow_resume_async_handler,
    ),
    "workflow-cancel": BuiltinCommand(
        name="workflow-cancel",
        description=(
            "Cancel the latest (or a given) workflow run",
            "取消最近（或指定）一次工作流运行",
        ),
        handler=_wf_async_only_hint("workflow-cancel"),
        async_handler=_workflow_cancel_async_handler,
    ),
    "workflow-list": BuiltinCommand(
        name="workflow-list",
        description=(
            "List workflow definitions (name · version · steps · last run)",
            "列出工作流定义（名称 · 版本 · 步骤数 · 最近运行）",
        ),
        handler=_wf_async_only_hint("workflow-list"),
        async_handler=_workflow_list_async_handler,
    ),
    "workflow-delete": BuiltinCommand(
        name="workflow-delete",
        description=(
            "Delete a workflow definition (/workflow-delete <name|id>; run history is kept)",
            "删除工作流定义（/workflow-delete <名称|id>；历史运行保留）",
        ),
        handler=_wf_async_only_hint("workflow-delete"),
        async_handler=_workflow_delete_async_handler,
    ),
}

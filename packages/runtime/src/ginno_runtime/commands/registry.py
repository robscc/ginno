"""Built-in slash command registry — protocol home + assembly.

Built-ins short-circuit the turn: the handler renders a reply directly and the
graph never runs (no LLM call, no checkpoint write). ``/clear`` is the one
exception handler-side: its async path rewrites the checkpoint (empties the
messages channel) before replying.

Package layout (命令包化，重构批次 1-2)：命令体按域拆在 ``commands/`` 下的
兄弟模块里，本模块只保留协议（``BuiltinCommand``，定义在 ``_base`` 并在此
re-export）与装配聚合（``BUILTINS`` 从各域收集）：

- ``help_cmds``     帮助域：help
- ``session_cmds``  会话域：clear / compact / fork / subagent / subagent-split
- ``goal_cmds``     目标域：goal / plan
- ``mount_cmds``    挂载域：mount / mounts / umount / primary
- ``dev_cmds``      开发域：dev-swap / think
- ``workflow_cmds`` 工作流域：workflow-run/-status/-pause/-resume/-cancel/
  -list/-delete

绞杀者模式兼容层：历史上全部定义都在本模块，下面的 import 块把各域名字
re-export 回 ``registry`` 命名空间——现有 ``from ...commands.registry
import X`` / ``commands.registry.X`` 引用点零改动。新代码请从各域模块直接
import；本兼容层在引用点全部迁移后可移除。

Skills are NOT registered here — they live on disk (SKILL.md) and are resolved
by the resolver via ``SkillLoader``. Names collide → built-in wins (documented
in docs/commands-and-mentions-design.md). 命令清单/文案/签名/channel_policy
由 tests/unit/test_commands_registry_contract.py 的契约快照逐字节钉住。
"""

from __future__ import annotations

from ._base import BuiltinCommand, _log
from .dev_cmds import (
    _ANSI_RE,
    _DEV_SWAP_TASKS,
    _dev_swap_async_handler,
    _dev_swap_background,
    _think_async_handler,
    _think_handler,
    _think_usage,
    COMMANDS as _DEV_COMMANDS,
)
from .goal_cmds import (
    _GOAL_USAGE_EN,
    _GOAL_USAGE_ZH,
    _STATUS_LABELS,
    _fmt_plan_line,
    _goal_handler,
    _goal_status_label,
    _goal_summary,
    _goal_usage,
    _no_goal,
    _plan_async_handler,
    _plan_handler,
    COMMANDS as _GOAL_COMMANDS,
)
from .help_cmds import _help_handler, COMMANDS as _HELP_COMMANDS
from .mount_cmds import (
    _MOUNT_USAGE_EN,
    _MOUNT_USAGE_ZH,
    _fmt_mounts,
    _match_mount,
    _mount_handler,
    _mount_usage,
    _mounts_handler,
    _mounts_list,
    _primary_handler,
    _umount_handler,
    COMMANDS as _MOUNT_COMMANDS,
)
from .session_cmds import (
    _SUBAGENT_USAGE_EN,
    _SUBAGENT_USAGE_ZH,
    _clear_async_handler,
    _clear_handler,
    _compact_async_handler,
    _compact_handler,
    _fork_async_handler,
    _fork_handler,
    _is_split_form,
    _split_form_task,
    _subagent_async_handler,
    _subagent_handler,
    _subagent_split_async_handler,
    _subagent_usage,
    fork_session_core,
    COMMANDS as _SESSION_COMMANDS,
)
from .workflow_cmds import (
    _WF_RUN_STATUS_LABELS,
    _WORKFLOW_FAMILY_USAGE_EN,
    _WORKFLOW_FAMILY_USAGE_ZH,
    _wf_async_only_hint,
    _wf_call,
    _wf_defs_table,
    _wf_family_usage,
    _wf_match_defs,
    _wf_no_defs_reply,
    _wf_resolve_run,
    _wf_run_line,
    _wf_status_card,
    _wf_status_label,
    _workflow_cancel_async_handler,
    _workflow_delete_async_handler,
    _workflow_list_async_handler,
    _workflow_pause_async_handler,
    _workflow_resume_async_handler,
    _workflow_run_async_handler,
    _workflow_status_async_handler,
    COMMANDS as _WORKFLOW_COMMANDS,
)

# 域收集顺序即装配顺序（BUILTINS 对外是无序 dict；/help 与契约快照都按
# 名称排序展示，与这里的顺序无关）。
_DOMAIN_PACKAGES: tuple[dict[str, BuiltinCommand], ...] = (
    _HELP_COMMANDS,
    _SESSION_COMMANDS,
    _GOAL_COMMANDS,
    _MOUNT_COMMANDS,
    _DEV_COMMANDS,
    _WORKFLOW_COMMANDS,
)


def _assemble_builtins(packages: tuple[dict[str, BuiltinCommand], ...]) -> dict[str, BuiltinCommand]:
    """Collect per-domain COMMANDS into one registry, refusing collisions.

    Domain packages are edited independently — a name colliding across two
    domains would silently shadow one command; fail loud at import time
    instead (first registration wins is NOT acceptable here).
    """
    merged: dict[str, BuiltinCommand] = {}
    for pkg in packages:
        for name, cmd in pkg.items():
            if name != cmd.name:
                raise RuntimeError(f"builtin command {name!r}: entry name/key mismatch")
            if name in merged:
                raise RuntimeError(f"duplicate builtin command name: {name!r}")
            merged[name] = cmd
    return merged


BUILTINS: dict[str, BuiltinCommand] = _assemble_builtins(_DOMAIN_PACKAGES)

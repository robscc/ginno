"""Instincts — 行为规则沉淀层（docs/instincts-design.md，P0 最小闭环）。

分层定位：wiki 注入（用户策展知识）/ memory pool（事实性结论）之外的
第三层——「别再这么做 / 该这么做」的操作性行为规则，带置信度随使用
自动升降级。P0 只做确定性事件（权限拒绝/重试自救）的捕获 + 模板规则
生成（零 LLM）+ 注入管道 + 管理页。

用法（graph / engine 插桩）：
    from .instincts import observe, maybe_analyze, build_instinct_context
"""

from .types import Instinct, InstinctsConfig, load_instincts_config
from .store import (
    list_instincts,
    get_instinct,
    save_instinct,
    delete_instinct,
    instincts_dir_for,
    observations_path,
)
from .capture import observe, recent_observations, maybe_analyze
from .inject import build_instinct_context

__all__ = [
    "Instinct",
    "InstinctsConfig",
    "load_instincts_config",
    "list_instincts",
    "get_instinct",
    "save_instinct",
    "delete_instinct",
    "instincts_dir_for",
    "observations_path",
    "observe",
    "recent_observations",
    "maybe_analyze",
    "build_instinct_context",
]

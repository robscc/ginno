"""后台任务系统（background-tasks-design.md）：进程注册表 + 持久化 + 注入。"""

from .registry import TaskRegistry, get_registry, reset_registry
from . import store

__all__ = ["TaskRegistry", "get_registry", "reset_registry", "store"]

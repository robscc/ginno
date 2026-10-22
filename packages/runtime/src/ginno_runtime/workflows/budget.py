"""Run 级 LLM 调用预算(odw-comparison P1-③,ODW maxAgentCalls 的 Ginno 适配)。

语义(对齐 ODW):**每次尝试都计数**——agent/llm 节点的一次执行、它的每次
retry、并行 loop 的每个 item 各算一次。retry 放大成本失控面,只数首次调用
不够。预算耗尽是**硬失败**:BudgetExceeded 不吃 retry(再试只会再花),节点
报错、按 on_error 语义落 stop/continue。

预算来源(高优先者胜):
1. create-run 的 ``max_agent_calls`` 参数(no_cache 同款 per-run 钉死);
2. DSL 顶层 ``budget.maxAgentCalls``;
3. 设置 ``workflow_max_agent_calls``(全局默认,0 = 不设限)。

与 recursion_limit 的分工:后者防图结构死循环(superstep 数),前者防真金
白银的模型调用数——调度触发的无人值守 run 尤其需要后者覆盖不到的成本闸。
"""

from __future__ import annotations

from typing import Any

_UNSET = object()


class BudgetExceeded(Exception):
    """Run 的 LLM 调用预算耗尽。控制流级:retry 边界不重试(见 base.py)。"""

    def __init__(self, node_id: str, spent: int, limit: int) -> None:
        self.node_id = node_id
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"run budget exhausted: {spent} agent/LLM calls spent "
            f"(limit {limit}); node '{node_id}' refused. "
            "Raise budget.maxAgentCalls or disable the workflow_max_agent_calls "
            "setting to allow more."
        )


class AgentCallBudget:
    """Run 级计数器。``limit=0`` 表示不设限(缺省)。"""

    __slots__ = ("limit", "spent")

    def __init__(self, limit: int = 0) -> None:
        self.limit = max(0, int(limit or 0))
        self.spent = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def spend(self, node_id: str) -> None:
        """记一次调用尝试;超限抛 BudgetExceeded。

        先加后判:超限的那次**不执行**(spend 在节点执行前调用),但计数
        已含被拒尝试,事件里 spent/limit 如实。"""
        self.spent += 1
        if self.enabled and self.spent > self.limit:
            raise BudgetExceeded(node_id=node_id, spent=self.spent, limit=self.limit)


def resolve_limit(
    run_override: Any = _UNSET,
    dsl: dict | None = None,
    setting: Any = _UNSET,
) -> int:
    """预算合成:run 参数 > DSL budget.maxAgentCalls > 设置;0 = 不设限。"""
    for cand in (run_override, (dsl or {}).get("budget", {}).get("maxAgentCalls"), setting):
        if cand is _UNSET or cand is None:
            continue
        try:
            return max(0, int(cand))
        except (TypeError, ValueError):
            continue
    return 0

"""Memory summarization: distill pool excerpts into MEMORY.md via LLM.

The summarizer reads all pool entries + existing MEMORY.md, calls the LLM with
SUMMARIZE_PROMPT to merge/extract reusable knowledge, writes back to MEMORY.md,
then clears the pool.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from .. import paths
from ..graph import text_of_content
from ..models import build_model
from .pool import clear_pool, read_pool, sanitize_for_memory

SUMMARIZE_PROMPT = """\
你是一个知识提炼器。你的任务是从对话摘录中提取可长期复用的知识，合并进现有的全局记忆中。

## 提取标准
提取以下类型的知识：
1. **技术决策** — 架构选型、API 约定、配置变更、技术栈选择
2. **问题诊断** — 排查过的 bug、根因、解决方案（只保留通用结论，不要保留调试过程）
3. **用户偏好** — 工作习惯、沟通风格、工具偏好、命名规范
4. **项目上下文** — 正在进行的工作、里程碑、依赖关系、阻塞项
5. **可复用模式** — 反复出现的代码模式、流程、最佳实践

## 过滤标准（不提取）
- 一次性操作细节（"我帮你修了这个文件"这类执行过程）
- 临时调试步骤和错误堆栈
- 闲聊、寒暄、过渡性对话
- 已被后续对话推翻的结论
- 过于具体以至于跨 session 无法复用的信息

## 输出要求
- 直接输出合并后的完整记忆，不要输出分析过程
- 按主题分组，使用 Markdown 标题（## 主题）
- 每条知识一行 bullet point
- 如果新知识与现有记忆冲突，以新知识为准，更新旧条目
- 保持精炼，总量控制在 3000 字以内
- 使用与源内容相同的语言
"""


def _read_existing_memory() -> str:
    """Read existing MEMORY.md (or empty string if not present)."""
    p = paths.memory_index_path()
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    # Skip the default boilerplate
    if text.startswith("# Ginno Memory"):
        return ""
    return text


def _write_memory(text: str) -> None:
    """Write sanitized memory text to MEMORY.md."""
    sanitized = sanitize_for_memory(text)
    paths.memory_index_path().write_text(sanitized, encoding="utf-8")


async def summarize_pool(model_provider: str | None = None) -> dict:
    """Summarize pool excerpts into MEMORY.md.

    Returns {ok, summarized_chars, pool_entries, error?}.
    """
    pool = read_pool()
    if not pool:
        return {"ok": True, "summarized_chars": 0, "pool_entries": 0, "message": "pool empty"}

    existing = _read_existing_memory() or "(empty)"
    excerpts = "\n\n---\n\n".join(e.get("content", "") for e in pool if e.get("content"))

    try:
        provider = model_provider or _get_default_provider()
        model = build_model(provider)
    except Exception as e:
        return {"ok": False, "error": f"model build failed: {e}", "pool_entries": len(pool)}

    try:
        response = await model.ainvoke([
            SystemMessage(content=SUMMARIZE_PROMPT),
            HumanMessage(content=f"## Existing Memory\n{existing}\n\n## New Conversation Excerpts\n{excerpts}\n\nPlease produce an updated memory summary."),
        ])
        new_memory = text_of_content(response.content)
        _write_memory(new_memory)
        clear_pool()
        return {
            "ok": True,
            "summarized_chars": len(new_memory),
            "pool_entries": len(pool),
        }
    except Exception as e:
        return {"ok": False, "error": f"summarization failed: {e}", "pool_entries": len(pool)}


def _get_default_provider() -> str:
    """Get the default enabled provider (fallback to 'custom')."""
    from .. import providers as prov_mod

    return prov_mod.get_default_provider()

"""Knowledge / LLMWiki subsystem.

Ports Molly's WikiLLM approach to Ginno's Python runtime: scan an Obsidian vault
into an in-memory index, retrieve relevant entries via multi-signal scoring
(no embeddings by default), and inject them into the agent system prompt.

P0 scope: read-only retrieval + injection. Compilation, association discovery,
and the memory refinery land in later phases (see docs/knowledge-and-wiki-design.md).
"""

from .config import load_knowledge_config, save_knowledge_config
from .types import KnowledgeConfig, RetrievalResult, WikiEntry

__all__ = [
    "WikiEntry",
    "RetrievalResult",
    "KnowledgeConfig",
    "load_knowledge_config",
    "save_knowledge_config",
]

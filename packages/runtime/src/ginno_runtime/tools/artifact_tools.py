"""Artifact tools — explicit registration (attach_ref auto-registers too)."""

from __future__ import annotations

from langchain_core.tools import tool

ARTIFACT_TOOL_NAMES = {"artifact_register", "artifact_list"}


@tool
def artifact_register(kind: str, name: str, ref: str = "") -> str:
    """Register an artifact (file/doc/workflow/link) in the Artifacts panel."""
    # The actual store write happens server-side (it knows the project slug);
    # this tool's return just confirms intent. The WS layer persists it.
    return f"[registered {kind}: {name}]"


ALL_ARTIFACT_TOOLS = [artifact_register]

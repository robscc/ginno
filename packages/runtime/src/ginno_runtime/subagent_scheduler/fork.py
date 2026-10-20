"""Fork seeding (P3 contract 2): copy the parent's checkpoint messages into
the fork child's fresh checkpoint under freshly minted ids, healing the
dangling tool_call tail the parent's in-flight superstep leaves behind."""

from __future__ import annotations

import sys

_NS = sys.modules[__package__]  # package namespace: all former module globals


# The fork child's first turn: the brief already sits at the top of the seeded
# history, so the turn itself only needs a start instruction.
def _fork_start_text() -> str:
    return _NS.t(
        "This is a parallel branch (fork) split from the parent conversation; the complete "
        "parent context (including your brief) is inherited above. Start working on the "
        "goal directly, and output the final report in the report format when done.",
        "这是从父对话分出的并行分支（fork），上方已继承父对话的完整上下文"
        "（含你的 brief）。请直接开始执行目标，完成后按报告格式输出最终报告。",
    )


def _fork_copy(m):
    """Deep-faithful copy of one checkpoint message under a FRESH id.

    The id discipline (steering/checkpointer contract): the file checkpointer's
    delta compares message ids only, so any copy into another session's
    history must re-mint every id — a reused id that later diverges would
    silently store nothing. tool_calls / tool_call_id / additional_kwargs are
    preserved (attribution tags, steer markers, image/code trailers all ride
    there), so the child's UI history replays faithfully.

    Strip-chain verdict (P3 contract 2 核查项): the seed keeps FULL fidelity —
    strip_old_images / strip_tool_*_markers are MODEL-VIEW transforms applied
    by agent_node at every turn, so the fork child's model context is bounded
    by the same chain automatically. Stripping in the SEED would instead
    corrupt the child's persisted record (its UI history would lose the image
    payloads / code-panel markers the parent's view still shows)."""
    import copy as _copy
    import uuid as _uuid

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    kw = dict(getattr(m, "additional_kwargs", None) or {})
    if isinstance(m, HumanMessage):
        return HumanMessage(content=m.content, additional_kwargs=kw, id=_fork_id(m))
    if isinstance(m, ToolMessage):
        return ToolMessage(
            content=m.content,
            tool_call_id=getattr(m, "tool_call_id", None),
            name=getattr(m, "name", None),
            additional_kwargs=kw,
            id=_fork_id(m),
        )
    if isinstance(m, AIMessage):
        return AIMessage(
            content=m.content,
            tool_calls=list(getattr(m, "tool_calls", None) or []),
            additional_kwargs=kw,
            id=_fork_id(m),
        )
    out = _copy.deepcopy(m)
    try:
        out.id = f"fork-{_uuid.uuid4().hex[:12]}"
    except Exception:
        pass
    return out


def _fork_id(_m) -> str:
    import uuid as _uuid

    return f"fork-{_uuid.uuid4().hex[:12]}"


def _heal_fork_tail(copies: list) -> int:
    """Answer dangling tool_calls at the tail of the COPIED history.

    A fork always runs from inside the parent's LIVE turn (the spawn tool
    executes in the parent's tools node), and checkpoints commit per
    superstep — so the parent checkpoint being copied ends with the AIMessage
    whose tool_calls (spawn_subagent's own, plus any parallel calls in the
    same batch) have NO ToolMessage answers yet; the batch commits only when
    the tools node returns. Copied as-is, the child's first model request
    forwards a tool_use with no tool_result and the provider API 400s every
    turn of the fork child — the exact shape ``_heal_interrupted_turn``
    documents and repairs for a stopped turn (engine.py). This appends the
    same "(interrupted)" placeholders to the COPY only: the parent's own turn
    continues and answers those calls for real, so the parent's record and
    the child's divergence here is correct (the child inherits context, not
    the parent's in-flight work).

    Returns the number of placeholder ToolMessages appended. Only the TAIL
    can dangle: mid-history tool batches are complete at every committed
    superstep (and a stopped turn's dangling tail was already healed in the
    parent before any later turn ran)."""
    from langchain_core.messages import AIMessage, ToolMessage

    if not copies or not isinstance(copies[-1], AIMessage):
        return 0
    added = 0
    for tc in getattr(copies[-1], "tool_calls", None) or []:
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if not tc_id:
            continue
        tc_name = (
            tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        ) or ""
        copies.append(
            ToolMessage(
                content="(interrupted)",
                tool_call_id=tc_id,
                name=tc_name,
                # Fresh id: the checkpointer's delta is id-keyed; a reused or
                # missing id would corrupt the child's delta chain.
                id=_fork_id(None),
            )
        )
        added += 1
    if added:
        _NS._log.info(
            "subagent_fork_tail_healed placeholders=%d", added,
        )
    return added


async def _seed_fork_history(
    parent_session_id: str, child_id: str, slug: str, brief_text: str
) -> int:
    """Copy the parent's current checkpoint messages into the child's fresh
    checkpoint (via the child graph's own aupdate_state — the same rewrite
    path compaction uses, so the checkpoint shape is guaranteed correct),
    prefixed by the standalone brief. Returns the number of copied messages."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..checkpointer import FileCheckpointer

    tup = await FileCheckpointer(slug).aget_tuple(
        {"configurable": {"thread_id": parent_session_id}}
    )
    msgs = (
        ((tup.checkpoint.get("channel_values") or {}).get("messages") or [])
        if tup
        else []
    )
    copies = [
        _fork_copy(m) for m in msgs if not isinstance(m, SystemMessage)
    ]
    # The parent is mid-turn (the fork runs inside its tools node): heal the
    # dangling tool_call tail BEFORE seeding, or the child's first turn 400s.
    _heal_fork_tail(copies)
    brief_msg = HumanMessage(
        content=brief_text,
        # New id, same discipline as every steer/result injection.
        id=f"fork-brief-{_NS.uuid.uuid4().hex[:10]}",
        additional_kwargs={"ginno_subagent_fork": parent_session_id},
    )
    child = _NS._SESSIONS.get(child_id)
    graph = (child or {}).get("graph")
    if graph is None:
        raise RuntimeError(
            _NS.t(f"fork child session {child_id} has no available graph",
                  f"fork 子会话 {child_id} 无可用 graph")
        )
    await graph.aupdate_state(
        {
            "configurable": {
                "thread_id": child_id,
                "project_slug": slug,
                "agent_id": (child or {}).get("agent_id"),
            }
        },
        {"messages": [brief_msg, *copies]},
        as_node="agent",
    )
    _NS._log.info(
        "subagent_fork_seeded parent=%s child=%s messages=%d",
        parent_session_id, child_id, len(copies),
    )
    return len(copies)

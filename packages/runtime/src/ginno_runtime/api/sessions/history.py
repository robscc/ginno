"""Paged view of the same transcript used by the full history contract."""

import hashlib
import re
from bisect import bisect_right
from collections import Counter

import orjson


def _digest(value: object) -> str:
    data = orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS, default=str)
    return hashlib.sha256(data).hexdigest()[:24]


def address_rows(rows: list[dict]) -> None:
    """Give older records without message IDs the same address in every view."""
    seen: Counter[str] = Counter()
    turn_id = None
    for row in rows:
        if not row.get("id"):
            identity = _digest([row["role"], row["blocks"]])
            seen[identity] += 1
            row["id"] = f"history-{identity}-{seen[identity]}"
        if row["role"] == "user":
            turn_id = row.get("turnId") or row["id"]
        if row["role"] == "assistant" and turn_id:
            row["turnId"] = turn_id


def _tool_header(block: dict) -> str:
    content = block["content"]
    first = content.lstrip().split("\n", 1)[0].rstrip()
    if content.strip() == "(interrupted)":
        return "(interrupted)"
    if block["name"] == "bg_run" and (task := re.search(r"\btask=t_[0-9a-f]+", content)):
        return task[0]
    if block["name"] == "delegate_agent" and first.startswith("[delegate "):
        link = re.search(r"^delegation=[0-9a-f]+$", content, re.M)
        return first + (f"\n{link[0]}" if link else "")
    if re.match(r"^\[(?:exit -?\d+|timeout after [^\]]+)\]\n", content):
        exit_line = content.partition("\n")[0]
        stderr = content.partition("\n--- stderr ---\n")[2].strip().partition("\n")[0]
        return f"{exit_line}\n\n--- stderr ---\n{stderr[:300]}" if stderr and exit_line != "[exit 0]" else exit_line + "\n"
    if block.get("failed") or re.match(r"^\[(?:error\]|blocked:)", content, re.I):
        return first[:300]
    return ""


def tool_records(rows: list[dict]):
    for row in rows:
        for index, block in enumerate(row["blocks"]):
            if block.get("kind") != "tool" or not (block.get("content") or block.get("diff")):
                continue
            # Include the result revision: a cache entry for a checkpoint that
            # precedes microcompaction must never resurrect a cleared output.
            key = _digest([row["id"], index, block.get("id"), block["content"], block.get("diff")])
            yield row, index, block, key


def paginate(
    rows: list[dict], starts: set[int], aliases: dict[str, tuple[int, int | None]], *, session_id: str,
    turns: int, before: str | None, refresh: str | None, coverage: str | None,
    revision: str | None, target: str | None,
) -> tuple[list[dict], dict]:
    address_rows(rows)

    notice = None
    if rows and str(rows[0].get("id", "")).startswith("microcompact:"):
        notice = rows[0]
    first = int(notice is not None)
    boundaries = sorted({first, *(i for i in starts if first <= i < len(rows))})
    # Each cursor carries the identity of the first row and a digest of the
    # prefix. Appending new turns preserves older boundaries. Rewrites that
    # affect a retained prefix force a fresh projection of the loaded range.
    prefix = hashlib.sha256()
    cursors = {}
    start_set = set(boundaries)
    for index in range(first, len(rows)):
        if index in start_set:
            cursors[index] = f"{rows[index]['id']}:{prefix.hexdigest()[:24]}"
        prefix.update(_digest(rows[index]).encode())
    if notice is not None:
        prefix.update(_digest(notice).encode())
    current = prefix.hexdigest()[:24]
    positions = {cursor: index for index, cursor in cursors.items()}
    by_id = {row["id"]: index for index, row in enumerate(rows)}
    tail = boundaries[max(0, len(boundaries) - turns)]
    start = tail
    end = len(rows)
    mode = "replace"
    if before:
        # Anchor by identity if the preceding rows were rewritten. The
        # response then replaces the entire loaded span, including the tail.
        cursor_id = before.rsplit(":", 1)[0]
        anchor = by_id.get(cursor_id)
        if anchor in start_set:
            ordinal = bisect_right(boundaries, anchor) - 1
            start = boundaries[max(0, ordinal - turns)]
            if revision == current and positions.get(before) == anchor:
                end = anchor
                mode = "prepend"
    elif refresh in positions:
        start = positions[refresh]
        mode = "refresh"
    if mode == "replace" and coverage:
        if coverage in by_id:
            start = min(start, boundaries[bisect_right(boundaries, by_id[coverage]) - 1])
        elif refresh or before:
            # Compaction can delete the oldest visited row while keeping
            # later visited turns. Rebuild from the surviving prefix.
            start = first
    target_index = None
    target_block = None
    if target is not None:
        target_index, target_block = aliases.get(target, (by_id.get(target), None))
        covered = by_id.get(coverage)
        if revision == current and (target_index is None or (covered is not None and target_index >= covered)):
            mode = "locate"
            start = end
        elif target_index is not None and target_index >= first:
            start = min(start, boundaries[bisect_right(boundaries, target_index) - 1])
    page_rows = rows[start:end]
    for row, index, block, key in tool_records(page_rows):
        content = block["content"]
        stub = {
            **block,
            "content": _tool_header(block),
            "detail": {"sessionId": session_id, "key": key, "chars": len(content),
                       "lines": content.count("\n") + 1},
        }
        if diff := block.get("diff"):
            stub["diff"] = {**diff, "text": ""}
        row["blocks"][index] = stub

    return page_rows, {
        "mode": mode,
        "tail": cursors.get(tail),
        "before": cursors.get(start) if start > first else None,
        "coverage": rows[start]["id"] if start < len(rows) else None,
        "revision": current,
        "notice": notice,
        "target": {"id": rows[target_index]["id"], "block": target_block} if target_index is not None else None,
    }

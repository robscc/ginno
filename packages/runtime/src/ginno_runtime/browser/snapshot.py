"""CDP accessibility snapshot → ego-style text + @N / loc= refMap.

Honesty target is 85% of the main document (design §6). Cross-origin iframes
and closed shadow roots are omitted on purpose.
"""

from __future__ import annotations

from typing import Any

# Roles that are worth exposing as refs. Everything else is still walked so
# we can keep the tree, but only these get an @N the agent can click/fill.
_INTERACTIVE = {
    "button",
    "link",
    "textbox",
    "searchbox",
    "combobox",
    "checkbox",
    "radio",
    "switch",
    "menuitem",
    "tab",
    "option",
    "slider",
    "spinbutton",
    "listbox",
    "treeitem",
    "heading",
    "image",
    "cell",
    "gridcell",
    "row",
    "listitem",
}


def _prop(node: dict, name: str) -> Any:
    for p in node.get("properties") or []:
        if isinstance(p, dict) and p.get("name") == name:
            return p.get("value")
    return None


def _name(node: dict) -> str:
    raw = node.get("name")
    if isinstance(raw, dict):
        return str(raw.get("value") or "")
    return str(raw or "")


def _role(node: dict) -> str:
    raw = node.get("role")
    if isinstance(raw, dict):
        return str(raw.get("value") or "generic")
    return str(raw or "generic")


def _ignored(node: dict) -> bool:
    return bool(node.get("ignored"))


def flatten_ax(
    root: dict | None,
    *,
    max_nodes: int = 400,
    start_ref: int = 0,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Walk an Accessibility.getFullAXTree payload into snapshot text + refs.

    ``start_ref`` continues @N numbering across same-origin iframe trees so
    a page with frames does not collide refs (M2).
    """
    if not root:
        return "", {}
    nodes_in = root.get("nodes") if isinstance(root, dict) else None
    if isinstance(nodes_in, list) and nodes_in:
        by_id = {n.get("nodeId"): n for n in nodes_in if isinstance(n, dict)}
        root_id = nodes_in[0].get("nodeId")
        children_of: dict[Any, list] = {}
        for n in nodes_in:
            if not isinstance(n, dict):
                continue
            kids = n.get("childIds") or n.get("children") or []
            children_of[n.get("nodeId")] = [by_id[k] for k in kids if k in by_id]
        tree_root = by_id.get(root_id) or nodes_in[0]
    else:
        tree_root = root if isinstance(root, dict) else {}
        children_of = None

    lines: list[str] = []
    refs: dict[str, dict[str, Any]] = {}
    counter = [max(0, int(start_ref))]

    def walk(node: dict, depth: int) -> None:
        if counter[0] >= max_nodes or not isinstance(node, dict):
            return
        if _ignored(node):
            kids = (
                children_of.get(node.get("nodeId"), [])
                if children_of is not None
                else (node.get("children") or [])
            )
            for c in kids:
                walk(c, depth)
            return
        role = _role(node)
        name = _name(node).strip()
        # Document / layout roles keep the tree walk but never get an @N.
        skip_roles = {
            "generic",
            "none",
            "InlineTextBox",
            "LineBreak",
            "RootWebArea",
            "WebArea",
            "document",
            "ignored",
        }
        interesting = role in _INTERACTIVE or (name and role not in skip_roles)
        backend = node.get("backendDOMNodeId")
        loc = None
        if interesting:
            counter[0] += 1
            ref = str(counter[0])
            loc = _css_guess(role, name, node)
            refs[ref] = {
                "ref": ref,
                "role": role,
                "name": name,
                "loc": loc,
                "backendNodeId": backend,
            }
            extra = f", loc={loc!r}" if loc else ""
            indent = "  " * max(depth, 0)
            label = name or role
            lines.append(f"{indent}[ref={ref}{extra}] [{role}] {label}")
        kids = (
            children_of.get(node.get("nodeId"), [])
            if children_of is not None
            else (node.get("children") or [])
        )
        next_depth = depth + 1 if interesting else depth
        for c in kids:
            walk(c, next_depth)

    walk(tree_root, 0)
    return "\n".join(lines), refs


def _css_guess(role: str, name: str, node: dict) -> str:
    html_tag = _prop(node, "htmlTag") or ""
    if isinstance(html_tag, dict):
        html_tag = html_tag.get("value") or ""
    html_id = _prop(node, "idForLabel") or ""
    if isinstance(html_id, dict):
        html_id = html_id.get("value") or ""
    if html_id:
        return f"#{html_id}"
    if html_tag and name:
        safe = name.replace("'", "\\'")[:40]
        return f"{html_tag}[aria-label='{safe}'], {html_tag}:has-text('{safe}')"
    if html_tag:
        return str(html_tag)
    return f"[role='{role}']"


def format_snapshot(url: str, title: str, tree_text: str) -> str:
    head = f"[document] {title or ''} — {url or ''}"
    return head if not tree_text else f"{head}\n{tree_text}"


def merge_ax_frames(
    frames: list[tuple[str, dict | None]],
    *,
    max_nodes: int = 400,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Flatten the main AX tree plus same-origin iframe trees into one refMap.

    Each item is ``(label, ax_payload)``. An empty label is the top document.
    ``ax_payload is None`` means the frame was skipped (cross-origin / closed).
    """
    lines: list[str] = []
    refs: dict[str, dict[str, Any]] = {}
    used = 0
    for label, tree in frames:
        if tree is None:
            if label:
                lines.append(f"  [iframe] {label} (cross-origin, omitted)")
            continue
        remaining = max(0, max_nodes - used)
        if remaining <= 0:
            break
        body, part = flatten_ax(tree, max_nodes=remaining, start_ref=used)
        if label:
            lines.append(f"  [iframe] {label}")
        if body:
            lines.append(body)
        refs.update(part)
        if part:
            used = max(used, max(int(k) for k in part))
    return "\n".join(lines), refs


def flatten_frame_tree(tree: dict | None) -> list[dict[str, Any]]:
    """Walk Page.getFrameTree.frameTree into a depth-first list of frames."""
    if not isinstance(tree, dict):
        return []
    frame = tree.get("frame") if isinstance(tree.get("frame"), dict) else {}
    out: list[dict[str, Any]] = [frame] if frame else []
    for child in tree.get("childFrames") or []:
        if isinstance(child, dict):
            out.extend(flatten_frame_tree(child))
    return out

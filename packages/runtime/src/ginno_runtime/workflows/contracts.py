"""Machine-readable node contracts for DSL authors (stability plan P1b).

Single source of truth for what the synthesis prompt and the workflow-dev
agent teach about node types: the node registry (types / aliases /
params_schema) plus a static table of structural rules and one-line examples
that live outside the schemas (edge prohibitions, writes semantics). Previously these rules were hand-maintained in two places
(_SYNTHESIZE_PROMPT and the workflow-dev seed prompt) and drifted.

Rendered lazily: ``render_catalog`` imports the nodes package at call time —
importing this module at module scope would be circular (nodes.builtin
imports agents).
"""

from __future__ import annotations

# Structural rules that params_schema cannot express. Kept terse: they are
# injected into LLM prompts, so every line costs tokens on every synthesis.
_STRUCTURAL_RULES: dict[str, list[str]] = {
    "agent": [
        "goal (or title) required; `agent` optional — omit it to default to the dev "
        "agent (fallback order dev→research→writer); never invent role names; "
        "optional skills [names]",
        "declare `writes` {key: json-schema} when a later step/loop consumes this "
        "step's data; key must match the {{context.key}} used downstream; "
        'lists: {"type":"array","items":{"type":"object"}}',
        "do NOT put WRITE_JSON instructions in goals — the engine extracts "
        "structured output from declared writes",
    ],
    "llm": [
        "prompt required; single shot, no tools; optional `output` key writes one context key",
    ],
    "branch": [
        "cases [{when, then}] first match wins + default; when = sandboxed expr over context",
        "routes via cases/default ONLY — never add plain edges from a branch "
        "(per-case `transform` allowed)",
    ],
    "loop": [
        "over (expr e.g. context.items, or int) + as + body + max_iters>=1 required; "
        "on_empty skip|fail",
        "body returns to the loop head structurally: do NOT add an edge FROM the "
        "body; reference the item via {{<as>}}; loop has at most one explicit "
        "out-edge (done/next)",
        'optional parallel:true | {max_concurrency:1..8} = run the body over ALL '
        "items at once (data parallel). Contract: the body must be a step/agent "
        'whose `writes` keys are ALL {"type":"array"} — each item appends one '
        "element in index order; results land as arrays in context",
    ],
    "human": [
        "question; pauses the run for a user answer (resume may patch context)",
    ],
    "python": [
        "deterministic whitelisted script — NO LLM: prefer for mechanical "
        "fetch/normalize/compute steps",
        "`entry` must be a registered name (validate rejects unknown entries); "
        "`args` accepts {{...}} templates or plain objects; the return value is "
        "written back to context per `writes`",
    ],
    "pass": ["no-op placeholder; terminal or connector"],
}

_EXAMPLES: dict[str, str] = {
    "agent": '{"id":"search","type":"step","agent":"research","goal":"Select the top '
    'stocks.","writes":{"stocks":{"type":"array","items":{"type":"object"}}}}',
    "llm": '{"id":"sum","type":"llm","prompt":"Summarise {{context.reports}}","output":"digest"}',
    "branch": '{"id":"gate","type":"branch","cases":[{"when":"len(context.drafts) > 0",'
    '"then":"notify"}],"default":"done"}',
    "loop": '{"id":"each","type":"loop","over":"context.stocks","as":"stock",'
    '"body":"review","max_iters":50}',
    "human": '{"id":"confirm","type":"human","question":"Publish these drafts?"}',
    "python": '{"id":"bills","type":"python","entry":"fetch_aliyun_bills",'
    '"args":{"month":"{{context.month}}"},"writes":{"bills":{"type":"array","items":{"type":"object"}}}}',
    "pass": '{"id":"done","type":"pass"}',
}


def node_contracts() -> list[dict]:
    """Enumerate every non-internal registered node kind with its schema,
    structural rules and a one-line example."""
    from .nodes import registry

    out: list[dict] = []
    for t in registry.known_types():
        cls = registry.get_node(t)
        if cls is None or getattr(cls, "_internal", False):
            continue  # extract is compiler-internal; authors must not use it
        out.append(
            {
                "type": t,
                "aliases": list(getattr(cls, "aliases", ()) or ()),
                "params_schema": getattr(cls, "params_schema", {"type": "object"}),
                "rules": _STRUCTURAL_RULES.get(t, []),
                "example": _EXAMPLES.get(t, ""),
            }
        )
    return out


def render_catalog(char_budget: int = 2400, include_examples: bool = True) -> str:
    """Compact text rendering for prompt injection.

    Degrades in two steps when over budget: drop examples first, then keep
    only the first rule per type — the type/field listing itself is never
    dropped (it is the contract)."""
    for with_examples in ((include_examples, False) if include_examples else (False,)):
        text = _render(with_examples)
        if len(text) <= char_budget:
            return text
    # still over: one rule per type
    return _render(False, max_rules=1)


def _render(with_examples: bool, max_rules: int | None = None) -> str:
    lines: list[str] = []
    for c in node_contracts():
        props = list((c["params_schema"].get("properties") or {}).keys())
        req = c["params_schema"].get("required") or []
        head = c["type"]
        if c["aliases"]:
            head += " (alias " + "/".join(c["aliases"]) + ")"
        fields = ", ".join(props) if props else "no fields"
        line = f"- {head}: {fields}"
        if req:
            line += f" [required: {', '.join(req)}]"
        lines.append(line)
        rules = c["rules"] if max_rules is None else c["rules"][:max_rules]
        for r in rules:
            lines.append(f"  · {r}")
        if with_examples and c["example"]:
            lines.append(f"  e.g. {c['example']}")
    return "\n".join(lines)

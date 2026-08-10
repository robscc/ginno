"""Compiler-internal ``extract`` node (master-plan §2.2).

Moves structured-output responsibility off the step model and onto a dedicated
extraction step the compiler injects after any ``step``/``agent`` node that
declares ``writes``. The step model just pursues its goal; this node reads the
step's final reply text (``state.results[source_node]``) and produces strict
JSON conforming to the declared ``writes`` schema — arrays first-class.

Users never write ``extract`` nodes; the compiler synthesizes them
(:func:`ginno_runtime.workflows.compiler._inject_extract_nodes`). The node is
registered so the compiler can build it, and ``dsl.validate_dsl`` accepts the
type, but it is marked ``_internal`` so tooling/UI can treat it as synthetic.
"""

from __future__ import annotations

import json
import re
import time

from langchain_core.messages import HumanMessage

from ...graph import text_of_content
from . import agent_helpers as ah
from .base import BaseNode, llm_invoke_with_timeout, validate_against
from .registry import register_node

# Cap how much of the source reply we hand the extractor. Long replies (full
# reports) usually carry the structured list near the end; 8000 chars keeps the
# extraction prompt cheap while covering typical outputs (master-plan §2.2 L).
_SOURCE_CHAR_CAP = 8000


def _validate_writes(data: dict, schema: dict) -> tuple[dict, list[str]]:
    """Type-check each declared key; return (validated_dict, errors)."""
    errs: list[str] = []
    out: dict = {}
    for key, key_schema in (schema or {}).items():
        if not isinstance(key_schema, dict):
            key_schema = {}
        val = data.get(key)
        if val is None:
            errs.append(f"key '{key}' missing or null")
            continue
        key_errs = validate_against(key_schema, val)
        if key_errs:
            errs.extend(f"{key}: {e}" for e in key_errs)
        else:
            out[key] = val
    return out, errs


def _cap_text(text: str, cap: int) -> str:
    """Keep the head AND tail when truncating: structured results (lists, final
    answers) frequently sit at the END of a long step reply, so a head-only cut
    would drop exactly what the extractor needs (master-plan §2.2 L)."""
    if len(text) <= cap:
        return text
    tail = cap // 3
    head = cap - tail
    return text[:head] + "\n…[中段省略]…\n" + text[-tail:]


def _extract_json_from_text(text: str) -> dict:
    """Return the first complete JSON object in ``text`` (fence-tolerant)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            v = json.loads(text[i : j + 1])
            if isinstance(v, dict):
                return v
        except json.JSONDecodeError:
            pass
    raise ValueError("no JSON object found in extraction output")


@register_node
class ExtractNode(BaseNode):
    """Read a source step's reply, emit strict JSON for the declared ``writes``."""

    type = "extract"
    _internal = True  # compiler-synthesized; not user-authored
    params_schema = {
        "type": "object",
        "required": ["source_node", "writes"],
        "properties": {
            "source_node": {"type": "string"},
            "writes": {"type": "object"},
            "extract_model": {"type": "string"},
            "back_to": {"type": "string"},
        },
    }

    @staticmethod
    async def execute(node, cctx, state, config, eff) -> dict:
        run_ctx = cctx["run_ctx"]
        node_id = node["id"]
        source_id = node["source_node"]
        writes_schema = node.get("writes") or {}
        context = dict(state.get("context") or {})
        context_meta = dict(state.get("context_meta") or {})
        events: list = []

        def emit(ev):
            ev.setdefault("ts", time.time())
            events.append(ev)
            run_ctx["events"].append(ev)

        emit({"run_id": run_ctx["run_id"], "node_id": node_id,
              "kind": "node_enter", "node_type": "extract"})

        source_text = (state.get("results") or {}).get(source_id, "") or ""

        def _commit(validated: dict, method: str) -> dict:
            for k in validated:
                context_meta[k] = f"extract:{node_id}"
            emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                  "kind": "context_write", "keys": list(validated.keys()),
                  "method": method})
            emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                  "kind": "node_exit", "status": "done"})
            return {
                "context": {**context, **validated},
                "context_meta": context_meta,
                "events": events,
                "__output__": validated,
            }

        # ---- Fast path: the step already emitted a valid WRITE_JSON covering
        # every declared key — adopt it and skip the extraction LLM call. ----
        wj = ah.parse_writes(source_text)
        if wj and all(k in wj for k in writes_schema):
            validated, errs = _validate_writes(wj, writes_schema)
            if not errs:
                return _commit(validated, "write_json")

        # ---- Main path: dedicated extraction LLM (≤2 attempts). ----
        from ...models import build_model_by_name

        extract_model_name = node.get("extract_model")
        try:
            m = build_model_by_name(extract_model_name) if extract_model_name else cctx["model"]
        except Exception:
            # A misconfigured extract_model must not kill the run's dep build;
            # degrade to the step model so extraction still has a chance.
            m = cctx["model"]

        schema_str = json.dumps(writes_schema, ensure_ascii=False)
        # Enumerate the exact top-level keys (with types) + an output skeleton so
        # the model can't rename/omit them — the #1 cause of "key missing" failures.
        key_lines = []
        for k, sch in writes_schema.items():
            t = sch.get("type", "any") if isinstance(sch, dict) else "any"
            key_lines.append(f'  - "{k}"（类型 {t}）')
        keys_block = "\n".join(key_lines)
        skeleton = "{\n" + ",\n".join(f'  "{k}": ...' for k in writes_schema) + "\n}"
        capped_source = _cap_text(source_text, _SOURCE_CHAR_CAP)
        base_prompt = (
            "你是结构化数据抽取器。下面的「步骤输出」是某个工作流步骤执行后的结果文本。\n"
            "请从中抽取信息并构造一个 JSON 对象，严格遵守：\n"
            "1. 输出对象的顶层必须恰好包含以下字段，键名完全一致，不得改名、不得增删：\n"
            f"{keys_block}\n"
            "2. 每个字段的值须符合其类型；从步骤输出中找到对应内容，原样或整理后填入。\n"
            "3. 只输出这一个 JSON 对象，不要任何解释、注释、代码围栏。\n"
            "4. 仅当步骤输出中确实完全没有某字段的内容时才输出 null。\n\n"
            f"完整 Schema：{schema_str}\n\n"
            f"输出结构示例：\n{skeleton}\n\n"
            f"步骤输出：\n{capped_source}"
        )
        prompt = base_prompt
        last_err = ""
        validated = None
        for attempt in range(2):
            if attempt > 0:
                prompt = base_prompt + (
                    "\n\n[上一次输出有误：" + last_err + "。请重新从步骤输出中抽取，"
                    "确保顶层恰好包含上述全部字段且值非 null（除非确无内容），"
                    "只输出修正后的 JSON 对象。]"
                )
            resp = await llm_invoke_with_timeout(m.ainvoke([HumanMessage(content=prompt)]))
            # Per-call usage telemetry (source=workflow). The extraction model
            # may differ from the run model — attribute the configured name.
            ah.record_model_usage(
                resp, run_ctx.get("usage_attr"),
                model_override=getattr(m, "model", None) or getattr(m, "model_name", None),
            )
            raw = text_of_content(resp.content)
            try:
                parsed = _extract_json_from_text(raw)
            except Exception as e:
                last_err = f"JSON 解析失败：{e}"
                continue
            validated, errs = _validate_writes(parsed, writes_schema)
            if not errs:
                break
            last_err = "; ".join(errs)
            validated = None

        if validated is None:
            emit({"run_id": run_ctx["run_id"], "node_id": node_id,
                  "kind": "error",
                  "error": f"抽取失败（来源步骤 {source_id}）：{last_err}",
                  "traceback": None})
            raise RuntimeError(f"ExtractNode '{node_id}' failed: {last_err}")

        return _commit(validated, "llm")

    @classmethod
    def add_edges(cls, g, node: dict, d: dict) -> None:
        from langgraph.graph import END

        nid = node["id"]
        # Loop-body source: the compiler records back_to=<loop head>; the
        # back-edge is wired here (LoopNode skips its own back-edge for bodies
        # that gained an extract node).
        if node.get("back_to"):
            g.add_edge(nid, node["back_to"])
            return
        nxt = cls._outgoing(d, nid)
        g.add_edge(nid, nxt or END)

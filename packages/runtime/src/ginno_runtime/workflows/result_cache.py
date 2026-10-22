"""Cross-run step result cache (odw-comparison P1-①, 2026-10).

Prefix-replay model (mirrors open-dynamic-workflow's resume): when a workflow
re-runs (fresh run / retry / rerun_from), every pure-LLM node compares a
content-addressed fingerprint against a per-workflow index; a hit replays the
recorded ``{text, writes}`` instead of calling the model, and the first miss
breaks the prefix — every later node executes live. Edit one step and re-run:
its upstream replays for free, only the diverged suffix spends tokens.

Fingerprint = (schema version, node id, node type, rendered prompt, canonical
writes schema, agent, skills, output key, provider, model, extract_model,
timeout_s, on_error) — the rendered prompt already internalizes upstream
context / loop items / human patches, so no full system prompt is needed
(and dsl_version deliberately stays OUT: an edit that changes nothing a node
consumes should keep hitting, which is the whole point of content addressing).

Safety boundary — only pure-LLM nodes cache:
* ``llm`` nodes never touch tools → always eligible.
* ``agent``/``step`` nodes only when their turn used NO tool calls (a step
  that read files/surfed had side-effect-dependent output we cannot replay).
* ``python``/``browser``/``human``/``branch``/``loop``/``extract`` never
  cache (extract re-derives from the cached ``results`` text via the
  WRITE_JSON fast path, so it costs no LLM on replay).
* Parallel-loop bodies cache PER ITEM through the gather adapter's existing
  ``parallel_progress`` skip machinery — a hit is just a progress entry
  restored before the batch starts.

resume / retry_from_checkpoint never enable this cache: their LangGraph
checkpoint is the same-run authority with strictly stronger semantics.

Storage: ``~/.ginno/workflow_cache/<workflow_id>.json`` — one index file per
workflow definition, merge-on-write with atomic replace, capped at 512 entries
(oldest evicted) with a 14-day TTL. Entries are plain LLM output, same trust
domain as run JSON / events.jsonl.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from . import dsl as wf_dsl
from . import expr as wf_expr
from .. import paths
from .. import world_state

# Bump when the fingerprint anchor set changes — invalidates every entry.
FP_VERSION = 2  # v2: persona digest joins the fingerprint (odw follow-up 2026-10-23)

# Only these node types participate in node-level caching. ``step`` is the
# authoring alias of ``agent`` (registry canonical() maps it); the DSL dict
# keeps whatever the author wrote, so both spellings must pass.
CACHEABLE_TYPES = {"llm", "agent", "step"}

MAX_ENTRIES = 512
TTL_SECONDS = 14 * 24 * 3600

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]+$")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _index_path(workflow_id: str) -> Any:
    # Workflow ids are store-validated, but the cache also gets ids from run
    # records — a hostile/odd id must never escape the cache dir.
    name = workflow_id if _SAFE_ID.match(workflow_id or "") else _sha(workflow_id or "?")
    return paths.workflow_cache_dir() / f"{name}.json"


def _render_prompt(node: dict, render_ctx: dict) -> str:
    if node.get("type") == "llm":
        return wf_expr.render(node.get("prompt") or "", render_ctx)
    return wf_expr.render(node.get("goal") or node.get("title") or "", render_ctx)


def fingerprint(node: dict, render_ctx: dict, cctx: dict, item: Any = None) -> str | None:
    """Content-addressed identity of one node execution (or one loop item
    when ``item`` is given). Returns None for non-cacheable node types."""
    if node.get("type") not in CACHEABLE_TYPES:
        return None
    ua = (cctx.get("run_ctx") or {}).get("usage_attr") or {}
    fp: dict[str, Any] = {
        "v": FP_VERSION,
        "node_id": node.get("id"),
        "node_type": node.get("type"),
        "prompt": _render_prompt(node, render_ctx),
        "writes": node.get("writes") or {},
        "agent": node.get("agent") or "",
        "skills": node.get("skills") or [],
        "output": node.get("output") or "",
        "provider": ua.get("provider") or "",
        "model": ua.get("model") or "",
        "extract_model": node.get("extract_model") or "",
        "timeout_s": node.get("timeout_s"),
        "on_error": node.get("on_error") or "stop",
        # 指纹收紧(odw follow-up):persona 决定 agent step 的系统提示——
        # 同 prompt 不同 persona 结果不同,不进指纹会拿旧 persona 的缓存
        # 结果冒充新答案。取 persona 正文的 sha 前 16 位(不落全文)。
        "persona": _persona_hash(cctx, node),
    }
    if item is not None:
        fp["item"] = _canonical(item)
    return _sha(_canonical(fp))


def _persona_hash(cctx: dict, node: dict) -> str:
    """The executing persona's content digest (16 hex), or "" for default.

    persona 解析复用 agent_node 的选取逻辑太重(会 import 整条 agent 线),
    这里按 name 从 agents registry 直查;查不到(默认 persona/循环无 agent)
    返回空串——指纹语义保持「无 persona 变更」。"""
    name = node.get("agent") or ""
    if not name:
        return ""
    try:
        from ..agents import registry as agents_reg

        p = agents_reg.get(name)
        body = (p or {}).get("body") or (p or {}).get("prompt") or ""
        return _sha(body)[:16] if body else ""
    except Exception:  # noqa: BLE001 — registry 缺失/循环:不阻断缓存
        return ""


def _in_gather_mode(node: dict, cctx: dict, state: dict) -> bool:
    """Same detection as AgentNode.execute's gather dispatch: the body of a
    parallel loop whose head already published the item table."""
    owner = wf_dsl.parallel_loop_owner(cctx.get("dsl") or {}, node["id"])
    return owner is not None and (
        (state.get("loop_iters") or {}).get(owner["id"]) or {}
    ).get("parallel")


class RunResultCache:
    """Per-run handle over one workflow's shared result index.

    ``broken`` is the prefix latch: the first node-level miss sets it and
    every later lookup returns None (all live). It does NOT stop recording —
    entries are content-addressed, so the live suffix refreshes the index and
    the NEXT retry replays a longer prefix (ODW's increasing-return property).
    """

    def __init__(
        self, workflow_id: str, *, run_id: str = "", dsl_version: Any = None,
        force_live_node: str | None = None,
    ):
        self.workflow_id = workflow_id
        self._run_id = run_id
        self._dsl_version = dsl_version
        self._path = _index_path(workflow_id)
        self._entries: dict[str, dict] | None = None
        self.broken = False
        # rerun_from's target node is an EXPLICIT user request to re-execute —
        # replaying it would turn the rerun into a no-op. It skips lookup
        # without tripping the prefix latch (downstream still replays when the
        # re-executed output is unchanged, which is correct: same input, same
        # fingerprint, same result).
        self.force_live_node = force_live_node

    # ---------------- storage ---------------- #

    def _load(self) -> dict[str, dict]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict] = {}
        try:
            raw = json.loads(self._path.read_text() or "{}")
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                cutoff = time.time() - TTL_SECONDS
                for fp, ent in raw["entries"].items():
                    if isinstance(ent, dict) and isinstance(ent.get("ts"), (int, float)) and ent["ts"] >= cutoff:
                        entries[fp] = ent
        except Exception:
            # Missing or corrupt index → start empty (rebuilt on next flush).
            entries = {}
        self._entries = entries
        return entries

    def _flush(self) -> None:
        """Merge-on-write: re-read the disk state, overlay this run's entries
        (ours wins — freshest ts for the same fingerprint), trim, replace
        atomically. Two concurrent runs of one workflow may each lose the
        other's newest entry in the merge window — that only degrades to a
        future miss, never to a wrong hit."""
        if self._entries is None:
            return
        try:
            merged: dict[str, dict] = {}
            try:
                raw = json.loads(self._path.read_text() or "{}")
                if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                    merged = {k: v for k, v in raw["entries"].items() if isinstance(v, dict)}
            except Exception:
                merged = {}
            merged.update(self._entries)
            if len(merged) > MAX_ENTRIES:
                keep = sorted(merged.items(), key=lambda kv: kv[1].get("ts") or 0, reverse=True)
                merged = dict(keep[:MAX_ENTRIES])
            self._entries = dict(merged)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "entries": merged}, ensure_ascii=False))
            os.replace(tmp, self._path)
        except Exception:
            # Best-effort persistence: a failed flush must never fail the run.
            pass

    # ---------------- node-level (sequential path) ---------------- #

    def lookup(self, node: dict, state: dict, eff: Any, cctx: dict) -> dict | None:
        """Pre-execution check in BaseNode.make_node's wrapper. Returns a
        cache entry ``{"text", "writes"}`` or None; a miss trips the prefix
        latch."""
        if self.broken or node.get("type") not in CACHEABLE_TYPES:
            return None
        if node.get("id") == self.force_live_node:
            return None
        if _in_gather_mode(node, cctx, state):
            # Per-item caching is the gather adapter's job (lookup_item).
            return None
        context = dict(state.get("context") or {})
        if node.get("type") == "llm":
            render_ctx = {**context, **(eff or {})}
        else:
            render_ctx = {**context, **dict(state.get("loop_vars") or {}), **(eff or {})}
        fp = fingerprint(node, render_ctx, cctx)
        if fp is None:
            return None
        ent = self._load().get(fp)
        if ent is None:
            self.broken = True
            return None
        return {"text": str(ent.get("text") or ""), "writes": dict(ent.get("writes") or {})}

    def maybe_record(self, node: dict, state: dict, eff: Any, cctx: dict, update: dict) -> None:
        """Post-execution record in the wrapper. Eligibility: cacheable type,
        not gather mode, successful (update is not None — the soft-fail path
        never reaches here), and no tool_call events (pure LLM only)."""
        if node.get("type") not in CACHEABLE_TYPES:
            return
        if _in_gather_mode(node, cctx, state):
            return
        events = update.get("events") or []
        if any(e.get("kind") == "tool_call" for e in events if isinstance(e, dict)):
            return
        context = dict(state.get("context") or {})
        if node.get("type") == "llm":
            render_ctx = {**context, **(eff or {})}
            text = str((update.get("__output__") or {}).get("text") or "")
            writes: dict = {}
        else:
            render_ctx = {**context, **dict(state.get("loop_vars") or {}), **(eff or {})}
            text = str((update.get("results") or {}).get(node["id"]) or "")
            writes = dict(update.get("__output__") or {})
        if not text:
            return
        fp = fingerprint(node, render_ctx, cctx)
        if fp is None:
            return
        self._load()[fp] = {
            "node_id": node.get("id"),
            "node_type": node.get("type"),
            "text": text,
            "writes": writes,
            "run_id": self._run_id,
            "dsl_version": self._dsl_version,
            "ts": time.time(),
        }
        self._flush()

    # ---------------- per-item (parallel gather path) ---------------- #

    def lookup_item(self, node: dict, render_ctx: dict, cctx: dict, item: Any) -> dict | None:
        """Per-item check in the gather adapter (before the batch starts). A
        miss does NOT trip the prefix latch — items are independent."""
        if node.get("type") not in CACHEABLE_TYPES:
            return None
        fp = fingerprint(node, render_ctx, cctx, item=item)
        if fp is None:
            return None
        ent = self._load().get(fp)
        if ent is None:
            return None
        return {"text": str(ent.get("text") or ""), "writes": dict(ent.get("writes") or {})}

    def record_item(
        self, node: dict, render_ctx: dict, cctx: dict, item: Any, *, text: str, vals: dict
    ) -> None:
        if node.get("type") not in CACHEABLE_TYPES or not text:
            return
        fp = fingerprint(node, render_ctx, cctx, item=item)
        if fp is None:
            return
        self._load()[fp] = {
            "node_id": node.get("id"),
            "node_type": node.get("type"),
            "text": text,
            "writes": dict(vals or {}),
            "run_id": self._run_id,
            "dsl_version": self._dsl_version,
            "ts": time.time(),
        }
        self._flush()

    # ---------------- entry-point helper ---------------- #

    @staticmethod
    def drop(workflow_id: str) -> None:
        """Best-effort index removal when the workflow definition is deleted."""
        try:
            _index_path(workflow_id).unlink(missing_ok=True)
        except Exception:
            pass


def build_run_cache(run: dict, workflow_id: str) -> RunResultCache | None:
    """Gate for the API drivers: the global settings flag (env-overridable)
    and the per-run ``cache_disabled`` opt-out (create-run ``no_cache``)."""
    if not world_state.workflow_result_cache_enabled():
        return None
    if isinstance(run, dict) and run.get("cache_disabled"):
        return None
    return RunResultCache(
        workflow_id, run_id=str((run or {}).get("id") or ""), dsl_version=(run or {}).get("dsl_version")
    )


def synth_cached_update(node: dict, cctx: dict, state: dict, hit: dict) -> dict:
    """Build the state update a cache hit contributes — shape-identical to
    the node's own execute() return so BaseNode._post / _with_patch and the
    extract fast path work unchanged. Events mirror each type's real ordering
    (agent: enter→exit→context_write; llm: enter→context_write→exit) and
    carry ``cached`` / ``method:"cache"`` markers; usage is zero because no
    model call happens (and record_model_usage is never invoked)."""
    run_ctx = cctx["run_ctx"]
    node_id = node["id"]
    context = dict(state.get("context") or {})
    events: list = []

    def emit(ev):
        ev.setdefault("ts", time.time())
        events.append(ev)
        run_ctx["events"].append(ev)

    zero = {"input_tokens": 0, "output_tokens": 0}
    text = str(hit.get("text") or "")
    # 溯源(odw follow-up):命中来自哪次 run(UI badge 的 title 与 inspector
    # 直接可见);cached 键保持 bool(前端/更新链按 truthy 消费),来源走
    # 并行的 from_run 键。
    from_run = str(hit.get("run_id") or "") or None

    if node.get("type") == "llm":
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter",
              "node_type": "llm", "cached": True,
              **({"from_run": from_run} if from_run else {})})
        out = {"text": text}
        update: dict[str, Any] = {"events": events, "__output__": out}
        key = node.get("output")
        if key:
            update["context"] = {**context, key: text}
            emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "context_write",
                  "keys": [key], "method": "cache"})
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit",
              "status": "done", "usage": zero, "cached": True, "output": text,
              **({"from_run": from_run} if from_run else {})})
        return update

    # agent / step
    emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_enter",
          "node_type": "step", "cached": True,
          **({"from_run": from_run} if from_run else {})})
    writes = dict(hit.get("writes") or {})
    meta = dict(state.get("context_meta") or {})
    for k in writes:
        meta[k] = f"step:{node_id}"
    emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "node_exit",
          "status": "done", "usage": zero, "cached": True, "output": text,
              **({"from_run": from_run} if from_run else {})})
    if writes:
        emit({"run_id": run_ctx["run_id"], "node_id": node_id, "kind": "context_write",
              "keys": list(writes.keys()), "method": "cache"})
    return {
        "context": {**context, **writes},
        "context_meta": meta,
        "results": {**state.get("results", {}), node_id: text},
        "events": events,
        "__output__": writes,
    }

"""DSL synthesis from a session trace (split out of ``api/workflows.py``):
the synthesizer prompt, the ≤3-attempt self-correcting loop, the background
driver + task registry behind ``summarize-from-session``, and the trace
windowing/formatting helpers shared with the trace-preview endpoint.

Import note: this module is imported at the BOTTOM of ``api/workflows.py`` and
must not be imported directly. Every dependency that used to be a module global
of ``api/workflows.py`` (_log, _push_session_event, wf_dsl, text_of_content)
is resolved through ``_base`` AT CALL TIME, so monkeypatching an attribute on
``ginno_runtime.api.workflows`` keeps reaching these code paths unchanged."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import HTTPException

from . import workflows as _base

# ---- P6: synthesize a workflow DSL draft from a session's conversation ----
# Bump on any change to the prompt above so synthesis cases can be grouped by
# the prompt that produced them (quality-plan §3.2 A/B).
# synth-5: doctor dataflow findings (loop.over.no_source et al.) are fed back
# into the retry loop alongside validate_dsl errors, and the node-type catalog
# is rendered from the single-source contracts module.
SYNTH_PROMPT_VERSION = "synth-5"
# The node-type section is rendered at call time from workflows/contracts.py
# (single source of truth: registry schemas + structural rules) so the prompt
# can no longer drift from the engine's real node surface. {{NODE_CATALOG}}
# is the injection point; see _synthesize_prompt().
_SYNTHESIZE_PROMPT_TEMPLATE = (
    "You are a workflow synthesizer. Given a conversation trace between a user and "
    "an agent (including tool calls), produce a reusable workflow DSL that captures "
    "the repeatable process as a directed graph.\n\n"
    "DSL object shape:\n"
    '{\n  "name": "<short imperative name>",\n  "description": "<one line>",\n'
    '  "entry": "<first node id>",\n'
    '  "context": {"schema": {"type":"object","properties":{...}}, "initial": {...}},\n'
    '  "nodes": [ {"id","type","agent","goal", ...} ],\n'
    '  "edges": [ {"from","to"} ]\n}\n\n'
    "Node contract (every type the engine accepts, with fields and rules):\n"
    "{{NODE_CATALOG}}\n\n"
    "Rules:\n"
    "- `entry` MUST be an existing node id; every edge endpoint MUST exist.\n"
    "- Put any per-run inputs the conversation revealed into context.schema + context.initial.\n"
    "- Default to a simple linear step chain; only add branch/loop when the trace clearly shows conditionals or repetition.\n"
    "- Mechanical fetch/normalize/compute steps: PREFER the deterministic `python` "
    "node (whitelisted entry, no LLM) over an agent step.\n"
    "- Any per-run placeholder you use in goal/prompt fields ({{variable_name}}) MUST also appear "
    'in context.schema.properties with type:"string" and in context.initial as "" '
    "so the caller can fill them before running.\n"
    # Dataflow contract (master-plan §2.2.9): steps that hand data to later steps
    # declare `writes`; the engine injects an implicit extractor that produces
    # strict JSON. The model must NOT be told to emit WRITE_JSON in its goal.
    "- When a step produces data a LATER step (or a loop) consumes, declare a "
    '`writes` field on that step mapping context keys to JSON Schema. Lists of '
    'items: {"type":"array","items":{"type":"object"}}. Single text: {"type":"string"}. '
    "The key name must match the {{context.key}} reference used downstream. "
    "Example: {\"id\":\"search\",\"type\":\"step\",\"agent\":\"research\","
    "\"goal\":\"Select the top stocks.\",\"writes\":{\"stocks\":{\"type\":\"array\",\"items\":{\"type\":\"object\"}}}}. "
    "A loop over such a list reads over:\"context.stocks\".\n"
    "- Do NOT put WRITE_JSON instructions in goals; the engine extracts structured "
    "output automatically from declared writes.\n\n"
    "Reply with ONLY the JSON object, no prose, no markdown fences."
)


def _synthesize_prompt() -> str:
    """Assemble the synthesizer prompt with the live node catalog (P1b).

    Lazy on purpose: importing contracts touches the nodes registry, which
    must not happen at server-module import time (circular via agents)."""
    from ..workflows import contracts as wf_contracts

    return _SYNTHESIZE_PROMPT_TEMPLATE.replace(
        "{{NODE_CATALOG}}", wf_contracts.render_catalog()
    )


def _resolve_trace_window(
    messages: list, data: dict
) -> tuple[list, dict | None]:
    """Resolve the ``last_n`` / ``range`` selection of a summarize request into
    (sliced messages, descriptor recorded on the synthesis case).

    方案B 阶段4: ``range`` = ``{start?, end?}`` — INCLUSIVE indices into the
    session's checkpoint message list (the same indexing
    ``/api/workflows/summarize-trace/{sid}`` previews). ``start`` clamps to 0,
    ``end`` clamps to the last index. Back-compat: ``last_n`` (tail window)
    wins when both are given. Raises 400 when the selection is empty (no
    messages at all, or a range that clamps past itself)."""
    last_n = data.get("last_n")
    last_n = last_n if isinstance(last_n, int) and not isinstance(last_n, bool) and last_n > 0 else None
    if last_n:
        return messages[-last_n:], {"last_n": last_n}
    rng = data.get("range")
    if isinstance(rng, dict):
        n = len(messages)
        start = rng.get("start")
        end = rng.get("end")
        lo = max(0, start) if isinstance(start, int) and not isinstance(start, bool) else 0
        hi = min(n - 1, end) if isinstance(end, int) and not isinstance(end, bool) else n - 1
        if lo > hi:
            raise HTTPException(
                status_code=400,
                detail=f"range selects no messages (session has {n}; got start={start}, end={end})",
            )
        return messages[lo : hi + 1], {"start": lo, "end": hi}
    return messages, None


def _trace_text(messages, last_n: int | None = None) -> str:
    """Compact readable trace of a session for the synthesizer.

    Truncation strategy (workflow-ux-redesign S4a): file-read tool outputs are
    collapsed to their first lines (the full content never helps synthesis),
    other long outputs keep head + tail (endings carry the conclusion), plain
    messages keep their first 500 chars. ``last_n`` limits the trace to the
    most recent N messages (S5 range control). Callers pass an ALREADY-sliced
    message list for the 阶段4 ``range`` selection — see
    ``_resolve_trace_window``."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    FILE_READ_TOOLS = {"read_file", "Read", "cat", "head", "tail", "view_file"}
    msgs = messages or []
    if last_n and last_n > 0:
        msgs = msgs[-last_n:]
    lines: list[str] = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            c = _base.text_of_content(m.content)
            lines.append(f"USER: {c[:500]}")
        elif isinstance(m, AIMessage):
            c = _base.text_of_content(m.content)
            if c.strip():
                lines.append(f"AGENT: {c[:500]}")
            for tc in getattr(m, "tool_calls", None) or []:
                lines.append(f"  -> tool {tc.get('name')}({json.dumps(tc.get('args') or {}, ensure_ascii=False)[:200]})")
        elif isinstance(m, ToolMessage):
            c = m.content if isinstance(m.content, str) else str(m.content)
            name = getattr(m, "name", "tool") or "tool"
            if name in FILE_READ_TOOLS:
                preview = "\n".join(c.splitlines()[:3])[:200]
                lines.append(f"  <= {name}: {preview}… [file content elided]")
            elif len(c) > 400:
                lines.append(f"  <= {name}: {c[:200]}…[…]…{c[-100:]}")
            else:
                lines.append(f"  <= {name}: {c[:200]}")
    return "\n".join(lines)


def _extract_json_obj(text: str) -> dict | None:
    import re

    text = (text or "").strip()
    # strip markdown fences if the model added them despite instructions
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    # fallback: first balanced {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            v = json.loads(text[start : end + 1])
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# In-process synthesis task registry (mirrors _WF_RUN_TASKS for workflow runs;
# separate because the run registry's done-callback is run-specific). Sole
# source of "running" for synthesis cases: after a restart it is empty, so
# cases left without output.json render as 未完成 — no reconciliation needed.
_SYNTH_TASKS: dict[str, asyncio.Task] = {}


async def _run_synthesis(
    trace: str,
    model,
    case_dir,
    usage_attr: dict | None = None,
    on_attempt=None,
) -> dict:
    """The ≤3-attempt self-correcting synthesis loop (shared by the live
    endpoint and offline replay). Returns a result dict::

        {"ok", "dsl", "raw", "errors", "fail_stage", "attempts_used", "total_ms"}

    Records each attempt onto ``case_dir`` when provided (quality-plan §3.1).
    Each model call is also logged to the global usage store (source=workflow,
    usage-stats-design §3.6) when ``usage_attr`` carries the attribution.
    ``on_attempt`` (optional async callback) is invoked after each recorded
    attempt with ``{attempt, parse, validate_errors, latency_ms}`` (no raw —
    the case-detail API provides that); replay leaves it None.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from ..workflows import doctor as wf_doctor
    from ..workflows import synthesis as wf_synth
    from ..workflows.nodes import agent_helpers as ah

    extra_hint = ""
    dsl = None
    raw = ""
    errs: list[str] = ["no attempt made"]
    doc_errs: list[str] = []  # last attempt's doctor messages (report fallback)
    fail_stage = None
    doctor_warnings: list[dict] = []
    t_start = time.time()
    attempt = 0
    for attempt in range(3):
        t0 = time.time()
        resp = await model.ainvoke(
            [SystemMessage(content=_synthesize_prompt()), HumanMessage(content=trace + extra_hint)]
        )
        ah.record_model_usage(resp, usage_attr)
        latency_ms = int((time.time() - t0) * 1000)
        raw = _base.text_of_content(resp.content)
        dsl = _extract_json_obj(raw)
        if not isinstance(dsl, dict):
            extra_hint = (
                "\n\n[Previous attempt error: the reply was not a JSON object. "
                "Reply ONLY with a single {...} JSON object.]"
            )
            errs = ["model did not return a JSON DSL object"]
            fail_stage = "format.not_json"
            wf_synth.record_attempt(case_dir, attempt=attempt + 1, latency_ms=latency_ms,
                                    raw=raw, parse="not_json", validate_errors=[],
                                    hint_fed_back=extra_hint)
            if on_attempt:
                await on_attempt({"attempt": attempt + 1, "parse": "not_json",
                                  "validate_errors": [], "latency_ms": latency_ms})
            continue
        dsl = _base.wf_dsl.normalize_dsl(dsl)
        errs = _base.wf_dsl.validate_dsl(dsl)
        # Dataflow lint (stability plan P1a): validate passes structural checks
        # but not dataflow ones (loop.over without a source, dangling
        # {{context.x}} refs) — the doctor catches exactly the incident class,
        # so its errors join the self-correction loop; warnings only surface.
        doc_errs: list[str] = []
        doc_rules: list[str] = []
        doctor_warnings = []
        if not errs:
            dr = wf_doctor.run_doctor(dsl)
            doctor_warnings = list(dr.get("warnings") or [])
            for e in dr.get("errors") or []:
                doc_rules.append(e.get("rule") or "unknown")
                doc_errs.append(f"{e.get('rule')}: {e.get('message')}")
        wf_synth.record_attempt(
            case_dir, attempt=attempt + 1, latency_ms=latency_ms, raw=raw, parse="ok",
            validate_errors=list(errs), doctor_errors=list(doc_errs),
            hint_fed_back=None if not (errs or doc_errs)
            else "[DSL errors: " + "; ".join(errs or doc_errs) + "]",
        )
        if on_attempt:
            await on_attempt({"attempt": attempt + 1, "parse": "ok",
                              "validate_errors": list(errs),
                              "doctor_errors": list(doc_errs),
                              "latency_ms": latency_ms})
        if not errs and not doc_errs:
            fail_stage = None
            break
        if errs:
            fail_stage = "schema." + (errs[0].split(":")[0][:40] if errs else "unknown")
            extra_hint = (
                "\n\n[Previous attempt DSL errors: " + "; ".join(errs) +
                ". Fix them and reply ONLY with the corrected JSON object.]"
            )
        else:
            fail_stage = "doctor." + (doc_rules[0][:40] if doc_rules else "unknown")
            extra_hint = (
                "\n\n[Previous attempt dataflow errors: " + "; ".join(doc_errs) +
                ". Fix the dataflow — declare the missing `writes` on the producing "
                "step or add the key to context.initial — and reply ONLY with the "
                "corrected JSON object.]"
            )
    return {
        # fail_stage is None only on a clean break (no validate AND no doctor
        # errors) — a last attempt that died on dataflow errors is not ok even
        # though ``errs`` (validate) is empty.
        "ok": isinstance(dsl, dict) and fail_stage is None,
        "dsl": dsl if isinstance(dsl, dict) else None,
        "raw": raw,
        # Validate errors take precedence; when the DSL is schema-clean but
        # dataflow-dirty, report the doctor messages instead of an empty list.
        "errors": errs or doc_errs,
        "fail_stage": fail_stage,
        "attempts_used": max(1, attempt + 1),
        "total_ms": int((time.time() - t_start) * 1000),
        "doctor_warnings": doctor_warnings,
    }


def _synth_error_text(result: dict) -> str:
    """User-facing failure text for a failed synthesis result (carried by the
    background finished-event and readable in the panel/chat error copy)."""
    if result.get("dsl") is None:
        return "model did not return a JSON DSL object"
    return "synthesized DSL invalid: " + "; ".join(result.get("errors") or [])


async def _run_synthesis_bg(
    session_id: str,
    synthesis_id: str,
    case_dir: Path,
    trace: str,
    model,
    usage_attr: dict | None,
) -> None:
    """Background driver: runs the ≤3-attempt loop, records the case outcome,
    and pushes live ``synthesis.event`` WS events (started/attempt/finished)
    so the right-dock 总结 panel and the waiting chat UI update in real time.

    Invariant: when this task ends normally the case always has output.json
    (written here, or healed by ``_spawn_synth_task``'s done-callback)."""
    from ..workflows import synthesis as wf_synth

    _base._log.info("synthesis_start synthesis=%s session=%s", synthesis_id, session_id)
    await _base._push_session_event(session_id, "synthesis.event",
                             {"synthesis_id": synthesis_id, "kind": "started"})
    try:
        async def _on_attempt(info: dict) -> None:
            _base._log.info(
                "synthesis_attempt synthesis=%s attempt=%s parse=%s errors=%d",
                synthesis_id, info.get("attempt"), info.get("parse"),
                len(info.get("validate_errors") or []),
            )
            await _base._push_session_event(
                session_id, "synthesis.event",
                {"synthesis_id": synthesis_id, "kind": "attempt", **info},
            )

        result = await _run_synthesis(trace, model, case_dir, usage_attr=usage_attr,
                                      on_attempt=_on_attempt)
        wf_synth.finish_case(
            case_dir,
            status="ok" if result["ok"] else "failed",
            dsl=result["dsl"],
            fail_stage=result["fail_stage"],
            total_latency_ms=result["total_ms"],
            attempts_used=result["attempts_used"],
        )
        _base._log.info(
            "synthesis_finish synthesis=%s ok=%s fail_stage=%s attempts=%s ms=%s",
            synthesis_id, result["ok"], result["fail_stage"],
            result["attempts_used"], result["total_ms"],
        )
        await _base._push_session_event(session_id, "synthesis.event", {
            "synthesis_id": synthesis_id,
            "kind": "finished",
            "ok": result["ok"],
            "fail_stage": result["fail_stage"],
            "attempts_used": result["attempts_used"],
            "total_ms": result["total_ms"],
            "error": None if result["ok"] else _synth_error_text(result),
            # Stability plan P1a: advisory dataflow warnings ride the finished
            # frame so the UI can surface them without blocking the draft.
            "doctor_warnings": result.get("doctor_warnings") or [],
        })
    except asyncio.CancelledError:
        raise
    except Exception:
        _base._log.exception("synthesis_failed synthesis=%s session=%s", synthesis_id, session_id)
        wf_synth.finish_case(case_dir, status="failed", dsl=None,
                             fail_stage="internal.error", total_latency_ms=0, attempts_used=0)
        await _base._push_session_event(session_id, "synthesis.event", {
            "synthesis_id": synthesis_id, "kind": "finished", "ok": False,
            "fail_stage": "internal.error", "attempts_used": 0, "total_ms": 0,
            "error": "internal error during synthesis",
        })


def _spawn_synth_task(synthesis_id: str, case_dir: Path, coro) -> asyncio.Task:
    """Register a synthesis background task with the permanent safety net
    (mirrors ``_spawn_run_task``): a non-cancelled task that ends while the
    case still has no output.json is healed to failed/internal.error."""
    from ..workflows import synthesis as wf_synth

    task = asyncio.create_task(coro)
    _SYNTH_TASKS[synthesis_id] = task

    def _on_done(t: asyncio.Task) -> None:
        _SYNTH_TASKS.pop(synthesis_id, None)
        if t.cancelled():
            return
        try:
            t.exception()  # consume: already logged inside the task
            if not (case_dir / "output.json").exists():
                wf_synth.finish_case(case_dir, status="failed", dsl=None,
                                     fail_stage="internal.error",
                                     total_latency_ms=0, attempts_used=0)
                _base._log.error("synthesis_guard_healed synthesis=%s (no output.json)",
                           synthesis_id)
        except Exception:
            _base._log.exception("synthesis_guard_failed synthesis=%s", synthesis_id)

    task.add_done_callback(_on_done)
    return task


async def _shutdown_synth_tasks() -> None:
    """Graceful shutdown: cancel live synthesis tasks. Deliberately does NOT
    write output.json for them — the leftover cases render as 未完成 after the
    restart (crash semantics), the intended reconciliation-free behavior."""
    pending = [t for t in list(_SYNTH_TASKS.values()) if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=3.0,
            )
        except (TimeoutError, asyncio.CancelledError):
            pass

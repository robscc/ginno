"""Workflow run engine drivers (split out of ``api/workflows.py``): dep
building for a run, run-status persistence helpers, the event pump, and the
background drivers (run / resume / continue / rerun-from) plus the spawn /
shutdown / reconciliation plumbing around them.

Import note: this module is imported at the BOTTOM of ``api/workflows.py`` and
must not be imported directly. Every dependency that used to be a module global
of ``api/workflows.py`` (wf_store, _WF_RUN_TASKS, build_model, ...) is resolved
through ``_base`` AT CALL TIME, so monkeypatching an attribute on
``ginno_runtime.api.workflows`` keeps reaching these code paths unchanged."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from . import workflows as _base

from ..lang import t


# ---- 跨 run 全局并发闸(odw-comparison P1-③)------------------------------------
# 信号量按当前设置值惰性(重)建:设置改了下一批 run 用新值,在途 run 不受
# 影响。0 = 不限(nullcontext 直通)。
_RUN_SEMAPHORE: asyncio.Semaphore | None = None
_RUN_SEMAPHORE_N: int = 0


def _max_concurrent_runs() -> int:
    from .. import world_state

    try:
        return max(0, int(world_state.context_settings().get("workflow_max_concurrent_runs") or 0))
    except (TypeError, ValueError):
        return 0


@asynccontextmanager
async def _global_run_slot(run_id: str):
    global _RUN_SEMAPHORE, _RUN_SEMAPHORE_N
    n = _max_concurrent_runs()
    if n <= 0:
        yield
        return
    if _RUN_SEMAPHORE is None or _RUN_SEMAPHORE_N != n:
        _RUN_SEMAPHORE = asyncio.Semaphore(n)
        _RUN_SEMAPHORE_N = n
    _base._log.info("workflow_run_slot_wait run=%s limit=%s", run_id, n)
    async with _RUN_SEMAPHORE:
        yield


def _wf_mcp_tools() -> list:
    try:
        return _base.shared._mcp.all_langchain_tools() if _base.shared._mcp else []
    except Exception:
        return []


def _wf_build_deps(run_id: str, workflow_id: str, pinned_version: int | None = None):
    """Resolve (wf, dsl, model, tools, fork_id, usage_attr) for a run by forking
    its source agent. Returns a 6-tuple of None when the workflow def is missing.

    ``usage_attr`` carries the attribution (provider/model/agent/run) the LLM
    nodes stamp on their per-call usage records (source=workflow); the driver
    adds ``session_id`` (present_in) before handing it to the engine.

    ``pinned_version``: CONTINUING a run (resume / retry-from-checkpoint /
    rerun_from) must compile the version the run STARTED with — resuming on the
    CURRENT def after an edit executes a different graph under the same run id
    (2026-09-25: a v5 edit made run bb1b394c re-execute half its nodes on the
    new topology and garble the step table). Raises ValueError when the pinned
    version no longer exists (driver marks the run failed with that reason).

    NOTE: fork_agent/build_model can RAISE (unknown agent, disabled/keyless
    provider) — callers must invoke this inside a try/except that marks the run
    failed, or the run is stranded in "running" forever.
    """
    wf = _base.wf_store.get_def(workflow_id)
    if not wf:
        return None, None, None, None, None, None
    if pinned_version is not None:
        dsl = _base.wf_storemod.get_version(workflow_id, int(pinned_version))
        if not dsl:
            raise ValueError(
                f"version v{pinned_version} of workflow '{workflow_id}' no longer "
                "exists — start a new run of the current version"
            )
    else:
        dsl = wf["dsl"]
    src_agent_id = None
    for n in dsl.get("nodes") or []:
        if n.get("agent"):
            src_agent_id = n["agent"]
            break
    src_agent_id = src_agent_id or wf.get("agent_id")
    # Fallback instead of fail when the referenced agent doesn't exist
    # (LLM-drafted DSLs invent role names — 2026-08-10 incident: the run
    # died at fork time before the first node). Same rule as AgentNode; a
    # warning event keeps the substitution visible. Only raises when NO
    # agent exists at all.
    from ..workflows.nodes import agent_helpers as ah

    resolved, agent_warn = ah.resolve_agent(src_agent_id)
    if resolved is None:
        raise ValueError(agent_warn or "no agents available")
    if agent_warn:
        _base._log.warning("workflow_run run=%s agent fallback: %s", run_id, agent_warn)
        _base.wf_events.append_event(run_id, "warning", message=agent_warn)
    fork = _base.agents_reg.fork_agent(resolved.id, f"wf-{run_id[:8]}-{resolved.id}")
    model = _base.build_model(fork.provider, fork.model or None)
    # Bind the run's file tools to a REAL workspace. With no workspace the
    # builtin tools fall back to the process cwd — for a packaged app that is
    # "/" and an agent's glob_files then walked the ENTIRE filesystem
    # (2026-09-25 incident). The Ginno home is always defined and its
    # sensitive subdirs are hard-denied by the tool layer anyway.
    tools = _base.build_all_tools(_wf_mcp_tools(), workspace=str(_base.paths.home()))
    usage_attr = {
        "provider": fork.provider or "",
        "model": fork.model or getattr(model, "model", None) or getattr(model, "model_name", "") or "",
        "agent_id": resolved.id,
        "run_id": run_id,
    }
    return wf, dsl, model, tools, fork.id, usage_attr


def _set_run_status(
    run_id: str,
    status: str,
    error: str | None = None,
    only_from: tuple[str, ...] | None = None,
    error_detail: dict | None = None,
) -> None:
    """Persist a run status transition.

    ``error`` is stored when given and cleared on ``done``; terminal statuses
    stamp ``finished``. ``error_detail`` ({"node_id", "traceback"}) is the
    structured companion for localization UIs; it follows the same lifecycle
    as ``error``. ``only_from`` guards races (e.g. a queued engine "done"
    must not overwrite a user cancel that landed first).
    """
    run = _base.wf_store.get_run(run_id)
    if not run:
        return
    if only_from is not None and run.get("status") not in only_from:
        return
    run["status"] = status
    run["updated"] = time.time()
    if error is not None:
        run["error"] = error
    if error_detail is not None:
        run["error_detail"] = error_detail
    if status == "done":
        run["error"] = None
        run["error_detail"] = None
    if status in _base.wf_storemod.TERMINAL_STATUSES and not run.get("finished"):
        run["finished"] = run["updated"]
    _base.wf_storemod._write_json(_base.wf_storemod._run_path(run_id), run)


def _touch_run(run_id: str) -> None:
    """Bump ``updated`` so the UI's stuck-detection sees activity even while a
    single long step runs (called per engine event)."""
    run = _base.wf_store.get_run(run_id)
    if run:
        run["updated"] = time.time()
        _base.wf_storemod._write_json(_base.wf_storemod._run_path(run_id), run)


def _set_run_pending_interrupt(run_id: str, payload: dict | None) -> None:
    """Stamp/clear ``pending_interrupt`` on the run JSON (workflow-ux-redesign
    P1). Lets any UI (panel, dock badge) tell WHY a run is paused — a human
    question vs. a generic interrupt — without fetching events.jsonl. The
    payload mirrors the interrupt event (kind/node/question)."""
    run = _base.wf_store.get_run(run_id)
    if not run:
        return
    run["pending_interrupt"] = payload
    run["updated"] = time.time()
    _base.wf_storemod._write_json(_base.wf_storemod._run_path(run_id), run)


def _backfill_first_run(workflow_id: str, run: dict) -> None:
    """Quality-plan §3.1: when a synthesized workflow's first run terminates,
    record the outcome on its synthesis case (L3 signal). Best-effort."""
    try:
        from ..workflows import synthesis as wf_synth

        meta = _base.wf_storemod._read_meta(workflow_id) or {}
        syn = (meta.get("synthesized_from") or {}).get("synthesis_id")
        if not syn:
            return
        outcome = wf_synth.load_case(syn) or {}
        first_run = (outcome.get("outcome") or {}).get("first_run")
        if first_run:
            return  # only record the FIRST run
        wf_synth.backfill_outcome(
            wf_synth.find_case_by_synthesis_id(syn),
            first_run={
                "run_id": run.get("id"),
                "status": run.get("status"),
                "failed_node": (run.get("error_detail") or {}).get("node_id"),
            },
        )
    except Exception:
        pass


async def _push_both(
    present_in: str | None, run_id: str, event: str, data: dict
) -> None:
    """One run event, two channels (design B P2): the presenting session's
    sockets (in-chat rendering, design A) AND the run-scoped sockets (Studio
    observer, headless watchers). Identical frame on both."""
    await _base._push_session_event(present_in, event, data)
    await _base._push_run_event(run_id, event, data)


async def _push_run_status(present_in: str | None, run_id: str, data: dict) -> None:
    """run.status frames also carry the fresh run JSON for run-scoped clients
    (the observer needs the step table, not just the status word)."""
    await _push_both(present_in, run_id, "run.status", data)
    await _base._push_run_event(run_id, "run.snapshot", {"run": _base.wf_store.get_run(run_id)})


async def _mark_run_failed(run_id: str, exc: BaseException, present_in: str | None = None) -> None:
    """The one place a run failure is persisted: error event + run.error +
    ledger + run.status push (so chat/panel show the reason immediately).

    Driver-level failures (agent fork / model+tool build) have no engine
    events, so the traceback is captured here; node_id stays None because no
    node ever started."""
    from ..workflows.engine import _trimmed_traceback

    err = f"{type(exc).__name__}: {exc}"
    tb = _trimmed_traceback(exc)
    ev = _base.wf_events.append_event(run_id, "error", error=err, traceback=tb)
    _set_run_status(
        run_id, "failed", error=err, error_detail={"node_id": None, "traceback": tb}
    )
    _base.sync_ledger.set_status(run_id, "failed", err)
    await _base._push_session_event(
        present_in, "run.status",
        {"run_id": run_id, "status": "failed", "error": err, "node_id": None},
    )
    await _base._push_run_event(run_id, "run.event", {"run_id": run_id, "payload": ev})
    await _push_run_status(present_in, run_id, {"run_id": run_id, "status": "failed", "error": err, "node_id": None})


async def _drive_run_events(run_id: str, present_in: str | None, wf: dict, agen) -> None:
    """Persist + push each engine event; keep run step status + terminal state in sync."""
    # Build the node->step map from the RUN's steps (which include injected
    # ``<id>__extract`` steps; the definition's steps do not), so extract-node
    # events update their step and the run-status recomputation accounts for them.
    _run0 = _base.wf_store.get_run(run_id) or {}
    node_to_step = {s["id"]: s["id"] for s in _run0.get("steps", [])}
    # loop node id -> body node id (to mark the body "skipped" when an
    # on_empty=skip loop completes with zero iterations; master-plan §2.1).
    loop_body: dict[str, str] = {}
    for _n in (wf.get("dsl") or {}).get("nodes") or []:
        if isinstance(_n, dict) and _n.get("type") == "loop" and _n.get("body"):
            loop_body[_n.get("id")] = _n["body"]
    last_interrupt: dict | None = None  # latest human-interrupt payload (P1)
    async for ev in agen:
        stored = _base.wf_events.append_event(run_id, ev.get("kind", ""), **{
            k: v for k, v in ev.items() if k not in ("kind", "run_id", "seq")
        })
        _touch_run(run_id)
        await _push_both(present_in, run_id, "run.event", {"run_id": run_id, "payload": stored})
        kind = ev.get("kind")
        nid = ev.get("node_id")
        if kind == "node_enter" and nid in node_to_step:
            _base.wf_store.update_step(run_id, node_to_step[nid], "running")
            # Step-status refresh on the run channel: the Studio observer
            # renders run.steps from snapshots, and mid-run there are no
            # status transitions — without this the step table stays at the
            # connect-time snapshot (all pending) while events flow around it
            # (2026-09-25: the「运行中的节点没有运行的标志」report). Deliberately
            # a run.steps frame, NOT a run.snapshot: the full snapshot carries
            # run.status, and update_step already recomputes "done" when the
            # last step lands — a snapshot here would announce done before the
            # engine's terminal event (and break snapshot ordering guarantees).
            await _base._push_run_event(
                run_id, "run.steps", {"steps": (_base.wf_store.get_run(run_id) or {}).get("steps", [])}
            )
        elif kind == "node_exit" and nid in node_to_step:
            # A cache-replayed node_exit carries the recorded text in
            # ``output`` (real executions never set it) — persist it so the
            # step table shows what was replayed, not an empty string.
            _base.wf_store.update_step(
                run_id, node_to_step[nid],
                "done" if ev.get("status") != "failed" else "failed",
                output=str(ev.get("output") or ""),
                cached=bool(ev.get("cached")),
            )
            await _base._push_run_event(
                run_id, "run.steps", {"steps": (_base.wf_store.get_run(run_id) or {}).get("steps", [])}
            )
        elif kind == "loop_skip":
            # on_empty=skip: the loop finished with zero iterations. The loop
            # node's own step lands "done" via its node_exit; its body never ran,
            # so mark the body step "skipped" (master-plan §2.1).
            body = loop_body.get(nid)
            if body and body in node_to_step:
                _base.wf_store.update_step(run_id, node_to_step[body], "skipped")
        elif kind == "interrupt":
            # A node suspended the graph (human question or manual pause,
            # workflow-ux-redesign #14) — remember the payload so the "paused"
            # transition below can stamp it on the run JSON. nature
            # distinguishes the two; HumanNode events carry none and keep the
            # historical "human" default.
            last_interrupt = {k: v for k, v in ev.items() if k not in ("run_id", "kind")}
            last_interrupt["kind"] = ev.get("nature") or "human"
            _set_run_pending_interrupt(run_id, last_interrupt)
            # HumanNode suspends before its node_enter/exit events fire, so the
            # step would stay "pending" forever — mark it running while waiting.
            if nid in node_to_step:
                _base.wf_store.update_step(run_id, node_to_step[nid], "running")
        elif kind == "resume":
            last_interrupt = None
            _set_run_pending_interrupt(run_id, None)
            # A manually paused step RE-EXECUTES after resume (checkpoint
            # rewind) — its node_exit marks it done; don't flip it here.
            if nid in node_to_step and ev.get("nature") != "manual":
                _base.wf_store.update_step(run_id, node_to_step[nid], "done")
        elif kind == "error" and ev.get("handled"):
            # Soft failure (stability plan P2, on_error="continue"): the node
            # exhausted its retries but the run GOES ON. Fail only the
            # attributed step, count a warning on the run, and never flip the
            # run status — the terminal done/failed event still decides it.
            raw_nid = ev.get("node_id")
            attr_nid = (
                raw_nid[: -len("__extract")]
                if isinstance(raw_nid, str) and raw_nid.endswith("__extract")
                else raw_nid
            )
            if attr_nid in node_to_step:
                _base.wf_store.update_step(run_id, node_to_step[attr_nid], "failed")
            _runw = _base.wf_store.get_run(run_id)
            if _runw:
                _runw["warnings"] = int(_runw.get("warnings") or 0) + 1
                _base.wf_storemod._write_json(_base.wf_storemod._run_path(run_id), _runw)
        elif kind == "done":
            _set_run_pending_interrupt(run_id, None)
            _set_run_status(run_id, "done", only_from=("running", "paused"))
            _fin = _base.wf_store.get_run(run_id)
            _warn = int((_fin or {}).get("warnings") or 0)
            await _push_run_status(
                present_in, run_id,
                {"run_id": run_id, "status": "done", **({"warnings": _warn} if _warn else {})},
            )
            if _fin:
                _backfill_first_run(wf.get("id"), _fin)
        elif kind == "paused":
            # "done" is allowed here deliberately: an end-of-graph supervisor
            # gate (<N>__sup with continue_to=None) is not a step-table row, so
            # the last real step's completion recomputes the run to "done"
            # BEFORE the gate suspends the engine. The engine's paused event is
            # authoritative over that premature done (studio e2e
            # test_studio_supervisor: a run parked at its final gate must stay
            # decidable — decide 409s on anything but "paused").
            _set_run_status(run_id, "paused", only_from=("running", "paused", "done"))
            await _push_run_status(
                present_in, run_id,
                {"run_id": run_id, "status": "paused", "pending_interrupt": last_interrupt},
            )
            # Pause transitions must reach EVERY client (the decision inbox
            # lists paused runs across recipes) — the session/run channels only
            # reach subscribers of this run.
            await _base._push_global_event("workflows.changed", {})
        elif kind == "error":
            # Mark the run failed FIRST: flipping a still-running step to
            # "failed" via update_step while the run were still "running" would
            # recompute it to "done" (all steps terminal) and the failed write
            # below would then be a no-op. With the run already "failed",
            # update_step only touches the step, not the run status.
            #
            # Extract-node attribution (master-plan §2.2.6): a failure raised by
            # a synthesized ``<src>__extract`` node is really the producing step's
            # failure — attribute error_detail.node_id to the source step so the
            # UI localizes it correctly, but keep the extract node id for detail.
            raw_nid = ev.get("node_id")
            attr_nid = raw_nid
            extract_nid = None
            if isinstance(raw_nid, str) and raw_nid.endswith("__extract"):
                extract_nid = raw_nid
                attr_nid = raw_nid[: -len("__extract")]
            _set_run_pending_interrupt(run_id, None)
            _set_run_status(
                run_id, "failed",
                error=str(ev.get("error") or ""),
                only_from=("running", "paused"),
                error_detail={
                    "node_id": attr_nid,
                    "extract_node_id": extract_nid,
                    "traceback": ev.get("traceback"),
                },
            )
            run = _base.wf_store.get_run(run_id)
            if run:
                for s in run.get("steps", []):
                    # Fail the attributed step (and any still-running step).
                    if s.get("id") == attr_nid or s.get("status") == "running":
                        _base.wf_store.update_step(run_id, s["id"], "failed")
            await _push_run_status(
                present_in, run_id,
                {
                    "run_id": run_id,
                    "status": "failed",
                    "error": str(ev.get("error") or ""),
                    "node_id": attr_nid,
                },
            )
            _backfill_first_run(wf.get("id"), _base.wf_store.get_run(run_id) or {})


def _apply_supervisor_override(run: dict | None, dsl: dict) -> dict:
    """Config layer 2 (方案B 阶段3): merge the run's ``supervisor_override``
    into the supervisor block BEFORE compiling. Deep-copies first — the stored
    version snapshot (and the def) must NEVER be mutated: resuming later must
    still see the pristine pinned DSL."""
    ov = (run or {}).get("supervisor_override")
    if not isinstance(ov, dict) or not ov:
        return dsl
    d = json.loads(json.dumps(dsl, ensure_ascii=False))
    d["supervisor"] = {**(d.get("supervisor") or {}), **ov}
    return d


async def _run_workflow_bg(run_id: str, workflow_id: str, context_override: dict | None, present_in: str | None = None) -> None:
    """Background driver: fork agent, stream the engine, persist + push events.

    Invariant: the run NEVER ends this coroutine still marked "running" — every
    failure (including dependency-building errors) lands as "failed" with the
    error persisted. CancelledError is re-raised: the cancel endpoint /
    shutdown path owns that status flip.
    """
    from ..workflows import engine as wf_engine

    fork_id = None
    # 跨 run 全局并发闸(odw P1-③):调度触发的无人值守 run 并行时全局限流
    # (``workflow_max_concurrent_runs``,0 = 不限)。闸门包住引擎驱动段——
    # run 创建/事件推送在闸外,排队只阻塞执行本身;排队期间取消照常生效
    # (await 信号量时可被 cancel)。
    async with _global_run_slot(run_id):
        await _run_workflow_bg_inner(run_id, workflow_id, context_override, present_in)


async def _run_workflow_bg_inner(run_id: str, workflow_id: str, context_override: dict | None, present_in: str | None = None) -> None:
    from ..workflows import engine as wf_engine

    fork_id = None
    try:
        run0 = _base.wf_store.get_run(run_id) or {}
        wf, dsl, model, tools, fork_id, usage_attr = _wf_build_deps(run_id, workflow_id, pinned_version=run0.get("dsl_version"))
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        dsl = _apply_supervisor_override(run0, dsl)
        # Replayable transcript session (transcript.py): every run gets one so
        # its agent steps read like scheduled agent tasks in the sessions list.
        from ..workflows import transcript as wf_transcript

        transcript_sid = await wf_transcript.begin_run_session(wf, {"id": run_id})
        if present_in:
            await _base._push_session_event(present_in, "run.bind", {"run_id": run_id, "workflow_id": workflow_id, "present_in_session_id": present_in})
        else:
            # Headless run (todo-sync et al.): there is no chat to carry run.*
            # events, and the completion push below only fires when the run
            # finishes. Announce the run at START too, so the Workflow panel
            # lists it as running immediately instead of materialising late.
            await _base._push_global_event("workflows.changed", {})
        # Cross-run result cache (odw-comparison P1-①): prefix replay for
        # fresh runs AND retries. Gated by settings + the run's own
        # cache_disabled (create-run "no_cache").
        from ..workflows import result_cache as wf_rc

        rc = wf_rc.build_run_cache(run0, workflow_id)
        agen = wf_engine.run_workflow(
            dsl, run_id=run_id, model=model, tools=tools,
            context_override=context_override,
            usage_attr={**(usage_attr or {}), "session_id": present_in},
            transcript_session=transcript_sid,
            result_cache=rc,
            # 预算钉死(odw P1-③):run 行持久化的 max_agent_calls(None=未指定,
            # 由 DSL/全局设置合成)。
            max_agent_calls=run0.get("max_agent_calls"),
        )
        await _drive_run_events(run_id, present_in, wf, agen)
        _base.sync_ledger.set_status(run_id, "ok")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _base._log.exception("workflow_run_failed run=%s workflow=%s", run_id, workflow_id)
        await _mark_run_failed(run_id, exc, present_in)
    finally:
        # The fork is a per-run scratch agent; drop it so the Agents list
        # doesn't accumulate wf-* clutter (reruns/resumes re-fork idempotently).
        if fork_id:
            try:
                _base.agents_reg.delete_agent(fork_id)
            except Exception:
                pass
        # Headless runs (todo sync et al.) have no present_in session; push
        # globally so the Workflow panel lists them without a manual refresh.
        await _base._push_global_event("workflows.changed", {})


async def _resume_workflow_bg(run_id: str, workflow_id: str, resume_value: dict, present_in: str | None = None) -> None:
    """Background driver to continue a paused run (human/supervisor decision).
    Same never-silent-running invariant as :func:`_run_workflow_bg`."""
    from ..workflows import engine as wf_engine

    fork_id = None
    try:
        run0 = _base.wf_store.get_run(run_id) or {}
        wf, dsl, model, tools, fork_id, usage_attr = _wf_build_deps(run_id, workflow_id, pinned_version=run0.get("dsl_version"))
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        dsl = _apply_supervisor_override(run0, dsl)
        _set_run_status(run_id, "running")
        # paused→running must be ANNOUNCED: the chat run card (and any run-
        # scoped observer) only learns of the flip from this push — the next
        # terminal push would otherwise leave it showing 已暂停 the whole time.
        await _push_run_status(present_in, run_id, {"run_id": run_id, "status": "running"})
        # Manual pauses (#14) carry no node-side resume event — tell the engine
        # to emit it from the run's pending_interrupt nature instead.
        _nature = ((_base.wf_store.get_run(run_id) or {}).get("pending_interrupt") or {}).get("kind")
        agen = wf_engine.resume_workflow(
            dsl, run_id=run_id, model=model, tools=tools, resume_value=resume_value,
            usage_attr={**(usage_attr or {}), "session_id": present_in},
            resume_nature="manual" if _nature == "manual" else None,
        )
        await _drive_run_events(run_id, present_in, wf, agen)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _base._log.exception("workflow_resume_failed run=%s workflow=%s", run_id, workflow_id)
        await _mark_run_failed(run_id, exc, present_in)
    finally:
        # paused-at-interrupt runs re-fork idempotently on next resume
        if fork_id:
            try:
                _base.agents_reg.delete_agent(fork_id)
            except Exception:
                pass


def _spawn_run_task(run_id: str, coro) -> asyncio.Task:
    """Register a run's background task and attach the permanent safety net.

    The done-callback guarantees the "never silently running" invariant even if
    a future code path forgets a status write: a non-cancelled task that ends
    while the on-disk run is still "running" is healed to "failed". Also prunes
    ``_WF_RUN_TASKS`` (the ``_await`` endpoint falls back to a disk read).
    """
    task = asyncio.create_task(coro)
    _base._WF_RUN_TASKS[run_id] = task

    def _on_done(t: asyncio.Task) -> None:
        _base._WF_RUN_TASKS.pop(run_id, None)
        if t.cancelled():
            return
        try:
            run = _base.wf_store.get_run(run_id)
            if run and run.get("status") == "running":
                exc = t.exception()
                if exc is not None:
                    from ..workflows.engine import _trimmed_traceback

                    err = f"{type(exc).__name__}: {exc}"
                    detail = {"node_id": None, "traceback": _trimmed_traceback(exc)}
                else:
                    err = "run task ended without emitting a terminal event"
                    detail = None
                _base.wf_events.append_event(
                    run_id, "error", error=err,
                    traceback=(detail or {}).get("traceback"),
                )
                _set_run_status(run_id, "failed", error=err, error_detail=detail)
                _base.sync_ledger.set_status(run_id, "failed", err)
                _base._log.error("workflow_run_guard_healed run=%s err=%s", run_id, err)
        except Exception:
            _base._log.exception("workflow_run_guard_failed run=%s", run_id)

    task.add_done_callback(_on_done)
    return task


def _reconcile_orphan_runs() -> None:
    """Heal runs stranded by a previous process exit (startup reconciliation).

    Called once in the lifespan startup: at that moment ``_WF_RUN_TASKS`` is
    empty and no run task can possibly be alive, so every run still marked
    "running" is by definition an orphan from a crash/quit. Each is flipped to
    the terminal ``interrupted`` status with a persisted reason + an event (which
    also (re)creates its events file). "paused" runs are left untouched — their
    checkpoint is intact and they stay resumable. Failed runs missing an inline
    ``error`` get it backfilled from their last error event so the UI can show
    the reason without parsing events.jsonl.
    """
    interrupted = 0
    backfilled = 0
    for run in _base.wf_store.list_runs():
        rid = run.get("id")
        status = run.get("status")
        if status == "running":
            reason = "sidecar restarted during run (orphaned)"
            _base.wf_events.append_event(rid, "interrupted", error=reason)
            _set_run_status(rid, "interrupted", error=reason)
            _base.sync_ledger.set_status(rid, "interrupted", reason)
            interrupted += 1
        elif status == "failed" and not run.get("error"):
            errs = _base.wf_events.read_events(rid, kind="error")
            if errs:
                last_ev = errs[-1]
                last = last_ev.get("error") or ""
                if last:
                    run["error"] = last
                    # Carry the diagnostic fields along too, so historical
                    # failed runs enjoy the same localization data.
                    if last_ev.get("traceback") or last_ev.get("node_id"):
                        run["error_detail"] = {
                            "node_id": last_ev.get("node_id"),
                            "traceback": last_ev.get("traceback"),
                        }
                    _base.wf_storemod._write_json(_base.wf_storemod._run_path(rid), run)
                    backfilled += 1
    if interrupted or backfilled:
        _base._log.info(
            "run_reconciliation interrupted=%d backfilled=%d", interrupted, backfilled
        )


async def _shutdown_run_tasks() -> None:
    """Graceful shutdown: cancel live run tasks, then mark any run still
    "running" on disk as interrupted. Runs already paused stay paused."""
    pending = [(rid, t) for rid, t in list(_base._WF_RUN_TASKS.items()) if not t.done()]
    for _, t in pending:
        t.cancel()
    if pending:
        try:
            await asyncio.wait_for(
                asyncio.gather(*(t for _, t in pending), return_exceptions=True),
                timeout=3.0,
            )
        except (TimeoutError, asyncio.CancelledError):
            pass
    for rid, _ in pending:
        run = _base.wf_store.get_run(rid)
        if run and run.get("status") == "running":
            reason = "sidecar shut down during run"
            _base.wf_events.append_event(rid, "interrupted", error=reason)
            _set_run_status(rid, "interrupted", error=reason)
            _base.sync_ledger.set_status(rid, "interrupted", reason)


def _run_checkpoint_path(run_id: str) -> Path:
    """Checkpoint artifact for a run. Engine compiles with
    ``FileCheckpointer("default")`` + ``thread_id=run_id`` (server never
    overrides the slug), so all run checkpoints land in the default project's
    sessions dir under the run id. Exact-name only — never glob (per-session
    dirs share prefixes; same rule as delete_session)."""
    return _base.paths.project_sessions_dir("default") / f"{run_id}.json"


def _remove_run_artifacts(run_id: str) -> None:
    """Delete a run's persisted files: run JSON + events JSONL + checkpoint."""
    _base.wf_store.delete_run(run_id)
    _run_checkpoint_path(run_id).unlink(missing_ok=True)


async def _continue_run_bg(run_id: str, workflow_id: str, present_in: str | None = None) -> None:
    """Background driver for retry-from-checkpoint (P2): same never-silent-
    running invariant as the other drivers; streams the graph with input=None
    on the copied checkpoint so completed steps are not re-executed."""
    from ..workflows import engine as wf_engine

    fork_id = None
    try:
        run0 = _base.wf_store.get_run(run_id) or {}
        wf, dsl, model, tools, fork_id, usage_attr = _wf_build_deps(run_id, workflow_id, pinned_version=run0.get("dsl_version"))
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        dsl = _apply_supervisor_override(run0, dsl)
        _set_run_status(run_id, "running")
        await _push_run_status(present_in, run_id, {"run_id": run_id, "status": "running"})
        agen = wf_engine.continue_workflow(
            dsl, run_id=run_id, model=model, tools=tools,
            usage_attr={**(usage_attr or {}), "session_id": present_in},
        )
        await _drive_run_events(run_id, present_in, wf, agen)
        _base.sync_ledger.set_status(run_id, "ok")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _base._log.exception("workflow_continue_failed run=%s workflow=%s", run_id, workflow_id)
        await _mark_run_failed(run_id, exc, present_in)
    finally:
        if fork_id:
            try:
                _base.agents_reg.delete_agent(fork_id)
            except Exception:
                pass
        await _base._push_global_event("workflows.changed", {})


def _truncate_checkpoint_to(run_id: str, checkpoint_id: str) -> None:
    """Trim the run's checkpoint record so its HEAD is ``checkpoint_id``
    (rerun_from). Entries are append-only with parent links *within* the file,
    so a prefix cut stays self-consistent for delta reconstruction — anything
    after the cut (later supersteps, newer interrupts) simply ceases to exist
    for this fork."""
    p = _run_checkpoint_path(run_id)
    try:
        rec = json.loads(p.read_text() or "{}")
    except Exception:
        return
    cps = rec.get("checkpoints") or []
    idx = next((i for i, c in enumerate(cps) if c.get("checkpoint_id") == checkpoint_id), None)
    if idx is None or idx == len(cps) - 1:
        return
    rec["checkpoints"] = cps[: idx + 1]
    # Strip the head entry's pending_writes: they are the ORIGINAL superstep's
    # completed-task outputs (put_writes land on the checkpoint that scheduled
    # the node). surface_pending_writes would then tell langgraph "this task
    # already finished" and SKIP the very node we were asked to re-run — the
    # opposite of rerun_from. Dropping them forces re-execution from scratch.
    rec["checkpoints"][-1].pop("pending_writes", None)
    p.write_text(json.dumps(rec, ensure_ascii=False, default=str))


async def _rerun_from_bg(
    run_id: str, workflow_id: str, node_id: str, pinned_dsl: dict, present_in: str | None = None
) -> None:
    """Background driver for rerun-from-node (design B 屏2「从节点重跑」):

    walks the copied checkpoint's state history (newest first) for the last
    snapshot where ``node_id`` was scheduled, truncates the record so that
    snapshot is the head, then continues like retry_from_checkpoint — only
    the target node and its suffix re-execute, with the source run's context.

    Fidelity limits (accepted, see the review doc): side effects after the
    fork point re-fire; for loop bodies the LAST scheduling wins; the compiled
    graph comes from the run's PINNED dsl_version (never the current one)."""
    from ..checkpointer import FileCheckpointer
    from ..workflows import compiler as wf_compiler
    from ..workflows import engine as wf_engine

    fork_id = None
    try:
        wf, _cur, model, tools, fork_id, usage_attr = _wf_build_deps(
            run_id, workflow_id, pinned_version=int(pinned_dsl.get("dsl_version") or 0) or None
        )
        if not wf:
            raise ValueError(f"workflow '{workflow_id}' not found")
        pinned_dsl = _apply_supervisor_override(_base.wf_store.get_run(run_id), pinned_dsl)
        _set_run_status(run_id, "running")
        await _push_run_status(present_in, run_id, {"run_id": run_id, "status": "running"})
        graph = wf_compiler.compile_workflow(
            pinned_dsl, model, tools, {"run_id": run_id, "events": []},
            checkpointer=FileCheckpointer("default", surface_pending_writes=True),
        )
        target_cid: str | None = None
        async for snap in graph.aget_state_history({"configurable": {"thread_id": run_id}}):
            if node_id in (getattr(snap, "next", ()) or ()):
                target_cid = (snap.config.get("configurable") or {}).get("checkpoint_id")
                break  # newest first → the last time the node was scheduled
        if not target_cid:
            raise ValueError(t(
                f"Node {node_id} has no traceable checkpoint (never scheduled, or checkpoints were cleaned up)",
                f"节点 {node_id} 没有可回溯的检查点（未调度过，或检查点已清理）",
            ))
        _truncate_checkpoint_to(run_id, target_cid)
        # Cross-run result cache: the truncated prefix never passes through
        # the node wrapper (checkpoint-committed), so no false hits there.
        # The TARGET node is force-live (the user asked to re-execute it);
        # its suffix replays only where fingerprints still match, and the
        # re-executed results refresh the index.
        from ..workflows import result_cache as wf_rc

        rc = wf_rc.build_run_cache(_base.wf_store.get_run(run_id) or {}, workflow_id)
        if rc is not None:
            rc.force_live_node = node_id
        agen = wf_engine.continue_workflow(
            pinned_dsl, run_id=run_id, model=model, tools=tools,
            usage_attr={**(usage_attr or {}), "session_id": present_in},
            result_cache=rc,
        )
        await _drive_run_events(run_id, present_in, wf, agen)
        _base.sync_ledger.set_status(run_id, "ok")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _base._log.exception("workflow_rerun_from_failed run=%s node=%s", run_id, node_id)
        await _mark_run_failed(run_id, exc, present_in)
    finally:
        if fork_id:
            try:
                _base.agents_reg.delete_agent(fork_id)
            except Exception:
                pass
        await _base._push_global_event("workflows.changed", {})

"""Shared builders/helpers for the Studio e2e suites (design B, P1+P2).

Covers the surface shipped in 方案B 阶段1+2: run lifecycle, the run-scoped
WebSocket, manual pause + context_patch, rerun_from(node_id), supervisor gates
(human mode) and the decision-inbox / run-list filters.

Conventions every suite relies on:

* DSLs are built from ``llm``/``human`` nodes so a ScriptedChatModel drives the
  REAL engine with no tools; a node that declares ``writes`` must answer with
  ``WRITE_JSON {...}`` so the extract fast path needs no extra model call.
* ``patch_model`` swaps the driver's model factory per test; each run forks a
  FRESH model instance, so scripts replay from index 0 on every run/resume.
* WebSocket receives have no timeout in TestClient — ALWAYS drain through
  :func:`ws_drain`, which caps frames and ends on a frame that certainly
  arrives (the ``run.snapshot`` pushed right after a terminal ``run.status``).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi.testclient import TestClient

from ginno_runtime.testing.fake_model import ScriptedChatModel, script

API = "/api"


# --------------------------------------------------------------------------- #
# DSL builders (each returns a create-workflow payload)
# --------------------------------------------------------------------------- #
def dsl_linear(n: int = 2, writes_first: bool = False, name: str = "L") -> dict:
    """``s1..sn`` llm nodes chained. ``writes_first`` makes s1 a STEP node
    producing an items array via the WRITE_JSON fast path (injects an
    s1__extract step). NB: writes only work on step/agent nodes — the extract
    reads the ``results`` channel, which llm nodes don't populate."""
    nodes = []
    for i in range(1, n + 1):
        node: dict = {"id": f"s{i}", "type": "llm", "prompt": f"p{i}"}
        if i == 1 and writes_first:
            node = {"id": "s1", "type": "step", "agent": "dev", "goal": "produce items",
                    "writes": {"items": {"type": "array", "items": {"type": "string"}}}}
        nodes.append(node)
    edges = [{"from": f"s{i}", "to": f"s{i + 1}"} for i in range(1, n)]
    return {"name": name, "dsl": {"entry": "s1", "nodes": nodes, "edges": edges}}


def dsl_template(key: str = "tag", name: str = "T") -> dict:
    """s1 (step) writes a string ``key``; s2 renders ``value={{context.<key>}}``
    — the observable for context_patch tests (assert via a Recording model)."""
    return {
        "name": name,
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "step", "agent": "dev", "goal": "produce",
                 "writes": {key: {"type": "string"}}},
                {"id": "s2", "type": "llm", "prompt": f"value={{{{context.{key}}}}}"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        },
    }


def dsl_with_human(question: str = "继续吗？") -> dict:
    """s1 llm → h human → s2 llm: parks at the human node deterministically."""
    return {
        "name": "H",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "one"},
                {"id": "h", "type": "human", "question": question},
                {"id": "s2", "type": "llm", "prompt": "two"},
            ],
            "edges": [{"from": "s1", "to": "h"}, {"from": "h", "to": "s2"}],
        },
    }


def dsl_branch(flag_value: str = "go") -> dict:
    """s1 (step) writes a ``flag`` string → branch routes to ``go_a`` (case
    hit) or ``go_b`` (default). Branch routes via cases — no explicit edge
    from it."""
    return {
        "name": "B",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "step", "agent": "dev", "goal": "produce flag",
                 "writes": {"flag": {"type": "string"}}},
                {"id": "gate", "type": "branch",
                 "cases": [{"when": f'flag == "{flag_value}"', "then": "go_a"}],
                 "default": "go_b"},
                {"id": "go_a", "type": "llm", "prompt": "route a"},
                {"id": "go_b", "type": "llm", "prompt": "route b"},
            ],
            "edges": [{"from": "s1", "to": "gate"}],
        },
    }


def dsl_loop(items: list | None = None, max_iters: int = 8, on_empty: str = "skip") -> dict:
    """context.initial seeds ``items``; loop runs body ``b`` per item; then
    ``use``. Sequential (no parallel) so scripting is one call per iteration."""
    if items is None:
        items = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
    return {
        "name": "LP",
        "dsl": {
            "entry": "lp",
            "context": {"initial": {"items": items}},
            "nodes": [
                {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
                 "body": "b", "max_iters": max_iters, "on_empty": on_empty},
                {"id": "b", "type": "llm", "prompt": "item {{it.name}}"},
                {"id": "use", "type": "llm", "prompt": "use"},
            ],
            "edges": [{"from": "lp", "to": "use"}],
        },
    }


def gated(doc: dict, after: list | None = None, mode: str = "human", enabled: bool = True, **extra) -> dict:
    """Copy a create-workflow payload with a supervisor block attached.
    ``after=None`` → every_step (all step-ish nodes); pass ids for after_nodes."""
    sup: dict = {"enabled": enabled, "mode": mode, **extra}
    if after is not None:
        sup["checkpoints"] = {"after_nodes": after}
    out = json.loads(json.dumps(doc, ensure_ascii=False))
    out["dsl"]["supervisor"] = sup
    return out


# --------------------------------------------------------------------------- #
# Model patching
# --------------------------------------------------------------------------- #
def patch_model(monkeypatch, turns: list) -> None:
    """Patch the workflow driver's model factory. ``turns``: plain strings
    (text answers) or AIMessages from ``script(...)``/``script_raise(...)``."""
    msgs = [t if not isinstance(t, str) else script(text=t) for t in turns]
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=list(msgs)),
    )


def wj(value: Any) -> str:
    """A text turn the writes fast path can extract."""
    return "WRITE_JSON " + json.dumps(value, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# API helpers
# --------------------------------------------------------------------------- #
def make_wf(client: TestClient, doc: dict) -> dict:
    r = client.post(f"{API}/workflows", json=doc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok"), body
    return body["workflow"]


def run_wf(client: TestClient, wf_id: str, context_override: dict | None = None, session_id: str | None = None) -> str:
    payload: dict = {"workflow_id": wf_id}
    if context_override is not None:
        payload["context_override"] = context_override
    if session_id is not None:
        payload["session_id"] = session_id
    r = client.post(f"{API}/workflow_runs", json=payload)
    assert r.status_code == 200, r.text
    return r.json()["run"]["id"]


def await_run(client: TestClient, run_id: str) -> dict:
    r = client.post(f"{API}/workflow_runs/{run_id}/_await")
    assert r.status_code == 200, r.text
    return r.json()["run"]


def get_run(client: TestClient, run_id: str) -> dict | None:
    r = client.get(f"{API}/workflow_runs/{run_id}")
    return r.json().get("run") if r.status_code == 200 else None


def evs(client: TestClient, run_id: str, node_id: str | None = None, kind: str | None = None) -> list:
    q: dict = {}
    if node_id:
        q["node_id"] = node_id
    if kind:
        q["kind"] = kind
    r = client.get(f"{API}/workflow_runs/{run_id}/events", params=q or None)
    assert r.status_code == 200, r.text
    return r.json()["events"]


def kinds(events: list) -> list:
    return [e.get("kind") for e in events]


def enters(events: list) -> list:
    return [e.get("node_id") for e in events if e.get("kind") == "node_enter"]


def steps(run: dict) -> dict:
    return {s["id"]: s.get("status") for s in run.get("steps", [])}


def decide(client: TestClient, run_id: str, decision: str, patch: dict | None = None):
    return client.post(f"{API}/workflow_runs/{run_id}/decide", json={"decision": decision, "context_patch": patch})


def resume(client: TestClient, run_id: str, value: dict):
    return client.post(f"{API}/workflow_runs/{run_id}/resume", json=value)


# --------------------------------------------------------------------------- #
# WebSocket helpers
# --------------------------------------------------------------------------- #
def ws_drain(ws, stop: Callable[[dict], bool], max_frames: int = 400) -> list:
    """Receive until ``stop(frame)`` (inclusive), capped. TestClient receives
    have NO timeout — every caller must end on a frame that certainly arrives:
    the ``run.snapshot`` pushed right after a terminal ``run.status``."""
    frames: list = []
    for _ in range(max_frames):
        f = ws.receive_json()
        frames.append(f)
        if stop(f):
            return frames
    raise AssertionError(f"expected frame not seen within {max_frames} frames; got {len(frames)}")


def is_snapshot_with(status: str) -> Callable[[dict], bool]:
    """Stop predicate: the run.snapshot refresh that follows a terminal
    ``run.status`` frame — the reliable end-of-stream marker."""
    want = {"done", "failed", "cancelled"} if status in ("done", "failed", "cancelled") else {status}

    def _stop(f: dict) -> bool:
        return f.get("event") == "run.snapshot" and (f.get("run") or {}).get("status") in want

    return _stop

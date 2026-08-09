"""Completeness tests for docs/workflow-master-plan.md.

Covers:
* §2.1 loop on_empty (skip/fail) + loop_skip/loop_cap events + skipped body
* §2.2 implicit extract node (writes declaration, LLM extraction, WRITE_JSON
  fast path, validation failure attribution)
* §2.3 file-tool hard deny
* §2.4 event ts fidelity (spot check: node events carry their own ts)
* §4.2 doctor rule engine
* §4.5 node_exit usage telemetry (shape only)
* models.build_model_by_name resolution
* dsl.steps_from_dsl filtering of extract nodes
* §3.1 synthesis-case recording
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import doctor as wf_doctor
from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def _patch_build_model(monkeypatch, queue: list):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: queue.pop(0) if queue else ScriptedChatModel(scripts=[]),
    )


# --------------------------------------------------------------------------- #
# §2.1 loop on_empty
# --------------------------------------------------------------------------- #
def _loop_dsl(on_empty: str, items: list) -> dict:
    return {
        "entry": "loop",
        "context": {
            "schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            "initial": {"items": items},
        },
        "nodes": [
            {"id": "loop", "type": "loop", "over": "context.items", "as": "it",
             "body": "body", "max_iters": 5, "on_empty": on_empty},
            {"id": "body", "type": "step", "agent": "dev", "goal": "process {{it}}"},
        ],
        "edges": [],
    }


def test_loop_on_empty_skip_marks_body_skipped(client, monkeypatch):
    wf = store.create_def({"name": "LoopSkip", "dsl": _loop_dsl("skip", [])})
    _patch_build_model(monkeypatch, [])  # body never runs
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    kinds = [e["kind"] for e in evs]
    assert "loop_skip" in kinds
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps.get("body") == "skipped", steps


def test_loop_on_empty_fail_fails_run(client, monkeypatch):
    wf = store.create_def({"name": "LoopFail", "dsl": _loop_dsl("fail", [])})
    _patch_build_model(monkeypatch, [])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "failed", aw
    assert "on_empty" in (aw["run"].get("error") or "")


def test_loop_body_with_writes_extracts(client, monkeypatch):
    """A loop body step that declares writes gets an injected extract node wired
    body -> body__extract -> loop head (master-plan §2.2 loop-body case)."""
    wf = store.create_def({"name": "LoopExtract", "dsl": {
        "entry": "loop",
        "context": {"schema": {"type": "object", "properties": {"items": {"type": "array"}}},
                    "initial": {"items": ["a"]}},
        "nodes": [
            {"id": "loop", "type": "loop", "over": "context.items", "as": "it",
             "body": "body", "max_iters": 3},
            {"id": "body", "type": "step", "agent": "dev", "goal": "process {{it}}",
             "writes": {"result": {"type": "string"}}},
        ],
        "edges": [],
    }})
    _patch_build_model(monkeypatch, [
        ScriptedChatModel(scripts=[
            script(text="processed"),             # body step reply (no WRITE_JSON)
            script(text='{"result": "ok"}'),      # extraction
        ]),
    ])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    assert any(e.get("node_id") == "body__extract" for e in evs)
    cw = [e for e in evs if e["kind"] == "context_write" and e.get("keys") == ["result"]]
    assert cw, "expected extract to write result"


def test_loop_cap_event_when_max_iters_hit(client, monkeypatch):
    wf = store.create_def({"name": "LoopCap", "dsl": {
        "entry": "loop",
        "context": {"schema": {"type": "object", "properties": {"items": {"type": "array"}}},
                    "initial": {"items": ["a", "b", "c"]}},
        "nodes": [
            {"id": "loop", "type": "loop", "over": "context.items", "as": "it",
             "body": "body", "max_iters": 2},
            {"id": "body", "type": "step", "agent": "dev", "goal": "x"},
        ],
        "edges": [],
    }})
    # body runs twice (max_iters=2) then caps
    _patch_build_model(monkeypatch, [
        ScriptedChatModel(scripts=[script(text="a"), script(text="b")]),
    ])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    kinds = [e["kind"] for e in evs]
    assert "loop_cap" in kinds
    assert kinds.count("loop_iter") == 2


# --------------------------------------------------------------------------- #
# §2.2 implicit extract node
# --------------------------------------------------------------------------- #
def _writes_dsl() -> dict:
    return {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "step", "agent": "research", "goal": "pick stocks",
             "writes": {"stocks": {"type": "array", "items": {"type": "object"}}}},
        ],
        "edges": [],
    }


def test_extract_node_llm_path_writes_context(client, monkeypatch):
    wf = store.create_def({"name": "ExtractLLM", "dsl": _writes_dsl()})
    _patch_build_model(monkeypatch, [
        ScriptedChatModel(scripts=[
            script(text="I selected some stocks for you"),       # step reply (no WRITE_JSON)
            script(text='{"stocks": [{"code": "AAPL"}, {"code": "TSLA"}]}'),  # extraction
        ]),
    ])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    cw = [e for e in evs if e["kind"] == "context_write" and e.get("keys") == ["stocks"]]
    assert cw, "expected a context_write for stocks"
    assert cw[-1].get("method") == "llm"
    # extract node appears in the event stream under its synthesized id
    assert any(e.get("node_id") == "s1__extract" for e in evs)
    # and in the run's steps (needed for correct run-status accounting)
    assert any(s["id"] == "s1__extract" for s in aw["run"]["steps"])


def test_extract_multi_key_string_and_array(client, monkeypatch):
    """Mirror the real failure (market_news_summary string + stock_list array):
    the improved prompt must let the extractor emit BOTH exact keys."""
    wf = store.create_def({"name": "ExtractMulti", "dsl": {
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "agent": "research", "goal": "search news",
                   "writes": {"market_news_summary": {"type": "string"},
                              "stock_list": {"type": "array", "items": {"type": "object"}}}}],
        "edges": [],
    }})
    _patch_build_model(monkeypatch, [
        ScriptedChatModel(scripts=[
            script(text="News: markets up. Stocks: X, Y."),
            script(text=json.dumps({
                "market_news_summary": "Markets rose across regions.",
                "stock_list": [{"code": "X"}, {"code": "Y"}],
            })),
        ]),
    ])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    cw = [e for e in evs if e["kind"] == "context_write"
          and set(e.get("keys") or []) == {"market_news_summary", "stock_list"}]
    assert cw, "expected both keys written"


def test_extract_node_write_json_fast_path(client, monkeypatch):
    wf = store.create_def({"name": "ExtractFast", "dsl": _writes_dsl()})
    _patch_build_model(monkeypatch, [
        ScriptedChatModel(scripts=[
            script(text='done\nWRITE_JSON {"stocks": [{"code": "X"}]}'),
        ]),
    ])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    cw = [e for e in evs if e["kind"] == "context_write" and e.get("keys") == ["stocks"]]
    assert cw and cw[-1].get("method") == "write_json"


def test_cap_text_keeps_head_and_tail():
    """Structured results often sit at the END of a long step reply — a head-only
    truncation would drop them (master-plan §2.2 L)."""
    from ginno_runtime.workflows.nodes.extract import _cap_text

    short = "abc"
    assert _cap_text(short, 100) == short
    long_text = ("H" * 9000) + ("T" * 9000)
    out = _cap_text(long_text, 8000)
    assert len(out) <= 8000 + len("\n…[中段省略]…\n")
    assert out.startswith("H")
    assert out.rstrip().endswith("T"), "tail must be preserved"
    assert "中段省略" in out


def test_extract_failure_attributes_to_source_step(client, monkeypatch):
    wf = store.create_def({"name": "ExtractFail", "dsl": _writes_dsl()})
    _patch_build_model(monkeypatch, [
        ScriptedChatModel(scripts=[
            script(text="picked stocks"),
            script(text='{"stocks": "not an array"}'),  # attempt 1 invalid
            script(text='{"stocks": "still not an array"}'),  # attempt 2 invalid
        ]),
    ])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "failed", aw
    detail = aw["run"].get("error_detail") or {}
    assert detail.get("node_id") == "s1", detail          # attributed to source step
    assert detail.get("extract_node_id") == "s1__extract", detail


# --------------------------------------------------------------------------- #
# §4.2 doctor rule engine
# --------------------------------------------------------------------------- #
def test_doctor_loop_over_no_source():
    dsl = {
        "entry": "loop",
        "nodes": [
            {"id": "loop", "type": "loop", "over": "context.stocks",
             "body": "body", "max_iters": 3},
            {"id": "body", "type": "step", "goal": "x"},
        ],
        "edges": [],
    }
    r = wf_doctor.run_doctor(dsl)
    rules = [e["rule"] for e in r["errors"]]
    assert "loop.over.no_source" in rules


def test_doctor_goal_context_ref_no_source():
    dsl = {
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "goal": "use {{context.missing}}"}],
        "edges": [],
    }
    r = wf_doctor.run_doctor(dsl)
    rules = [e["rule"] for e in r["errors"]]
    assert "goal.context_ref.no_source" in rules


def test_doctor_satisfied_by_writes_and_initial():
    dsl = {
        "entry": "s1",
        "context": {"initial": {"date": "2026-08-10"}},
        "nodes": [
            {"id": "s1", "type": "step", "goal": "pick",
             "writes": {"stocks": {"type": "array", "items": {"type": "object"}}}},
            {"id": "loop", "type": "loop", "over": "context.stocks", "body": "b", "max_iters": 3},
            {"id": "b", "type": "step", "goal": "use {{context.date}}"},
        ],
        "edges": [{"from": "s1", "to": "loop"}],
    }
    r = wf_doctor.run_doctor(dsl)
    assert r["errors"] == [], r


def test_doctor_reserved_suffix_and_unused_writes():
    dsl = {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "step", "goal": "x",
             "writes": {"dead_key": {"type": "string"}}},
            {"id": "evil__extract", "type": "step", "goal": "y"},
        ],
        "edges": [{"from": "s1", "to": "evil__extract"}],
    }
    r = wf_doctor.run_doctor(dsl)
    rules = [e["rule"] for e in r["errors"]]
    assert "node_id.reserved_suffix" in rules
    wrules = [w["rule"] for w in r["warnings"]]
    assert "writes.unused" in wrules


def test_doctor_endpoint(client):
    wf = store.create_def({"name": "DoctorWf", "dsl": {
        "entry": "loop",
        "nodes": [
            {"id": "loop", "type": "loop", "over": "context.nope", "body": "b", "max_iters": 2},
            {"id": "b", "type": "step", "goal": "x"},
        ],
        "edges": [],
    }})
    r = client.get(f"/api/workflows/{wf['id']}/doctor")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert any(e["rule"] == "loop.over.no_source" for e in body["errors"])


# --------------------------------------------------------------------------- #
# validate_dsl: writes / extract_model / extract-type guards
# --------------------------------------------------------------------------- #
def test_validate_rejects_bad_writes_and_reserved_id():
    errs = wf_dsl.validate_dsl(wf_dsl.normalize_dsl({
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "goal": "x", "writes": {"k": {"no_type": 1}}}],
        "edges": [],
    }))
    assert any("writes" in e for e in errs)

    errs = wf_dsl.validate_dsl(wf_dsl.normalize_dsl({
        "entry": "a__extract",
        "nodes": [{"id": "a__extract", "type": "step", "goal": "x"}],
        "edges": [],
    }))
    assert any("__extract" in e for e in errs)

    errs = wf_dsl.validate_dsl(wf_dsl.normalize_dsl({
        "entry": "e",
        "nodes": [{"id": "e", "type": "extract", "source_node": "x", "writes": {"k": {"type": "string"}}}],
        "edges": [],
    }))
    assert any("extract" in e and "internal" in e for e in errs)


def test_steps_from_dsl_include_extracts_for_accounting():
    dsl = {
        "nodes": [
            {"id": "s1", "type": "step", "goal": "a",
             "writes": {"stocks": {"type": "array"}}},
            {"id": "s2", "type": "step", "goal": "b"},
        ],
    }
    # default view: user-authored steps only
    assert [s["id"] for s in wf_dsl.steps_from_dsl(dsl)] == ["s1", "s2"]
    # run-accounting view: extract pseudo-steps inserted after their producer
    ids = [s["id"] for s in wf_dsl.steps_from_dsl(dsl, include_extracts=True)]
    assert ids == ["s1", "s1__extract", "s2"]


# --------------------------------------------------------------------------- #
# §2.3 file-tool hard deny
# --------------------------------------------------------------------------- #
def test_hard_deny_home_protected_but_workspace_exempt(monkeypatch, tmp_path):
    """The sidecar home (~/.ginno) is denied, but a session workspace that is a
    PROPER subdir of home stays usable (master-plan §2.3 baseline)."""
    from ginno_runtime.tools import builtin as tools_builtin

    fake_home = tmp_path / "fake_home"
    (fake_home / "projects" / "default" / "sessions" / "s1").mkdir(parents=True)
    monkeypatch.setattr(tools_builtin.paths, "home", lambda: fake_home)
    ws = fake_home / "projects" / "default" / "sessions" / "s1"

    tools = {t.name: t for t in tools_builtin.build_builtin_tools(str(ws))}

    # A sensitive home file OUTSIDE the workspace is denied.
    secret = fake_home / "settings.json"
    secret.write_text('{"api_key": "sk"}', encoding="utf-8")
    out = tools["read_file"].invoke({"path": str(secret)})
    assert out.startswith("[error]") and "拒绝访问" in out

    # A file inside the session workspace is readable (exemption).
    (ws / "work.txt").write_text("hello", encoding="utf-8")
    assert tools["read_file"].invoke({"path": "work.txt"}) == "hello"

    # bash targeting a protected root is denied.
    out = tools["bash"].invoke({"command": f"cat {secret}"})
    assert out.startswith("[error]")


def test_hard_deny_cwd_home_fallback_protected(monkeypatch, tmp_path):
    """When the workspace equals home itself (the workflow cwd fallback), the
    home deny is NOT exempted — a cwd=home run must not scan ~/.ginno."""
    from ginno_runtime.tools import builtin as tools_builtin

    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True)
    monkeypatch.setattr(tools_builtin.paths, "home", lambda: fake_home)
    (fake_home / "data.txt").write_text("raw", encoding="utf-8")

    # workspace == home
    tools = {t.name: t for t in tools_builtin.build_builtin_tools(str(fake_home))}
    out = tools["read_file"].invoke({"path": str(fake_home / "data.txt")})
    assert out.startswith("[error]"), out
    # glob over home yields nothing (all pruned)
    assert tools["glob_files"].invoke({"pattern": "**/*.txt"}) == "(no matches)"


# --------------------------------------------------------------------------- #
# models.build_model_by_name
# --------------------------------------------------------------------------- #
def test_build_model_by_name_resolution(monkeypatch):
    import ginno_runtime.models as M

    monkeypatch.setattr(M.prov_mod, "load_providers", lambda: {
        "anthropic": {"enabled": True, "model": "claude-x"},
        "custom": {"enabled": True, "model": "qwen-plus"},
    })
    calls = []
    monkeypatch.setattr(M, "build_model",
                        lambda pid, model_name=None, **k: calls.append((pid, model_name)) or "MODEL")
    monkeypatch.setattr(M.prov_mod, "get_default_provider", lambda: "anthropic")

    assert M.build_model_by_name("qwen-plus") == "MODEL"
    assert calls[-1] == ("custom", "qwen-plus")
    M.build_model_by_name("anthropic")
    assert calls[-1] == ("anthropic", None)
    M.build_model_by_name("mystery-model")
    assert calls[-1] == ("anthropic", "mystery-model")


# --------------------------------------------------------------------------- #
# §2.4 event ts fidelity
# --------------------------------------------------------------------------- #
def test_events_carry_own_ts(client, monkeypatch):
    wf = store.create_def({"name": "TsWf", "dsl": {
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "x"}],
        "edges": [],
    }})
    _patch_build_model(monkeypatch, [ScriptedChatModel(scripts=[script(text="ok")])])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    node_evs = [e for e in evs if e["kind"] in ("node_enter", "node_exit")]
    assert node_evs, evs
    assert all(isinstance(e.get("ts"), (int, float)) for e in node_evs)


def test_node_exit_carries_usage_shape(client, monkeypatch):
    wf = store.create_def({"name": "UsageWf", "dsl": {
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "x"}],
        "edges": [],
    }})
    _patch_build_model(monkeypatch, [ScriptedChatModel(scripts=[script(text="ok")])])
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    exits = [e for e in evs if e["kind"] == "node_exit"]
    assert exits
    # usage key present (may be empty dict for the scripted model)
    assert "usage" in exits[-1]


# --------------------------------------------------------------------------- #
# §3.1 synthesis-case recording
# --------------------------------------------------------------------------- #
def test_synthesis_case_lifecycle():
    from ginno_runtime.workflows import synthesis as ws

    case_dir, syn_id = ws.new_case(
        "sess-abcdef12345", provider="anthropic", model="claude-x", last_n=None,
        trace="USER: hi", session_stats={"messages": 1, "tool_calls": 0},
        prompt_version="synth-test",
    )
    assert case_dir is not None and case_dir.is_dir()
    ws.record_attempt(case_dir, attempt=1, latency_ms=10, raw="{}", parse="ok",
                      validate_errors=["e"], hint_fed_back="hint")
    ws.finish_case(case_dir, status="failed", dsl={"x": 1}, fail_stage="schema.e",
                   total_latency_ms=10, attempts_used=1)
    ws.backfill_outcome(case_dir, created=True, workflow_id="wf123")

    case = ws.load_case(syn_id)
    assert case["input"]["trace"] == "USER: hi"
    assert case["output"]["fail_stage"] == "schema.e"
    assert case["outcome"]["workflow_id"] == "wf123"
    assert case["attempts"][0]["parse"] == "ok"

    cases = ws.list_cases()
    assert any(c["synthesis_id"] == syn_id for c in cases)


def test_synthesis_records_on_summarize(client, monkeypatch):
    """The live endpoint must produce a case dir + synthesis_id in the reply."""
    from ginno_runtime import server as srv
    from ginno_runtime.checkpointer import FileCheckpointer
    from langchain_core.messages import HumanMessage

    sid = "sess-synth-rec"
    srv._session_meta_upsert("default", {"id": sid, "title": "t", "agent_id": "dev"})
    cp = FileCheckpointer("default")
    state = {"messages": [HumanMessage(content="build a workflow")], "workspace": "/tmp",
             "project_slug": "default", "agent_id": "dev", "active_skills": [],
             "pending_tool_calls": []}
    cp.put({"configurable": {"thread_id": sid}},
           {"id": "c1", "channel_values": state, "pending_sends": []}, {}, {})

    dsl_json = json.dumps({"name": "W", "entry": "s1",
                           "nodes": [{"id": "s1", "type": "step", "goal": "x"}], "edges": []})
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model",
                        lambda *a, **k: ScriptedChatModel(scripts=[script(text=dsl_json)]))
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    body = r.json()
    assert body["ok"] is True, body
    assert body.get("synthesis_id")

    from ginno_runtime.workflows import synthesis as ws
    case = ws.load_case(body["synthesis_id"])
    assert case["output"]["status"] == "ok"


def _seed_case(client, monkeypatch, sid="sess-api-case"):
    from ginno_runtime import server as srv
    from ginno_runtime.checkpointer import FileCheckpointer
    from langchain_core.messages import HumanMessage

    srv._session_meta_upsert("default", {"id": sid, "title": "t", "agent_id": "dev"})
    cp = FileCheckpointer("default")
    state = {"messages": [HumanMessage(content="build a workflow")], "workspace": "/tmp",
             "project_slug": "default", "agent_id": "dev", "active_skills": [],
             "pending_tool_calls": []}
    cp.put({"configurable": {"thread_id": sid}},
           {"id": "c1", "channel_values": state, "pending_sends": []}, {}, {})
    dsl_json = json.dumps({"name": "W", "entry": "s1",
                           "nodes": [{"id": "s1", "type": "step", "goal": "x"}], "edges": []})
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model",
                        lambda *a, **k: ScriptedChatModel(scripts=[script(text=dsl_json)]))
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    return r.json()["synthesis_id"]


def test_synthesis_api_cases_and_stats(client, monkeypatch):
    syn_id = _seed_case(client, monkeypatch)
    cases = client.get("/api/synthesis/cases").json()
    assert cases["ok"] is True
    assert any(c["synthesis_id"] == syn_id for c in cases["cases"])
    stats = client.get("/api/synthesis/stats?days=30").json()
    assert stats["ok"] is True
    assert stats["total"] >= 1
    assert stats["l1_generated"] >= 1


def test_synthesis_api_case_detail_and_replay(client, monkeypatch):
    syn_id = _seed_case(client, monkeypatch, sid="sess-api-replay")
    detail = client.get(f"/api/synthesis/cases/{syn_id}").json()
    assert detail["ok"] is True
    assert detail["case"]["input"]["trace"]

    # replay re-runs synthesis on the stored trace (fresh fake model)
    dsl_json = json.dumps({"name": "W2", "entry": "s1",
                           "nodes": [{"id": "s1", "type": "step", "goal": "y"}], "edges": []})
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model",
                        lambda *a, **k: ScriptedChatModel(scripts=[script(text=dsl_json)]))
    rp = client.post(f"/api/synthesis/replay/{syn_id}").json()
    assert rp["ok"] is True, rp
    assert rp["dsl"]["name"] == "W2"
    assert rp["synthesis_id"] == syn_id


def test_synthesis_replay_404_unknown(client):
    assert client.post("/api/synthesis/replay/nope").status_code == 404

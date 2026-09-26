"""Supervisor AUTO mode end-to-end (design B §8.5, P2.5 / 方案B 阶段3): the LLM
adjudicator applies gate decisions (continue/skip/retry), the fallback ladder
escalates to a human park (low-confidence / judge-error / retry-limit /
interventions-exceeded / token-budget), budgets ride the events, and the
pre-run ``supervisor_override`` re-flips the mode per run without touching the
stored DSL.

Hermetic: the adjudicator is monkeypatched (``ginno_runtime.workflows.
supervisor_runtime.adjudicate``) except in the scripted-JSON case, which
drives the REAL adjudicator through the run's ScriptedChatModel.
"""

from __future__ import annotations

import json

import pytest
from pydantic import PrivateAttr

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _gated_dsl(mode: str = "auto", after: list | None = ["s1"], **extra):
    return {
        "name": "SupAuto",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "llm", "prompt": "one"},
                {"id": "s2", "type": "llm", "prompt": "value={{context.tag}}"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
            "supervisor": {
                "enabled": True,
                "mode": mode,
                **({"checkpoints": {"after_nodes": after}} if after is not None else {}),
                **extra,
            },
        },
    }


def _model(monkeypatch, texts):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[script(text=t) for t in texts]),
    )


def _verdict(decision="continue", confidence=0.95, reason="ok", patch=None, usage=None):
    return {
        "decision": decision,
        "confidence": confidence,
        "reason": reason,
        "context_patch": patch,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def _adjudicate(monkeypatch, results: list):
    """Script the adjudicator: results consumed in order, last one repeats."""
    seq = list(results)

    async def _fake(**kwargs):
        r = dict(seq.pop(0)) if seq else dict(results[-1])
        return r

    monkeypatch.setattr("ginno_runtime.workflows.supervisor_runtime.adjudicate", _fake)


def _run(client, wf, body_extra: dict | None = None) -> str:
    r = client.post("/api/workflow_runs", json={"workflow_id": wf["id"], **(body_extra or {})})
    assert r.status_code == 200, r.text
    return r.json()["run"]["id"]


def _await(client, rid):
    r = client.post(f"/api/workflow_runs/{rid}/_await")
    assert r.status_code == 200, r.text
    return r.json()


def _evs(client, rid, kind: str | None = None) -> list:
    params = {"kind": kind} if kind else None
    return client.get(f"/api/workflow_runs/{rid}/events", params=params).json()["events"]


def _decide(client, rid, decision, patch=None):
    return client.post(
        f"/api/workflow_runs/{rid}/decide",
        json={"decision": decision, "context_patch": patch},
    )


# --------------------------------------------------------------------------- #
# Decisions applied
# --------------------------------------------------------------------------- #
def test_auto_continue_applies_without_pause(client, monkeypatch):
    _adjudicate(monkeypatch, [_verdict()])
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "done"
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps == {"s1": "done", "s2": "done"}
    assert not [e for e in _evs(client, rid) if e["kind"] == "interrupt"]

    ev = _evs(client, rid, "sup_eval")
    assert len(ev) == 1
    assert ev[0]["verdict"] == "ok" and ev[0]["confidence"] == 0.95
    assert ev[0]["confidence_min"] == 0.7 and ev[0]["gate"] == "s1__sup"
    dec = _evs(client, rid, "sup_decision")
    assert len(dec) == 1
    d = dec[0]
    assert d["node_id"] == "s1" and d["gate"] == "s1__sup"
    assert d["mode"] == "auto" and d["decision"] == "continue" and d["confidence"] == 0.95
    assert d.get("context_patch") is None  # None-valued keys are stripped in storage
    assert d["budget"] == {
        "interventions": 0, "max_interventions": 5, "tokens": 15, "token_budget": 20000,
    }


def test_auto_retry_reexecutes_then_continues(client, monkeypatch):
    _adjudicate(monkeypatch, [_verdict("retry", 0.9, "输出不完整"), _verdict()])
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "one-again", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "done", aw
    enters = [e for e in _evs(client, rid) if e["kind"] == "node_enter" and e["node_id"] == "s1"]
    assert len(enters) == 2  # the gated node really re-ran
    dec = _evs(client, rid, "sup_decision")
    assert [d["decision"] for d in dec] == ["retry", "continue"]
    assert dec[0]["budget"]["interventions"] == 1  # the retry counted
    assert dec[1]["budget"]["interventions"] == 1  # continue does not add


def test_auto_skip_recorded_and_run_continues(client, monkeypatch):
    _adjudicate(monkeypatch, [_verdict("skip", 0.8, "该步骤结果应忽略")])
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "done"
    dec = _evs(client, rid, "sup_decision")[0]
    assert dec["decision"] == "skip" and dec["mode"] == "auto"
    assert dec["budget"]["interventions"] == 1
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps == {"s1": "done", "s2": "done"}  # skip routes to the successor


def test_auto_abort_ends_with_pending_suffix(client, monkeypatch):
    _adjudicate(monkeypatch, [_verdict("abort", 0.9, "结果有害")])
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "never"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "done"  # routed to END, not failed
    assert _evs(client, rid, "sup_decision")[0]["decision"] == "abort"
    steps = {s["id"]: s["status"] for s in aw["run"]["steps"]}
    assert steps["s2"] == "pending"


def test_context_patch_from_adjudicator_reaches_downstream(client, monkeypatch):
    prompts: list = []

    class Recording(ScriptedChatModel):
        _seen: list = PrivateAttr(default_factory=list)

        def __init__(self):
            super().__init__(scripts=[script(text="one"), script(text="two")])
            self._seen = []

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            chunk = "\n".join(str(getattr(m, "content", "")) for m in messages)
            self._seen.append(chunk)
            prompts.append(chunk)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model", lambda *a, **k: Recording()
    )
    _adjudicate(monkeypatch, [_verdict(patch={"tag": "zzz"})])
    wf = store.create_def(_gated_dsl())
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "done"
    dec = _evs(client, rid, "sup_decision")[0]
    assert dec["context_patch"] == ["tag"]
    assert any("value=zzz" in p for p in prompts)  # s2 rendered the patched value


def test_real_adjudicator_parses_scripted_json(client, monkeypatch):
    """No mock: the judge IS the run's ScriptedChatModel — its second script is
    the adjudication JSON, parsed by the real supervisor_runtime.adjudicate."""
    judge = json.dumps(
        {"decision": "continue", "confidence": 0.88, "reason": "输出可用"},
        ensure_ascii=False,
    )
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", f"好的，我的裁决：\n```json\n{judge}\n```", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "done", aw
    dec = _evs(client, rid, "sup_decision")
    assert len(dec) == 1
    assert dec[0]["mode"] == "auto" and dec[0]["confidence"] == 0.88
    assert dec[0]["reason"] == "输出可用"


# --------------------------------------------------------------------------- #
# Fallback ladder (first hit wins) → human park
# --------------------------------------------------------------------------- #
def test_low_confidence_falls_back_then_human_decides(client, monkeypatch):
    _adjudicate(monkeypatch, [_verdict("continue", 0.41, "不太确定")])
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "paused"
    pi = aw["run"]["pending_interrupt"]
    assert pi["kind"] == "supervisor" and pi["node_id"] == "s1"
    assert pi["fallback_reason"] == "low-confidence"
    assert pi["auto_suggestion"]["decision"] == "continue"
    assert pi["auto_suggestion"]["confidence"] == 0.41
    assert pi["question"].startswith("auto 置信度不足")
    fb = _evs(client, rid, "sup_fallback")
    assert len(fb) == 1 and fb[0]["reason"] == "low-confidence"
    ev = _evs(client, rid, "sup_eval")[0]
    assert ev["verdict"] == "low-confidence" and ev["confidence_min"] == 0.7

    # ladder end-to-end: the human decides, the run resumes on the pinned graph
    assert _decide(client, rid, "continue").status_code == 200
    aw2 = _await(client, rid)
    assert aw2["run"]["status"] == "done", aw2
    hd = _evs(client, rid, "supervisor_decision")
    assert hd and hd[-1]["decision"] == "continue" and hd[-1]["mode"] == "human"


def test_judge_error_falls_back(client, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr("ginno_runtime.workflows.supervisor_runtime.adjudicate", _boom)
    wf = store.create_def(_gated_dsl())
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "paused"
    pi = aw["run"]["pending_interrupt"]
    assert pi["fallback_reason"] == "judge-error"
    assert pi["auto_suggestion"] is None  # nothing to suggest
    assert pi["question"].startswith("auto 裁判不可用")
    assert _evs(client, rid, "sup_fallback")[0]["reason"] == "judge-error"
    assert _evs(client, rid, "sup_eval") == []  # nothing was judged
    assert _decide(client, rid, "continue").status_code == 200
    assert _await(client, rid)["run"]["status"] == "done"


def test_retry_limit_fallback(client, monkeypatch):
    _adjudicate(monkeypatch, [_verdict("retry", 0.9, "再试一次")])
    wf = store.create_def(_gated_dsl(retry_limit=1))
    _model(monkeypatch, ["one", "one-again"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "paused"
    pi = aw["run"]["pending_interrupt"]
    assert pi["fallback_reason"] == "retry-limit"
    enters = [e for e in _evs(client, rid) if e["kind"] == "node_enter" and e["node_id"] == "s1"]
    assert len(enters) == 2  # retry applied once, the second one hit the limit


def test_interventions_exceeded_fallback(client, monkeypatch):
    # every_step: gates after s1 AND s2 — the second non-continue decision
    # breaches max_interventions=1 and parks before it could be applied.
    _adjudicate(monkeypatch, [_verdict("skip", 0.9, "跳过")])
    wf = store.create_def(_gated_dsl(after=None, max_interventions=1))
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "paused"
    pi = aw["run"]["pending_interrupt"]
    assert pi["fallback_reason"] == "interventions-exceeded"
    dec = _evs(client, rid, "sup_decision")
    assert [d["decision"] for d in dec] == ["skip"]  # only the first was applied


def test_token_budget_fallback(client, monkeypatch):
    _adjudicate(monkeypatch, [
        _verdict(usage={"input_tokens": 100, "output_tokens": 50})
    ])
    wf = store.create_def(_gated_dsl(token_budget=1))
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf)
    aw = _await(client, rid)
    assert aw["run"]["status"] == "paused"
    pi = aw["run"]["pending_interrupt"]
    assert pi["fallback_reason"] == "token-budget"
    assert pi["question"].startswith("auto 裁判 token 超预算")


# --------------------------------------------------------------------------- #
# supervisor_override (config layer 2)
# --------------------------------------------------------------------------- #
def test_override_forces_auto_on_human_dsl(client, monkeypatch):
    _adjudicate(monkeypatch, [_verdict()])
    wf = store.create_def(_gated_dsl(mode="human"))
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf, {"supervisor_override": {"mode": "auto"}})
    aw = _await(client, rid)
    assert aw["run"]["status"] == "done"  # NO pause — the override won
    dec = _evs(client, rid, "sup_decision")
    assert len(dec) == 1 and dec[0]["mode"] == "auto" and dec[0]["decision"] == "continue"
    run = client.get(f"/api/workflow_runs/{rid}").json()["run"]
    assert run["supervisor_override"] == {"mode": "auto"}  # persisted verbatim


def test_override_forces_human_on_auto_dsl_and_survives_resume(client, monkeypatch):
    wf = store.create_def(_gated_dsl(mode="auto"))
    _model(monkeypatch, ["one", "two"])
    rid = _run(client, wf, {"supervisor_override": {"mode": "human"}})
    aw = _await(client, rid)
    assert aw["run"]["status"] == "paused"
    pi = aw["run"]["pending_interrupt"]
    assert pi["kind"] == "supervisor"
    assert "fallback_reason" not in pi  # a plain human park, not a ladder fallback
    assert _decide(client, rid, "continue").status_code == 200
    assert _await(client, rid)["run"]["status"] == "done"  # resumed on the pinned graph
    hd = _evs(client, rid, "supervisor_decision")
    assert hd[-1]["mode"] == "human"
    # the stored DSL was never mutated by the merge
    assert store.get_def(wf["id"])["dsl"]["supervisor"]["mode"] == "auto"

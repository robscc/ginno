"""Synthesis stats funnel groups by prompt_version (stability plan P4), so
prompt generations (synth-4 vs synth-5) A/B directly in Settings."""


def _seed_case(session_id, prompt_version, status, created=False):
    from ginno_runtime.workflows import synthesis as wf_synth

    case_dir, syn_id = wf_synth.new_case(
        session_id,
        provider="anthropic",
        model="test-model",
        last_n=None,
        trace="USER: x",
        session_stats={"messages": 1, "tool_calls": 0},
        prompt_version=prompt_version,
    )
    wf_synth.finish_case(
        case_dir,
        status=status,
        dsl={"name": "t"} if status == "ok" else None,
        fail_stage=None if status == "ok" else "schema.edge_unknown",
        total_latency_ms=10,
        attempts_used=1,
    )
    if created:
        wf_synth.backfill_outcome(case_dir, created=True, workflow_id="wf-x")
    return syn_id


def test_stats_groups_funnel_by_prompt_version(client):
    _seed_case("aaaa0001aaaa", "synth-4", "ok", created=True)
    _seed_case("bbbb0002bbbb", "synth-4", "failed")
    _seed_case("cccc0003cccc", "synth-5", "ok", created=True)

    r = client.get("/api/synthesis/stats?days=1")
    assert r.status_code == 200
    body = r.json()
    byv = body["by_prompt_version"]
    assert set(byv) == {"synth-4", "synth-5"}
    assert byv["synth-4"]["total"] == 2
    assert byv["synth-4"]["l1_generated"] == 1
    assert byv["synth-4"]["l2_adopted"] == 1
    assert byv["synth-5"]["total"] == 1
    assert byv["synth-5"]["l1_generated"] == 1
    # Global funnel still aggregates across versions.
    assert body["total"] == 3
    assert body["l1_generated"] == 2

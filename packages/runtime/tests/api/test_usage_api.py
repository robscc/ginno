"""API tests for /api/usage/* and the logged session usage (design §5)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest
from conftest import events_of

from ginno_runtime import usage_store
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def _fresh_store_cache():
    usage_store.reset_cache()
    yield
    usage_store.reset_cache()


def _seed(session_id="sess-a", provider="anthropic", model="claude-x", source="chat", **kw):
    usage_store.record(
        input_tokens=kw.get("input_tokens", 1000),
        output_tokens=kw.get("output_tokens", 100),
        cache_read_tokens=kw.get("cache_read_tokens", 600),
        cache_creation_tokens=kw.get("cache_creation_tokens", 0),
        provider=provider, model=model, source=source,
        session_id=session_id, project_slug="default",
    )


def test_overview_endpoint(client):
    _seed()
    _seed(provider="custom", model="deepseek-chat", session_id="sess-b",
          input_tokens=500, cache_read_tokens=0)
    r = client.get("/api/usage/overview?days=30")
    data = r.json()
    assert r.status_code == 200 and data["ok"]
    assert data["totals"]["input_tokens"] == 1500
    assert data["totals"]["calls"] == 2
    assert data["sessions_active"] == 2
    assert data["providers"][0]["provider"] == "anthropic"
    assert data["models"][0]["model"] == "claude-x"
    assert len(data["daily"]) == 30


def test_overview_days_clamped(client):
    _seed()
    assert client.get("/api/usage/overview?days=0").json()["window"]["days"] == 1
    assert client.get("/api/usage/overview?days=9999").json()["window"]["days"] == usage_store.RETENTION_DAYS


def test_overview_sources_breakdown(client):
    """Overview splits usage by source (chat vs workflow, design §3.6) while
    totals stay whole-account; sorted by total tokens desc."""
    _seed(source="chat", input_tokens=1000, output_tokens=100)
    _seed(source="workflow", session_id=None, input_tokens=300, output_tokens=50)
    _seed(source="workflow", session_id=None, input_tokens=200, output_tokens=20)
    data = client.get("/api/usage/overview?days=7").json()
    srcs = {s["source"]: s for s in data["sources"]}
    assert srcs["chat"]["calls"] == 1
    assert srcs["chat"]["input_tokens"] == 1000
    assert srcs["workflow"]["calls"] == 2
    assert srcs["workflow"]["input_tokens"] == 500
    assert data["sources"][0]["source"] == "chat"  # biggest first
    assert data["totals"]["input_tokens"] == 1500  # whole-account unchanged


def test_grid_endpoint(client):
    """点格图：连续日历 × 12 个 2h 桶，cell = [gross, net, out, cache, calls]"""
    _seed(input_tokens=1000, output_tokens=100, cache_read_tokens=600)
    data = client.get("/api/usage/grid").json()
    assert data["ok"]
    assert len(data["days"]) == len(data["grid"]) == 30
    assert data["days"][-1] == time.strftime("%Y-%m-%d", time.localtime())
    row = data["grid"][-1]
    assert len(row) == 12
    slot = time.localtime().tm_hour // 2
    cell = row[slot]
    assert cell[4] == 1                       # requests
    assert cell[0] == 1100                    # gross = in + out
    assert cell[1] == 1000 + 100 - 600        # net = (in - cache) + out
    assert cell[2] == 100 and cell[3] == 600  # out / cache
    assert row[(slot + 6) % 12] == [0, 0, 0, 0, 0]


def test_grid_endpoint_from_to_and_missing_days(client):
    today = datetime.now()
    day0 = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    day1 = today.strftime("%Y-%m-%d")
    _seed()
    data = client.get(f"/api/usage/grid?from={day0}&to={day1}").json()
    assert data["days"] == [day0, day1]
    # 没有 jsonl 文件的昨天照样占一列，整列全 0，且无 have/have_file 标记
    assert data["grid"][0] == [[0, 0, 0, 0, 0]] * 12
    assert "have" not in repr(data)


def test_grid_endpoint_clamps_to_retention(client):
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    data = client.get(f"/api/usage/grid?from={old}").json()
    assert len(data["days"]) == usage_store.RETENTION_DAYS
    assert len(data["grid"]) == usage_store.RETENTION_DAYS


def test_hourly_endpoint_removed(client):
    """/api/usage/hourly 没有调用方后被删除（design §7）。注意不能断言 404：
    server 末尾的 SPA catch-all 会把任何未知路径回成 index.html。"""
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/usage/grid" in paths
    assert "/api/usage/hourly" not in paths


def test_sessions_endpoint_joins_meta_and_placeholder(client):
    _seed(session_id="ghost-session")  # no session meta -> deleted placeholder
    data = client.get("/api/usage/sessions").json()
    assert data["ok"]
    rows = data["sessions"]
    assert rows[0]["session_id"] == "ghost-session"
    assert rows[0]["deleted"] is True
    assert rows[0]["title"].startswith("(deleted)")


def test_requests_endpoint_filters_and_paginates(client):
    for i in range(6):
        _seed(session_id="s", provider="anthropic" if i % 2 else "openai")
    data = client.get("/api/usage/requests?provider=anthropic").json()
    assert data["ok"] and data["total"] == 3
    page = client.get("/api/usage/requests?page=1&page_size=4").json()
    assert len(page["rows"]) == 4 and page["total"] == 6
    assert client.get("/api/usage/requests?source=goal").json()["total"] == 0


def test_session_usage_prefers_log(client):
    _seed(session_id="sess-log", input_tokens=4000, cache_read_tokens=1000)
    data = client.get("/api/sessions/sess-log/usage").json()
    assert data["ok"] and data["usage"]["input_tokens"] == 4000
    assert data["usage"]["cache_hit_ratio"] == 0.25
    # unknown session with no log → 404 (an unknown session is not the same
    # as a session with no usage; the frontend distinguishes them)
    r = client.get("/api/sessions/never-seen/usage")
    assert r.status_code == 404
    assert r.json()["ok"] is False  # structured detail is unwrapped (API-05)


def test_turn_is_recorded_end_to_end(create_session, ws_conv, client):
    """A real turn through the graph writes a usage row with the session's
    provider/model and source=chat, visible via the usage APIs."""
    # langchain 1.x shape: input_tokens is already the WHOLE prompt
    # (non-cached 60 + cache_read 40); extraction passes it through.
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
             "input_token_details": {"cache_read": 40, "cache_creation": 0}}
    sid = create_session([script(text="done", usage=usage)], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        events = conv.recv_until("message.end", "error")
    assert events_of(events, "usage"), "expected a usage event for the turn"

    req = client.get(f"/api/usage/requests?session_id={sid}").json()
    assert req["total"] == 1
    row = req["rows"][0]
    assert row["session_id"] == sid
    assert row["source"] == "chat"
    # whole-prompt input passes through unchanged
    assert row["input_tokens"] == 100
    assert row["cache_read_tokens"] == 40
    assert row["provider"] == "custom"  # nothing enabled -> fallthrough provider

    # session usage endpoint now sees the logged total
    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["usage"]["input_tokens"] == 100
    assert data["usage"]["calls"] == 1

    # and it survives an in-memory reset (simulated runtime restart)
    from ginno_runtime import server
    server._USAGE_BY_SESSION.clear()
    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["usage"]["input_tokens"] == 100


def test_context_field_in_event_and_endpoint(create_session, ws_conv, client):
    """TopBar 圆环数据：usage WS 事件带 context{tokens,window,percent}（本次
    调用的全量 prompt = 当前上下文），GET /usage 的 context 字段同口径
    （最近一次 log 行），无记录时为 None。"""
    usage = {"input_tokens": 50_000, "output_tokens": 10,
             "total_tokens": 50_010, "input_token_details": {}}
    sid = create_session([script(text="done", usage=usage)], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        events = conv.recv_until("message.end", "error")
    ev = events_of(events, "usage")[0]
    ctx = ev["context"]
    assert ctx["tokens"] == 50_000
    assert ctx["window"] > 0
    assert 0 < ctx["percent"] <= 100
    assert abs(ctx["tokens"] * 100 / ctx["window"] - ctx["percent"]) < 0.2

    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["context"]["tokens"] == 50_000 == ctx["tokens"]

    # 无模型调用的会话 → context 为 None（前端不显示圆环）
    sid2 = create_session([script(text="ok")], agent_id="dev")
    data2 = client.get(f"/api/sessions/{sid2}/usage").json()
    assert data2["context"] is None


def _ctx_seed(session_id, source, input_tokens):
    _seed(session_id=session_id, source=source, input_tokens=input_tokens)


def test_context_of_session_skips_compaction_record(client):
    """ContextRing 锯齿（2026-11-07）复现·单元级：压缩调用的 input 是
    「被压全量」（≈压缩前历史，如 360k/600k=60%），不是当前上下文。它 record
    进 usage_store（2026-10-12「压缩的也要算进去」口径不动）但 GET /usage 的
    context 必须跳过——否则手动 /compact 后压缩 record 是最后一条，切会话
    seed 出被压前的高占用，与 WS usage 帧的压缩后真值交替闪烁。"""
    _ctx_seed("sess-ctx", "chat", 78_000)
    _ctx_seed("sess-ctx", "compaction", 360_000)  # 最后一条 = 压缩调用
    data = client.get("/api/sessions/sess-ctx/usage").json()
    assert data["ok"]
    assert data["context"]["tokens"] == 78_000, (
        "context must reflect the last REAL conversation call, not the "
        f"compaction record: {data['context']}"
    )


def test_context_of_session_skips_sideband_records(client):
    """同口径外延：绕过会话 graph 的直连调用（标题/探活/记忆摘要/外部 agent/
    workflow 内部 agent/subagent 调度）的 input 都不代表本会话当前上下文；
    channels 投递（schedule/wecom/dingtalk…）跑的就是 session graph，保留。"""
    _ctx_seed("sess-side", "chat", 10_000)
    for src in ("title", "probe", "memory", "external", "workflow",
                "classifier", "mods", "subagent_gate", "subagent_plan"):
        _ctx_seed("sess-side", src, 900_000)
    _ctx_seed("sess-side", "schedule", 12_000)  # 真实 graph 调用 → 最新真值
    data = client.get("/api/sessions/sess-side/usage").json()
    assert data["context"]["tokens"] == 12_000

    # 只有非上下文记录 → context 为 None（环不显示），绝不显示假值
    _ctx_seed("sess-only-side", "title", 5_000)
    data2 = client.get("/api/sessions/sess-only-side/usage").json()
    assert data2["context"] is None


def test_context_no_sawtooth_after_manual_compact(
    create_session, ws_conv, client, isolated_home
):
    """ContextRing 锯齿（2026-11-07）复现·端到端：真实跑「两轮对话 + 手动
    /compact」。压缩总结直连 model.ainvoke（input=被压全量）record 进 store
    但命令 turn 不跑 graph、没有对话 record → 压缩 record 成为最后一条。
    修复前 GET /usage 的 context = 被压全量（≈压缩前占用，用户实测 360k/
    600k=60%），切会话 seed 高值、下轮 WS usage 帧又替换回低值（78k=13%）
    → 环交替闪烁。修复后 context 口径 = 最近一次对话调用，两源一致。
    回复文本刻意长：避免 graph 的退化回复 nudge 在同 turn 重调模型、
    额外消费 script 槽位。"""
    (isolated_home / "settings.json").write_text(
        json.dumps({"context": {"compact_keep_turns": 1}})
    )
    long_text = ("这一轮的回答内容足够长，不会触发图里的退化回复检测与重调机制，"
                 "保证每个 turn 恰好消费一个 script 槽位。")
    u1 = {"input_tokens": 5_000, "output_tokens": 10, "total_tokens": 5_010}
    u2 = {"input_tokens": 5_200, "output_tokens": 10, "total_tokens": 5_210}
    # 压缩总结调用的 usage：input=被压全量（远大于压缩后真实上下文）
    u_compact = {"input_tokens": 120_000, "output_tokens": 500,
                 "total_tokens": 120_500}
    sid = create_session(
        [
            script(text=long_text, usage=u1),
            script(text=long_text, usage=u2),
            script(text="summary", usage=u_compact),  # /compact 的 ainvoke
        ],
        agent_id="dev",
    )
    ws_ctx = []
    with ws_conv(sid) as conv:
        conv.invoke("第一个问题")
        ev1 = conv.recv_until("message.end", "error")
        conv.invoke("第二个问题")
        ev2 = conv.recv_until("message.end", "error")
        conv.invoke("/compact")
        events = conv.recv_until("message.end", "error")
        assert events_of(events, "notice"), "compact must have replied"
        # /compact 的命令 turn 不跑 graph → 不发 usage 帧（这正是压缩 record
        # 成为「最后一条」、旧口径取到假值的原因）
        assert not events_of(events, "usage")
        ws_ctx = [e["context"]["tokens"]
                  for evs in (ev1, ev2) for e in events_of(evs, "usage")
                  if e.get("context")]
    # WS 帧口径（engine _turn_ctx_tokens）：turn 内平滑后的对话真值
    assert ws_ctx == [5_000, 5_200], f"unexpected usage frames: {ws_ctx}"

    # API 口径（_context_of_session）：压缩 record（120k）不得顶替对话真值
    data = client.get(f"/api/sessions/{sid}/usage").json()
    assert data["context"]["tokens"] == 5_200, (
        f"GET /usage context must mirror the last conversation call, "
        f"not the compaction full-history input: {data['context']}"
    )


def test_provider_name_decorated(client):
    """提供商分布/模型分布/请求日志都附带 provider_name(人读名,未知 id 回退原值)
    ——2026-11-03 用户反馈:展示层直接画 prov_xxx id 没法认。"""
    _seed(provider="prov_abc123", model="glm-5")
    _seed(provider="custom", model="glm-4", session_id="sess-b")
    ov = client.get("/api/usage/overview?days=30").json()
    by_id = {p["provider"]: p for p in ov["providers"]}
    # isolated home 里 custom 槽位有缺省名(「自定义端点」/Custom endpoint);
    # prov_abc123 未配置 → 回退 id 本身
    assert by_id["custom"]["provider_name"]
    assert by_id["prov_abc123"]["provider_name"] == "prov_abc123"
    for m in ov["models"]:
        assert m["provider_name"] == (m["provider"] if m["provider"] == "prov_abc123" else m["provider_name"])
    rows = client.get("/api/usage/requests").json()["rows"]
    assert rows and all(r.get("provider_name") for r in rows)

"""turn 事件汇（api/stream/event_sink.py + 引擎/入站接线）单测。

覆盖（goal 验收逐条）：
- sink 未设置时零影响（真 turn 链 WS 帧序列对照）
- sink 收到事件 / 落盘形状严格 {ts,kind:"turn_event",event,data}
- delta 节流（fake clock 定量对比）而非 delta 即时保序 / close flush 尾字
- turn 结束注销（正常 + 崩溃路径）
- 并发两个 session 不串台（真引擎双 turn gather）
- sink 抛异常不影响主 turn（write 抛 + offer 抛两层）

集成部分走 GINNO_FAKE_LLM 脚本缝驱动**真** turn 引擎（零网络、不 mock 引擎），
与 test_a2a_inbound_exec.py 同款范式。路径隔离靠根 conftest isolated_home。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime.a2a import inbound_exec
from ginno_runtime.api.stream.event_sink import (
    DEFAULT_CHAR_LIMIT,
    TurnEventSink,
    get_sink,
    register_sink,
    unregister_sink,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _home_subdir(tmp_path, monkeypatch):
    """照 test_a2a_inbound_exec：exec_root 必须在 GINNO_HOME 之外。"""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _clean_sinks():
    """每个测试前后清 registry，防跨测试泄漏。"""
    from ginno_runtime.api.stream import event_sink as mod

    mod._SINKS.clear()
    yield
    mod._SINKS.clear()


class Recorder:
    def __init__(self):
        self.records: list[tuple[str, dict]] = []

    def write(self, event: str, data: dict) -> None:
        self.records.append((event, data))

    def events(self) -> list[str]:
        return [e for e, _ in self.records]

    def contents(self, kind: str) -> list[str]:
        return [d.get("content", "") for e, d in self.records if e == kind]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------------- #
# Part 1 — TurnEventSink 本体（fake clock，定量节流）
# --------------------------------------------------------------------------- #
def test_non_delta_immediate():
    rec = Recorder()
    sink = TurnEventSink(rec.write)
    sink.offer("tool.start", {"name": "bash"})
    assert rec.events() == ["tool.start"]  # 不等任何窗口
    sink.close()


def test_delta_throttled_quantitative():
    """1000 片 delta、模拟 10s 流（每片 10ms）：节流后 ~51 条 vs 不节流 1000 条。"""
    clock = FakeClock()
    rec = Recorder()
    sink = TurnEventSink(rec.write, interval_s=0.2, clock=clock)
    for i in range(1000):
        sink.offer("token.delta", {"content": f"x{i}"})
        clock.t += 0.01
    sink.close()
    throttled = len(rec.records)
    assert throttled <= 60, f"节流失效: {throttled} 条"
    assert "".join(rec.contents("token.delta")) == "".join(f"x{i}" for i in range(1000))

    # 对照组：interval=0 → 逐片直写（节流关闭时的行数基线）。
    rec2 = Recorder()
    sink2 = TurnEventSink(rec2.write, interval_s=0.0, clock=clock)
    for i in range(1000):
        sink2.offer("token.delta", {"content": f"x{i}"})
    sink2.close()
    assert len(rec2.records) == 1000
    assert throttled < len(rec2.records) / 10


def test_char_limit_flush():
    clock = FakeClock()  # 时钟不动 → 只能靠字符阈值触发
    rec = Recorder()
    sink = TurnEventSink(rec.write, interval_s=999, char_limit=100, clock=clock)
    sink.offer("token.delta", {"content": "首"})  # 首片即时
    sink.offer("token.delta", {"content": "a" * 60})
    assert len(rec.records) == 1  # 未达阈值，仍在缓冲
    sink.offer("token.delta", {"content": "b" * 60})  # 累计 120 ≥ 100 → flush
    assert len(rec.records) == 2
    assert rec.contents("token.delta")[1] == "a" * 60 + "b" * 60
    sink.close()


def test_non_delta_flushes_buffer_preserving_order():
    clock = FakeClock()
    rec = Recorder()
    sink = TurnEventSink(rec.write, interval_s=999, clock=clock)
    sink.offer("token.delta", {"content": "hello "})  # 首片即时
    sink.offer("token.delta", {"content": "world"})  # 缓冲
    sink.offer("tool.start", {"name": "bash"})  # 非 delta → 先 flush 再即时写
    assert rec.events() == ["token.delta", "token.delta", "tool.start"]
    assert rec.contents("token.delta") == ["hello ", "world"]
    sink.close()


def test_cross_kind_interleave_keeps_arrival_order():
    """token/thinking 交错：只合并连续同 kind，不重排。"""
    clock = FakeClock()
    rec = Recorder()
    sink = TurnEventSink(rec.write, interval_s=999, clock=clock)
    sink.offer("thinking.delta", {"content": "思1"})  # 首片即时
    sink.offer("thinking.delta", {"content": "思2"})
    sink.offer("token.delta", {"content": "文1"})
    sink.offer("token.delta", {"content": "文2"})
    sink.offer("thinking.delta", {"content": "思3"})
    sink.close()
    assert rec.events() == [
        "thinking.delta", "thinking.delta", "token.delta", "thinking.delta",
    ]
    assert rec.contents("thinking.delta") == ["思1", "思2", "思3"]
    assert rec.contents("token.delta") == ["文1文2"]


def test_close_flushes_tail_and_idempotent():
    clock = FakeClock()
    rec = Recorder()
    sink = TurnEventSink(rec.write, interval_s=999, clock=clock)
    sink.offer("token.delta", {"content": "首"})
    sink.offer("token.delta", {"content": "尾字"})
    assert rec.contents("token.delta") == ["首"]  # 尾字还在缓冲
    sink.close()
    assert rec.contents("token.delta") == ["首", "尾字"]  # 不丢尾字
    sink.close()  # 幂等
    assert rec.contents("token.delta") == ["首", "尾字"]


def test_write_exception_swallowed():
    calls = []

    def bad_write(event, data):
        calls.append(event)
        raise RuntimeError("disk on fire")

    sink = TurnEventSink(bad_write)
    sink.offer("token.delta", {"content": "a"})  # 绝不抛
    sink.offer("token.delta", {"content": "b"})
    sink.offer("tool.start", {"name": "x"})
    sink.close()
    # a 首片直写、b 缓冲后随 tool.start 前 flush、tool.start 即时——各尝试一次。
    assert calls == ["token.delta", "token.delta", "tool.start"]


def test_keepalive_skipped_and_empty_delta_ignored():
    rec = Recorder()
    sink = TurnEventSink(rec.write)
    sink.offer("keepalive", {})
    sink.offer("token.delta", {"content": ""})
    sink.offer("", {})
    sink.close()
    assert rec.records == []


def test_registry_session_scope_and_identity():
    a, b = Recorder(), Recorder()
    sa = TurnEventSink(a.write)
    sb = TurnEventSink(b.write)
    register_sink("sess-A", sa)
    register_sink("sess-B", sb)
    assert get_sink("sess-A") is sa
    assert get_sink("sess-B") is sb
    assert get_sink("sess-none") is None
    # 对象同一性校验：旧 sink 的注销不得误删新注册的 sink。
    newer = TurnEventSink(Recorder().write)
    register_sink("sess-A", newer)
    unregister_sink("sess-A", sa)  # 旧对象 → 不删
    assert get_sink("sess-A") is newer
    unregister_sink("sess-A", newer)
    assert get_sink("sess-A") is None
    assert get_sink("sess-B") is sb  # 邻居不受影响
    unregister_sink("sess-B", sb)


# --------------------------------------------------------------------------- #
# Part 2 — 真 turn 引擎集成（GINNO_FAKE_LLM 脚本缝，零网络）
# --------------------------------------------------------------------------- #
def _policy(tmp_path: Path, **over) -> dict:
    base = {
        "trust": "readonly",
        "caps": ["L0"],
        "exec_root": str(tmp_path / "ws"),
        "slug": "a2a-inbound",
        "title_prefix": "A2A · ",
        "timeout_s": 30,
    }
    base.update(over)
    return base


def _set_scripts(tmp_path, monkeypatch, scripts, tag="job"):
    scripts_file = tmp_path / f"scripts-{tag}.json"
    scripts_file.write_text(json.dumps(scripts), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts_file))


async def _run_job(tmp_path, monkeypatch, scripts, policy, *, context_id="ctx-1"):
    _set_scripts(tmp_path, monkeypatch, scripts)
    job = {
        "instruction": "do the task",
        "context_id": context_id,
        "session_id": "",
        "peer_name": "tester",
        "policy": policy,
    }
    return await inbound_exec._run_job(job)


class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    def frames(self) -> list[dict]:
        return [json.loads(s) for s in self.sent]


async def _make_session(tmp_path, monkeypatch, scripts, *, peer="tester"):
    """建一个真 a2a 入站会话（graph 用 fake LLM），供直驱 _run_stream。"""
    from ginno_runtime import subagent_types
    from ginno_runtime.agents import registry as agents_reg

    _set_scripts(tmp_path, monkeypatch, scripts, tag=peer)
    paths.ensure_layout()
    subagent_types.ensure_seeded()
    agents_reg.ensure_seeded()
    policy = inbound_exec.ExecPolicy.from_dict(_policy(tmp_path))
    gd = inbound_exec.GuardHookDispatcher(policy)
    session, sid = await inbound_exec._prepare_session(
        policy, gd, session_id=None, peer_name=peer
    )
    return session, sid


def _config(session, sid, text):
    return {
        "configurable": {
            "thread_id": sid,
            "project_slug": session["project_slug"],
            "agent_id": session.get("agent_id"),
            "turn_id": uuid.uuid4().hex,
            "user_text": text,
            "usage_source": "a2a-inbound",
        }
    }


async def _drive_turn(session, sid, text):
    from ginno_runtime.api import stream as stream_api

    await stream_api._run_stream(
        None, session["graph"], _config(session, sid, text), text,
        session, session.get("agent_id"), origin="schedule",
    )


async def test_run_job_writes_turn_event_records(tmp_path, monkeypatch):
    """落盘形状契约 + 工具/流式事件齐全 + 保序 + turn 结束注销。"""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "note.txt"
    target.write_text("file body", encoding="utf-8")
    prog = tmp_path / "progress.jsonl"
    inbound_exec.set_progress_path(str(prog))
    try:
        scripts = [
            {"content": "", "tool_calls": [
                {"name": "read_file", "args": {"path": str(target)}}
            ]},
            {"content": "done reading"},
        ]
        result = await _run_job(tmp_path, monkeypatch, scripts, _policy(tmp_path))
    finally:
        inbound_exec.set_progress_path("")
    assert result["state"] == "completed"
    sid = result["session_id"]
    assert get_sink(sid) is None, "turn 结束必须注销 sink"

    recs = [json.loads(line) for line in prog.read_text().splitlines() if line.strip()]
    turn_events = [r for r in recs if r.get("kind") == "turn_event"]
    assert turn_events, "进度文件里必须有 turn_event 记录"
    # 形状契约：严格 {ts, kind, event, data}，ts 为 float。
    for r in turn_events:
        assert set(r.keys()) == {"ts", "kind", "event", "data"}, r
        assert isinstance(r["ts"], float)
        assert isinstance(r["event"], str) and r["event"]
        assert isinstance(r["data"], dict)
    names = [r["event"] for r in turn_events]
    for expected in ("turn.start", "tool.start", "tool.end", "token.delta", "message.end"):
        assert expected in names, f"缺事件 {expected}: {names}"
    # 工具事件内容 + 保序：tool.start < tool.end < token.delta < message.end。
    ts = next(r for r in turn_events if r["event"] == "tool.start")
    assert ts["data"].get("name") == "read_file"
    assert names.index("tool.start") < names.index("tool.end")
    assert names.index("tool.end") < names.index("token.delta")
    assert names.index("token.delta") < names.index("message.end")
    # 流式文本完整（节流不丢字）。
    text = "".join(
        r["data"].get("content", "") for r in turn_events if r["event"] == "token.delta"
    )
    assert text == "done reading"
    # 既有进度机制不受影响（state working/final 仍在）。
    assert any(r.get("kind") == "state" for r in recs)
    # keepalive 不落盘。
    assert "keepalive" not in names


async def test_run_job_unregisters_sink_on_crash(tmp_path, monkeypatch):
    """turn 崩溃路径也必须注销（finally），且任务转 failed 不带走 worker。"""
    from ginno_runtime.api import stream as stream_api

    _set_scripts(tmp_path, monkeypatch, [{"content": "x"}])

    async def boom(*a, **kw):
        raise RuntimeError("turn exploded")

    monkeypatch.setattr(stream_api, "_run_stream", boom)
    result = await inbound_exec._run_job({
        "instruction": "t", "context_id": "ctx-crash", "session_id": "",
        "peer_name": "tester", "policy": _policy(tmp_path),
    })
    assert result["state"] == "failed"
    assert get_sink(result["session_id"]) is None


async def test_no_sink_zero_impact_frame_equivalence(tmp_path, monkeypatch):
    """同一会话连跑两个相同脚本 turn：无 sink vs 有 sink，WS 帧序列必须一致。"""
    from ginno_runtime import server_shared as shared

    session, sid = await _make_session(
        tmp_path, monkeypatch, [{"content": "hello world"}, {"content": "hello world"}]
    )
    ws = _FakeWS()
    shared._SESSION_WS[sid] = [ws]
    try:
        # turn 1：无 sink（本地桌面主链路的现状）。
        assert get_sink(sid) is None
        await _drive_turn(session, sid, "task one")
        frames1 = ws.frames()
        ws.sent.clear()
        # turn 2：注册 sink（只记录，不改变主链）。
        rec = Recorder()
        sink = TurnEventSink(rec.write)
        register_sink(sid, sink)
        try:
            await _drive_turn(session, sid, "task two")
        finally:
            sink.close()
            unregister_sink(sid, sink)
        frames2 = ws.frames()
    finally:
        shared._SESSION_WS.pop(sid, None)

    names1 = [f.get("event") for f in frames1]
    names2 = [f.get("event") for f in frames2]
    assert names1 == names2, f"sink 改变了主链帧序列:\n{names1}\n{names2}"
    assert "message.end" in names1
    text1 = "".join(f.get("content", "") for f in frames1 if f.get("event") == "token.delta")
    text2 = "".join(f.get("content", "") for f in frames2 if f.get("event") == "token.delta")
    assert text1 == text2 == "hello world"
    # sink 确实收到了同一 turn 的事件（不是空转）。
    assert "turn.start" in rec.events()
    assert "message.end" in rec.events()


async def test_concurrent_sessions_no_crosstalk(tmp_path, monkeypatch):
    """两个 session 并发跑真引擎：各自 sink 只收自己 session 的帧。"""
    from ginno_runtime import server_shared as shared

    sa, sida = await _make_session(
        tmp_path, monkeypatch, [{"content": "alpha reply"}], peer="peer-A"
    )
    sb, sidb = await _make_session(
        tmp_path, monkeypatch, [{"content": "beta reply"}], peer="peer-B"
    )
    assert sida != sidb
    ra, rb = Recorder(), Recorder()
    sink_a = TurnEventSink(ra.write)
    sink_b = TurnEventSink(rb.write)
    register_sink(sida, sink_a)
    register_sink(sidb, sink_b)
    try:
        await asyncio.gather(
            _drive_turn(sa, sida, "task A"),
            _drive_turn(sb, sidb, "task B"),
        )
    finally:
        sink_a.close()
        sink_b.close()
        unregister_sink(sida, sink_a)
        unregister_sink(sidb, sink_b)
        shared._SESSION_WS.pop(sida, None)
        shared._SESSION_WS.pop(sidb, None)

    for rec, sid, text in ((ra, sida, "alpha reply"), (rb, sidb, "beta reply")):
        assert rec.records, f"{sid} 的 sink 没收到事件"
        assert all(d.get("frame_session") == sid for _, d in rec.records), (
            f"{sid} 的 sink 收到了别的 session 的帧（串台）"
        )
        assert "turn.start" in rec.events()
        assert "message.end" in rec.events()
        joined = "".join(rec.contents("token.delta"))
        assert joined == text, f"{sid} 文本串台: {joined!r}"
    assert get_sink(sida) is None and get_sink(sidb) is None


async def test_sink_offer_exception_does_not_break_turn(tmp_path, monkeypatch):
    """恶意 sink（offer 直接抛）也不得影响主 turn——引擎侧兜底。"""
    from ginno_runtime import server_shared as shared

    session, sid = await _make_session(
        tmp_path, monkeypatch, [{"content": "still fine"}]
    )

    class _BoomSink:
        def offer(self, event, data):
            raise RuntimeError("sink exploded")

    ws = _FakeWS()
    shared._SESSION_WS[sid] = [ws]
    boom = _BoomSink()
    register_sink(sid, boom)
    try:
        await _drive_turn(session, sid, "task")
    finally:
        unregister_sink(sid, boom)
        shared._SESSION_WS.pop(sid, None)
    names = [f.get("event") for f in ws.frames()]
    assert "message.end" in names, f"主 turn 被 sink 异常打断: {names}"
    text = "".join(f.get("content", "") for f in ws.frames() if f.get("event") == "token.delta")
    assert text == "still fine"


async def test_sink_write_exception_does_not_break_turn(tmp_path, monkeypatch):
    """write 回调抛（磁盘故障）：turn 照常完成，sink 异常被内部吞掉。"""
    from ginno_runtime import server_shared as shared

    session, sid = await _make_session(
        tmp_path, monkeypatch, [{"content": "resilient"}]
    )

    def bad_write(event, data):
        raise OSError("disk full")

    ws = _FakeWS()
    shared._SESSION_WS[sid] = [ws]
    sink = TurnEventSink(bad_write)
    register_sink(sid, sink)
    try:
        await _drive_turn(session, sid, "task")
    finally:
        sink.close()
        unregister_sink(sid, sink)
        shared._SESSION_WS.pop(sid, None)
    names = [f.get("event") for f in ws.frames()]
    assert "message.end" in names
    assert get_sink(sid) is None


def test_default_char_limit_sane():
    assert DEFAULT_CHAR_LIMIT >= 1024

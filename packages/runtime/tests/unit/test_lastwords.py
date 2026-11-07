"""遗言模式（docs/lastwords-design.md）单测。

覆盖：快照组装（§2 字段/注入消息排除）、write/consume round-trip、
TTL 过期、exactly-once（consume 后文件删）、session 不存在静默丢弃、
空标记、WAL 节流、prepare API（TestClient + 伪造活跃 turn）、恢复流程
（notify aupdate_state 注入 / auto headless turn / off / 安全分级——
auto 只对 reason=dev-swap 生效）、band/continuation 渲染格式。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime import lastwords, server_shared
from ginno_runtime.api import devswap

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

MSGS = [
    HumanMessage(content="早前的问题"),
    AIMessage(content="早前的回答"),
    HumanMessage(content="帮我跑一遍 dev-swap 验证遗言模式"),
    AIMessage(
        content="正在构建 runtime bundle……",
        tool_calls=[{"name": "bash", "args": {"command": "make app"}, "id": "tc1"}],
    ),
]


def _fake_loader(slug: str, sid: str) -> list:
    return list(MSGS)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    server_shared._RUNNING_TURNS.clear()
    lastwords._LAST_WRITE_AT.clear()
    lastwords._EFFORT.clear()
    lastwords._SHUTTING_DOWN = False
    yield
    server_shared._RUNNING_TURNS.clear()
    lastwords._LAST_WRITE_AT.clear()
    lastwords._EFFORT.clear()
    lastwords._SHUTTING_DOWN = False


@pytest.fixture
def api_client():
    app = FastAPI()
    app.include_router(devswap.router)
    return TestClient(app, client=("127.0.0.1", 51000))


def _write_valid(sid: str, reason: str = "dev-swap", **over) -> dict:
    snap = lastwords.snapshot_from_messages(
        sid, "default", MSGS, reason=reason, fingerprint_old="abc123def456"
    )
    snap.update(over)
    lastwords._write_atomic(lastwords._path(sid), snap)
    return snap


# --------------------------------------------------------------------------- #
# 设置读取
# --------------------------------------------------------------------------- #


class TestMode:
    def test_default_notify(self, isolated_home: Path):
        assert lastwords.mode() == "notify"

    def test_reads_settings(self, isolated_home: Path):
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"mode": "auto"}}})
        )
        assert lastwords.mode() == "auto"

    def test_invalid_falls_back(self, isolated_home: Path):
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"mode": "yolo"}}})
        )
        assert lastwords.mode() == "notify"

    def test_missing_settings_file(self, isolated_home: Path):
        assert lastwords.mode() == "notify"


# --------------------------------------------------------------------------- #
# 快照组装（§2）
# --------------------------------------------------------------------------- #


class TestSnapshot:
    def test_fields(self):
        snap = lastwords.snapshot_from_messages(
            "s1", "default", MSGS, reason="dev-swap", fingerprint_old="fp1"
        )
        assert snap["session_id"] == "s1"
        assert snap["project"] == "default"
        assert snap["reason"] == "dev-swap"
        assert snap["fingerprint_old"] == "fp1"
        assert snap["user_request_head"] == "帮我跑一遍 dev-swap 验证遗言模式"
        assert "构建" in snap["last_agent_message"]
        assert snap["running_tools"] == ["bash"]
        assert snap["ts"] > 0

    def test_injected_messages_skipped_for_user_head(self):
        msgs = [
            HumanMessage(content="真实请求"),
            AIMessage(content="工作中"),
            HumanMessage(
                content="[task finished] noise",
                additional_kwargs={"ginno_task_event": {"task": "t_1"}},
            ),
            HumanMessage(
                content="[lastwords] noise",
                additional_kwargs={"ginno_lastwords": {"session_id": "s"}},
            ),
        ]
        snap = lastwords.snapshot_from_messages("s", "default", msgs, reason="signal")
        assert snap["user_request_head"] == "真实请求"

    def test_head_truncated_to_500(self):
        msgs = [HumanMessage(content="x" * 800)]
        snap = lastwords.snapshot_from_messages("s", "default", msgs, reason="signal")
        assert len(snap["user_request_head"]) == 500

    def test_multimodal_content(self):
        msgs = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ]
            )
        ]
        snap = lastwords.snapshot_from_messages("s", "default", msgs, reason="signal")
        assert snap["user_request_head"] == "看这张图"


# --------------------------------------------------------------------------- #
# write / consume round-trip + TTL + exactly-once
# --------------------------------------------------------------------------- #


class TestWriteConsume:
    def test_round_trip(self, monkeypatch: pytest.MonkeyPatch, isolated_home: Path):
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        server_shared._RUNNING_TURNS["s1"] = "turn-1"
        n = lastwords.write_lastwords("dev-swap", fingerprint_old="fp9")
        assert n == 1
        entries = lastwords.consume_lastwords()
        assert len(entries) == 1
        snap = entries[0]
        assert snap["session_id"] == "s1"
        assert snap["reason"] == "dev-swap"
        assert snap["turn_id"] == "turn-1"
        assert snap["user_request_head"] == "帮我跑一遍 dev-swap 验证遗言模式"
        # consume 不删——恢复流程处置后 drop
        assert lastwords._path("s1").exists()
        lastwords.drop_lastwords("s1")
        assert not lastwords._path("s1").exists()
        assert lastwords.consume_lastwords() == []

    def test_no_active_turn_writes_marker(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        n = lastwords.write_lastwords("dev-swap")
        assert n == 0
        marker = lastwords.lastwords_dir() / "_marker.json"
        assert marker.exists()
        doc = json.loads(marker.read_text())
        assert doc["reason"] == "dev-swap"
        # marker 消费即删，不产出注入条目
        assert lastwords.consume_lastwords() == []
        assert not marker.exists()

    def test_running_reason_no_marker_when_idle(self, monkeypatch: pytest.MonkeyPatch):
        # WAL 写（reason=running）在空闲时不落 marker——它不是显式预告
        n = lastwords.write_lastwords("running")
        assert n == 0
        assert not (lastwords.lastwords_dir() / "_marker.json").exists()

    def test_ttl_expired_dropped(self, isolated_home: Path):
        _write_valid("s-old", ts=int(time.time()) - lastwords.TTL_S - 5)
        assert lastwords.consume_lastwords() == []
        assert not lastwords._path("s-old").exists()

    def test_within_ttl_kept(self):
        _write_valid("s-new", ts=int(time.time()) - 60)
        assert len(lastwords.consume_lastwords()) == 1

    def test_corrupt_file_dropped(self):
        lastwords.lastwords_dir().mkdir(parents=True, exist_ok=True)
        (lastwords.lastwords_dir() / "s-bad.json").write_text("{not json")
        assert lastwords.consume_lastwords() == []
        assert not (lastwords.lastwords_dir() / "s-bad.json").exists()

    def test_missing_session_id_dropped(self):
        _write_valid("s-anon", session_id="")
        assert lastwords.consume_lastwords() == []

    def test_multi_sessions(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", lambda slug, sid: list(MSGS))
        server_shared._RUNNING_TURNS.update({"s1": "t1", "s2": "t2"})
        assert lastwords.write_lastwords("signal") == 2
        entries = lastwords.consume_lastwords()
        assert {e["session_id"] for e in entries} == {"s1", "s2"}


# --------------------------------------------------------------------------- #
# WAL 节流
# --------------------------------------------------------------------------- #


class TestWalThrottle:
    def test_tool_boundary_throttled(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords._LAST_WRITE_AT["s1"] = time.monotonic()  # 刚写过
        lastwords.note_tool_boundary("s1", "t1", "default", MSGS)
        assert not lastwords._path("s1").exists()
        # 间隔已过 → 写
        lastwords._LAST_WRITE_AT["s1"] = time.monotonic() - 10
        lastwords.note_tool_boundary("s1", "t1", "default", MSGS)
        assert lastwords._path("s1").exists()
        snap = json.loads(lastwords._path("s1").read_text())
        assert snap["reason"] == "running"

    def test_turn_start_writes_immediately(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords.note_turn_start("s2", "t9", "default")
        assert lastwords._path("s2").exists()

    def test_turn_end_removes_file(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords.note_turn_start("s3", "t3", "default")
        assert lastwords._path("s3").exists()
        lastwords.note_turn_end("s3", "t3")
        assert not lastwords._path("s3").exists()
        assert "s3" not in lastwords._LAST_WRITE_AT


# --------------------------------------------------------------------------- #
# prepare API
# --------------------------------------------------------------------------- #


class TestPrepareApi:
    def test_prepare_writes_snapshot(self, api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        server_shared._RUNNING_TURNS["sess-live"] = "turn-x"
        r = api_client.post("/api/devswap/lastwords/prepare", json={"fingerprint_old": "fp-old"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["count"] == 1 and body["mode"] == "notify"
        snap = json.loads(lastwords._path("sess-live").read_text())
        assert snap["reason"] == "dev-swap"
        assert snap["fingerprint_old"] == "fp-old"

    def test_prepare_idle_writes_marker(self, api_client: TestClient):
        r = api_client.post("/api/devswap/lastwords/prepare", json={})
        assert r.json() == {"ok": True, "count": 0, "mode": "notify"}
        assert (lastwords.lastwords_dir() / "_marker.json").exists()

    def test_prepare_local_only(self):
        app = FastAPI()
        app.include_router(devswap.router)
        with TestClient(app) as c:  # 默认 client host=“testclient”=非本机
            r = c.post("/api/devswap/lastwords/prepare", json={})
        assert r.json().get("error") == "local-only"


# --------------------------------------------------------------------------- #
# 恢复流程
# --------------------------------------------------------------------------- #


class FakeGraph:
    def __init__(self):
        self.updates: list[tuple[dict, dict]] = []

    async def aupdate_state(self, config, update, as_node=None):
        self.updates.append((config, update))


def _fake_session(sid: str, graph: FakeGraph) -> dict:
    return {
        "session_id": sid,
        "project_slug": "default",
        "graph": graph,
        "agent_id": "agent-1",
    }


class TestRecover:
    async def test_notify_injects_and_drops(self, monkeypatch: pytest.MonkeyPatch):
        _write_valid("s-rec", reason="dev-swap")
        g = FakeGraph()
        monkeypatch.setattr(
            "ginno_runtime.api.sessions._ensure_session",
            lambda sid: _fake_session(sid, g),
        )
        pushed: list[tuple[str, dict]] = []

        async def fake_push(event, data):
            pushed.append((event, data))

        monkeypatch.setattr(server_shared, "_push_global_event", fake_push)
        n = await lastwords.recover_lastwords()
        assert n == 1
        # aupdate_state 追加了一条 HumanMessage（带 ginno_task_event 戳）
        assert len(g.updates) == 1
        cfg, upd = g.updates[0]
        assert cfg["configurable"]["thread_id"] == "s-rec"
        msg = upd["messages"][0]
        assert msg.content.startswith("[lastwords]")
        kw = msg.additional_kwargs["ginno_task_event"]
        assert kw["kind"] == "lastwords" and kw["task"].startswith("lastwords-")
        # exactly-once：文件已删
        assert not lastwords._path("s-rec").exists()
        # 通知中心广播
        assert pushed and pushed[0][0] == "runtime.lastwords_restored"
        assert pushed[0][1]["session_id"] == "s-rec"

    async def test_session_gone_silent_drop(self, monkeypatch: pytest.MonkeyPatch):
        _write_valid("s-gone")
        monkeypatch.setattr("ginno_runtime.api.sessions._ensure_session", lambda sid: None)
        n = await lastwords.recover_lastwords()
        assert n == 0
        assert not lastwords._path("s-gone").exists()

    async def test_off_mode_drops_without_inject(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"mode": "off"}}})
        )
        _write_valid("s-off")
        g = FakeGraph()
        monkeypatch.setattr(
            "ginno_runtime.api.sessions._ensure_session",
            lambda sid: _fake_session(sid, g),
        )
        assert await lastwords.recover_lastwords() == 0
        assert not g.updates
        assert not lastwords._path("s-off").exists()

    async def test_auto_dev_swap_runs_headless_turn(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"mode": "auto"}}})
        )
        _write_valid("s-auto", reason="dev-swap")
        monkeypatch.setattr(
            "ginno_runtime.api.sessions._ensure_session",
            lambda sid: _fake_session(sid, FakeGraph()),
        )
        calls: list[dict] = []

        async def fake_run_stream(ws, graph, config, text, session, agent_id, **kw):
            calls.append({"config": config, "text": text, "kw": kw})

        monkeypatch.setattr("ginno_runtime.api.stream._run_stream", fake_run_stream)
        import ginno_runtime.api.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_run_stream", fake_run_stream)
        n = await lastwords.recover_lastwords()
        assert n == 1
        assert len(calls) == 1
        cfg = calls[0]["config"]["configurable"]
        assert cfg["thread_id"] == "s-auto"
        assert cfg["usage_source"] == "lastwords"
        assert calls[0]["text"].startswith("[lastwords]")
        assert "原始请求" in calls[0]["text"]
        assert calls[0]["kw"]["origin"] == "lastwords"
        assert calls[0]["kw"]["user_extra_kwargs"]["ginno_task_event"]["kind"] == "lastwords"
        # headless turn 收尾清旗
        assert "s-auto" not in server_shared._RUNNING_TURNS

    async def test_auto_signal_stays_notify(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """安全分级（§4）：auto 只对 reason=dev-swap 生效，signal/running 走 notify。"""
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"mode": "auto"}}})
        )
        _write_valid("s-sig", reason="signal")
        g = FakeGraph()
        monkeypatch.setattr(
            "ginno_runtime.api.sessions._ensure_session",
            lambda sid: _fake_session(sid, g),
        )
        stream_calls: list = []

        async def fake_run_stream(*a, **kw):
            stream_calls.append(1)

        import ginno_runtime.api.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_run_stream", fake_run_stream)
        assert await lastwords.recover_lastwords() == 1
        assert g.updates and not stream_calls  # 注入了折叠带，没跑 turn

    async def test_auto_signal_subagent_runs_headless_turn(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """首跑追加：subagent 会话（headless）+ app 重启（signal）→ 自动续跑。

        人工会话续 notify、subagent 放宽到 signal/running——子会话无人点
        「继续」，自动续跑零打扰；归因 origin=lastwords。
        """
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"mode": "auto"}}})
        )
        ts = time.time()
        _write_valid(
            "s-sub",
            reason="signal",
            ts=int(ts),
            effort=_effort(5, 200.0, ts=ts),  # 门槛正交：高价值才走到续跑
        )

        def fake_ensure(sid: str) -> dict:
            s = _fake_session(sid, FakeGraph())
            s["type"] = "subagent"
            s["parent_session_id"] = "s-parent"
            s["depth"] = 0
            return s

        monkeypatch.setattr("ginno_runtime.api.sessions._ensure_session", fake_ensure)
        calls: list[dict] = []

        async def fake_run_stream(ws, graph, config, text, session, agent_id, **kw):
            calls.append({"config": config, "kw": kw})

        import ginno_runtime.api.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_run_stream", fake_run_stream)
        assert await lastwords.recover_lastwords() == 1
        assert len(calls) == 1
        assert calls[0]["config"]["configurable"]["usage_source"] == "lastwords"
        assert calls[0]["kw"]["origin"] == "lastwords"

    async def test_auto_running_subagent_runs_headless_turn(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """WAL 残留 running（kill_stale SIGKILL 场景）的 subagent 遗言同样续跑。"""
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"mode": "auto"}}})
        )
        _write_valid("s-sub2", reason="running")

        def fake_ensure(sid: str) -> dict:
            s = _fake_session(sid, FakeGraph())
            s["type"] = "subagent"
            return s

        monkeypatch.setattr("ginno_runtime.api.sessions._ensure_session", fake_ensure)
        calls: list = []

        async def fake_run_stream(*a, **kw):
            calls.append(kw)

        import ginno_runtime.api.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_run_stream", fake_run_stream)
        assert await lastwords.recover_lastwords() == 1
        assert len(calls) == 1

    async def test_notify_mode_subagent_stays_notify(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """mode=notify（默认）下 subagent 也不自动续跑——放宽只作用于 auto。"""
        _write_valid("s-sub3", reason="signal")

        def fake_ensure(sid: str) -> dict:
            s = _fake_session(sid, g)
            s["type"] = "subagent"
            return s

        g = FakeGraph()
        monkeypatch.setattr("ginno_runtime.api.sessions._ensure_session", fake_ensure)
        stream_calls: list = []

        async def fake_run_stream(*a, **kw):
            stream_calls.append(1)

        import ginno_runtime.api.stream as stream_mod

        monkeypatch.setattr(stream_mod, "_run_stream", fake_run_stream)
        assert await lastwords.recover_lastwords() == 1
        assert g.updates and not stream_calls

    def test_low_effort_subagent_dropped_before_auto(self, isolated_home: Path):
        """门槛正交：低价值 subagent 遗言（0 工具/短时长）续跑前就被 consume 丢弃。"""
        ts = time.time()
        _write_valid(
            "s-sub-low",
            reason="signal",
            ts=int(ts),
            effort=_effort(0, 8.0, ts=ts),
        )
        assert lastwords.consume_lastwords() == []
        assert not lastwords._path("s-sub-low").exists()

    def test_is_subagent_session_dict_and_meta(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """判据：session dict 的 type 字段优先，缺失回落磁盘 meta。"""
        assert lastwords.is_subagent_session({"type": "subagent"}) is True
        assert lastwords.is_subagent_session({"type": "chat"}) is False
        assert lastwords.is_subagent_session(None) is False
        # dict 无 type → 回落 session_meta._find_meta（磁盘 index 的真源）
        import ginno_runtime.session_meta as sm

        monkeypatch.setattr(sm, "_find_meta", lambda sid: None)
        assert lastwords.is_subagent_session({"session_id": "s-x"}) is False
        monkeypatch.setattr(sm, "_find_meta", lambda sid: ({"type": "subagent"}, "default"))
        assert lastwords.is_subagent_session({"session_id": "s-x"}) is True


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


class TestRender:
    def test_band_format(self):
        snap = {
            "reason": "dev-swap",
            "user_request_head": "帮我跑一遍 dev-swap 验证遗言模式",
            "fingerprint_old": "abcdef1234567890",
            "bg_tasks": ["t_1", "t_2"],
        }
        band = lastwords.render_band(snap)
        assert band.startswith("[lastwords] dev-swap 重启恢复：")
        assert "帮我跑一遍" in band
        assert "旧指纹 abcdef123456" in band
        assert "后台任务 2 个" in band

    def test_band_truncates_long_head(self):
        band = lastwords.render_band({"reason": "signal", "user_request_head": "长" * 200})
        assert "…" in band
        assert len(band) < 160

    def test_continuation_mentions_all_sections(self):
        text = lastwords.render_continuation(
            {
                "user_request_head": "修复构建",
                "last_agent_message": "跑到单测阶段",
                "bg_tasks": ["t_9"],
            }
        )
        assert text.startswith("[lastwords]")
        assert "修复构建" in text
        assert "跑到单测阶段" in text
        assert "t_9" in text


# --------------------------------------------------------------------------- #
# 首跑质量修（2026-11-07）：恢复价值门槛 / running TTL 收紧 / bg 实时核对 /
# WAL effort 计数 / 优雅关闭保护
# --------------------------------------------------------------------------- #


def _effort(tool_calls: int, elapsed_s: float, *, ts: float | None = None) -> dict:
    """构造与快照 ts 相对偏移的 effort（started_at = ts - elapsed）。"""
    t = time.time() if ts is None else ts
    return {"turn_id": "t-e", "started_at": t - elapsed_s, "tool_calls": tool_calls}


class TestEffortGate:
    """门槛过滤矩阵（consume 时判定，低价值静默丢弃删文件）。"""

    def test_running_chitchat_dropped(self, isolated_home: Path):
        """首跑实况复现：闲聊 turn（0 工具、10s）→ 静默丢弃不注入。"""
        ts = time.time()
        _write_valid("s-chat", reason="running", ts=int(ts), effort=_effort(0, 10.0, ts=ts))
        assert lastwords.consume_lastwords() == []
        assert not lastwords._path("s-chat").exists()

    def test_running_min_tool_calls_kept(self):
        ts = time.time()
        _write_valid("s-tools", reason="running", ts=int(ts), effort=_effort(3, 5.0, ts=ts))
        entries = lastwords.consume_lastwords()
        assert len(entries) == 1 and entries[0]["session_id"] == "s-tools"

    def test_running_min_duration_kept(self):
        """0 工具但长 turn（纯生成 ≥120s）也值得恢复——时长维度放行。"""
        ts = time.time()
        _write_valid("s-long", reason="running", ts=int(ts), effort=_effort(0, 121.0, ts=ts))
        assert len(lastwords.consume_lastwords()) == 1

    def test_running_below_duration_dropped(self):
        ts = time.time()
        _write_valid("s-mid", reason="running", ts=int(ts), effort=_effort(1, 110.0, ts=ts))
        assert lastwords.consume_lastwords() == []

    def test_explicit_reason_halved_gate(self):
        """dev-swap/signal 门槛减半（3→2 工具 / 120→60s）：2 工具放行。"""
        ts = time.time()
        _write_valid("s-halved", reason="dev-swap", ts=int(ts), effort=_effort(2, 5.0, ts=ts))
        assert len(lastwords.consume_lastwords()) == 1

    def test_explicit_reason_halved_duration(self):
        ts = time.time()
        _write_valid("s-hd", reason="signal", ts=int(ts), effort=_effort(0, 61.0, ts=ts))
        assert len(lastwords.consume_lastwords()) == 1

    def test_explicit_reason_still_drops_low(self):
        """减半后仍低于门槛（1 工具、30s）→ 丢弃。"""
        ts = time.time()
        _write_valid("s-low", reason="dev-swap", ts=int(ts), effort=_effort(1, 30.0, ts=ts))
        assert lastwords.consume_lastwords() == []

    def test_legacy_snapshot_without_effort_passes(self):
        """旧版本快照无 effort 信号 → 保守放行（不可判定的不误杀）。"""
        _write_valid("s-legacy", reason="running")
        assert "effort" not in json.loads(lastwords._path("s-legacy").read_text())
        assert len(lastwords.consume_lastwords()) == 1

    def test_settings_override(self, isolated_home: Path):
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"min_effort": {"tool_calls": 5}}}})
        )
        ts = time.time()
        _write_valid("s-3", reason="running", ts=int(ts), effort=_effort(3, 5.0, ts=ts))
        _write_valid("s-5", reason="running", ts=int(ts), effort=_effort(5, 5.0, ts=ts))
        got = {e["session_id"] for e in lastwords.consume_lastwords()}
        assert got == {"s-5"} and not lastwords._path("s-3").exists()

    def test_settings_zero_disables_tool_gate(self, isolated_home: Path):
        """tool_calls=0 关闭工具维度门槛（时长维度仍生效）。"""
        (isolated_home / "settings.json").write_text(
            json.dumps({"devswap": {"lastwords": {"min_effort": {"tool_calls": 0}}}})
        )
        ts = time.time()
        _write_valid("s-any", reason="running", ts=int(ts), effort=_effort(1, 5.0, ts=ts))
        assert len(lastwords.consume_lastwords()) == 1

    def test_min_effort_reads_settings(self, isolated_home: Path):
        assert lastwords.min_effort() == (3, 120.0)
        (isolated_home / "settings.json").write_text(
            json.dumps(
                {"devswap": {"lastwords": {"min_effort": {"tool_calls": 7, "turn_seconds": 60}}}}
            )
        )
        assert lastwords.min_effort() == (7, 60.0)


class TestRunningTtl:
    """reason=running TTL 收紧到 15min（SIGKILL 残留过期更快）。"""

    def test_running_expired_at_16min(self):
        _write_valid(
            "s-r16",
            reason="running",
            ts=int(time.time()) - int(lastwords.RUNNING_TTL_S) - 60,
        )
        assert lastwords.consume_lastwords() == []
        assert not lastwords._path("s-r16").exists()

    def test_running_kept_at_14min(self):
        _write_valid(
            "s-r14",
            reason="running",
            ts=int(time.time()) - int(lastwords.RUNNING_TTL_S) + 60,
        )
        assert len(lastwords.consume_lastwords()) == 1

    def test_signal_kept_past_running_ttl(self):
        """显式优雅关闭的遗言保持 1h TTL。"""
        _write_valid(
            "s-sig50",
            reason="signal",
            ts=int(time.time()) - int(lastwords.RUNNING_TTL_S) - 600,
        )
        assert len(lastwords.consume_lastwords()) == 1

    def test_ttl_uses_consume_now(self):
        """TTL 以 consume 的 now 参数为准（不依赖文件 mtime）。"""
        now = time.time()
        _write_valid(
            "s-fut",
            reason="running",
            ts=int(now - lastwords.RUNNING_TTL_S - 1),
        )
        assert lastwords.consume_lastwords(now=now + 10) == []


class TestWalEffort:
    """WAL 写点维护工作量信号：计数不受节流丢写影响；write 合入 effort。"""

    def test_turn_start_snapshots_effort(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords.note_turn_start("s-w1", "t1", "default")
        snap = json.loads(lastwords._path("s-w1").read_text())
        assert snap["effort"]["tool_calls"] == 0
        assert snap["effort"]["turn_id"] == "t1"
        assert snap["effort"]["started_at"] > 0

    def test_counter_survives_throttle(self, monkeypatch: pytest.MonkeyPatch):
        """节流命中的写盘不丢计数：3 次被节流后放开写盘，快照含 tool_calls=3。"""
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords.note_turn_start("s-w2", "t2", "default")
        lastwords._LAST_WRITE_AT["s-w2"] = time.monotonic()  # 锁节流
        for _ in range(3):
            lastwords.note_tool_boundary("s-w2", "t2", "default", MSGS)
        assert lastwords._EFFORT["s-w2"]["tool_calls"] == 3
        lastwords._LAST_WRITE_AT["s-w2"] = time.monotonic() - 10  # 放开
        lastwords.note_tool_boundary("s-w2", "t2", "default", MSGS)
        snap = json.loads(lastwords._path("s-w2").read_text())
        assert snap["effort"]["tool_calls"] == 4

    def test_write_lastwords_merges_effort(self, monkeypatch: pytest.MonkeyPatch):
        """dev-swap/signal 显式落盘也带 effort（减半门槛判定需要数据）。"""
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords.note_turn_start("s-w3", "t3", "default")
        for _ in range(4):
            lastwords.note_tool_boundary("s-w3", "t3", "default", MSGS)
        lastwords._LAST_WRITE_AT["s-w3"] = time.monotonic() - 10
        server_shared._RUNNING_TURNS["s-w3"] = "t3"
        assert lastwords.write_lastwords("signal") == 1
        snap = json.loads(lastwords._path("s-w3").read_text())
        assert snap["reason"] == "signal"
        assert snap["effort"]["tool_calls"] >= 4

    def test_turn_end_clears_effort(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords.note_turn_start("s-w4", "t4", "default")
        lastwords.note_turn_end("s-w4", "t4")
        assert "s-w4" not in lastwords._EFFORT


class TestShutdownGuard:
    """优雅关闭保护：mark_shutdown 后 note_turn_end 不删遗言（SIGTERM 诊断 1a）。"""

    def test_note_turn_end_keeps_file_during_shutdown(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        lastwords.note_turn_start("s-sd", "t1", "default")
        assert lastwords._path("s-sd").exists()
        lastwords.mark_shutdown()
        lastwords.note_turn_end("s-sd", "t1")
        assert lastwords._path("s-sd").exists()  # signal 遗言保住了


class TestBgReconcile:
    """bg_tasks 注入前实时核对（数据源 = /api/tasks 同款 tasks/store index）。"""

    def test_done_tasks_removed_running_kept(self, monkeypatch: pytest.MonkeyPatch):
        from ginno_runtime.tasks import store as task_store

        monkeypatch.setattr(
            task_store,
            "read_index",
            lambda: {
                "t_a": {"status": task_store.STATUS_RUNNING},
                "t_b": {"status": "done"},
            },
        )
        snap = {"session_id": "s1", "bg_tasks": ["t_a", "t_b"]}
        lastwords.reconcile_bg_tasks(snap)
        assert snap["bg_tasks"] == ["t_a"]
        assert not snap.get("bg_tasks_unknown")
        assert "1 个" in lastwords.render_band(snap)

    def test_all_done_omits_note(self, monkeypatch: pytest.MonkeyPatch):
        from ginno_runtime.tasks import store as task_store

        monkeypatch.setattr(task_store, "read_index", lambda: {"t_a": {"status": "done"}})
        snap = {"session_id": "s1", "bg_tasks": ["t_a"]}
        lastwords.reconcile_bg_tasks(snap)
        assert snap["bg_tasks"] == []
        assert "后台任务" not in lastwords.render_band(snap)

    def test_source_unreachable_marks_unknown(self, monkeypatch: pytest.MonkeyPatch):
        def boom():
            raise RuntimeError("task store down")

        monkeypatch.setattr("ginno_runtime.tasks.store.read_index", boom)
        snap = {"session_id": "s1", "bg_tasks": ["t_a", "t_b"]}
        lastwords.reconcile_bg_tasks(snap)
        assert snap["bg_tasks"] == ["t_a", "t_b"]  # 保留原列表
        assert snap.get("bg_tasks_unknown") is True
        band = lastwords.render_band(snap)
        assert "2 个" in band and "状态未知" in band

    def test_empty_bg_noop(self):
        snap = {"session_id": "s1", "bg_tasks": []}
        lastwords.reconcile_bg_tasks(snap)
        assert "bg_tasks_unknown" not in snap

    async def test_recover_reconciles_before_inject(self, monkeypatch: pytest.MonkeyPatch):
        """恢复流程集簇：注入文本只含仍在跑的 bg 任务。"""
        from ginno_runtime.tasks import store as task_store

        monkeypatch.setattr(
            task_store,
            "read_index",
            lambda: {
                "t_live": {"status": task_store.STATUS_RUNNING},
                "t_dead": {"status": "done"},
            },
        )
        _write_valid("s-bg", reason="dev-swap", bg_tasks=["t_live", "t_dead"])
        g = FakeGraph()
        monkeypatch.setattr(
            "ginno_runtime.api.sessions._ensure_session",
            lambda sid: _fake_session(sid, g),
        )

        async def fake_push(event, data):
            pass

        monkeypatch.setattr(server_shared, "_push_global_event", fake_push)
        assert await lastwords.recover_lastwords() == 1
        band = g.updates[0][1]["messages"][0].content
        assert "1 个" in band and "2 个" not in band

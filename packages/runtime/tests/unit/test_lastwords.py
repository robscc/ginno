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
    HumanMessage(
        content="帮我跑一遍 dev-swap 验证遗言模式"
    ),
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
    yield
    server_shared._RUNNING_TURNS.clear()
    lastwords._LAST_WRITE_AT.clear()


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
        monkeypatch.setattr(
            lastwords, "_load_thread_messages", lambda slug, sid: list(MSGS)
        )
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
    def test_prepare_writes_snapshot(
        self, api_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(lastwords, "_resolve_slug", lambda sid: "default")
        monkeypatch.setattr(lastwords, "_load_thread_messages", _fake_loader)
        server_shared._RUNNING_TURNS["sess-live"] = "turn-x"
        r = api_client.post(
            "/api/devswap/lastwords/prepare", json={"fingerprint_old": "fp-old"}
        )
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
    async def test_notify_injects_and_drops(
        self, monkeypatch: pytest.MonkeyPatch
    ):
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
        monkeypatch.setattr(
            "ginno_runtime.api.sessions._ensure_session", lambda sid: None
        )
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

        monkeypatch.setattr(
            "ginno_runtime.api.stream._run_stream", fake_run_stream
        )
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
        assert (
            calls[0]["kw"]["user_extra_kwargs"]["ginno_task_event"]["kind"]
            == "lastwords"
        )
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
        band = lastwords.render_band(
            {"reason": "signal", "user_request_head": "长" * 200}
        )
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

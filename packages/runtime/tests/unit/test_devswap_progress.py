"""dev-swap 进度端点与状态机（docs/dev-swap-progress-design.md §2/§8）。

覆盖：上报解析/校验（stage 枚举、仅 127.0.0.1）、六阶段 running→ok/fail
状态机、run_id 去重（跨通道签名去重）、GET 快照、build 阶段 2s 广播节流。
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ginno_runtime import server_shared
from ginno_runtime.api import devswap

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clean_state():
    server_shared._DEVSWAP_PROGRESS.clear()
    devswap._last_signature = None
    devswap._last_broadcast_at = 0.0
    yield
    server_shared._DEVSWAP_PROGRESS.clear()
    devswap._last_signature = None
    devswap._last_broadcast_at = 0.0


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(devswap.router)
    return TestClient(app, client=("127.0.0.1", 51000))


def _post(client, run_id="ginno-runtime.vabc123.1000", stage="quiesce",
          state="running", detail=None, pct=None):
    body = {"run_id": run_id, "stage": stage, "state": state}
    if detail is not None:
        body["detail"] = detail
    if pct is not None:
        body["pct"] = pct
    return client.post("/api/devswap/progress", json=body)


class TestValidation:
    def test_bad_stage_rejected(self, client):
        r = _post(client, stage="nonsense")
        assert r.json()["ok"] is False

    def test_bad_state_rejected(self, client):
        r = _post(client, state="paused")
        assert r.json()["ok"] is False

    def test_missing_fields_422(self, client):
        assert client.post("/api/devswap/progress", json={"run_id": "x"}).status_code == 422

    def test_pct_out_of_range_422(self, client):
        assert _post(client, pct=150).status_code == 422

    def test_non_local_rejected(self):
        app = FastAPI()
        app.include_router(devswap.router)
        with TestClient(app) as c:  # 默认 client host=“testclient”=非本机
            r = c.post(
                "/api/devswap/progress",
                json={"run_id": "r", "stage": "quiesce", "state": "running"},
            )
        assert r.json().get("ok") is False
        assert r.json().get("error") == "local-only"


class TestStateMachine:
    def test_running_then_ok(self, client):
        _post(client, stage="quiesce", state="running")
        _post(client, stage="quiesce", state="ok", detail="无运行中 turn")
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["stages"]["quiesce"] == "ok"
        assert snap["stage"] == "quiesce"
        # 中间阶段 ok 是过场：run 级仍是 running（只有 finish 终态收敛）
        assert snap["state"] == "running"

    def test_running_then_fail(self, client):
        _post(client, stage="unit", state="running")
        _post(client, stage="unit", state="fail", detail="pytest failed")
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["stages"]["unit"] == "fail"
        # 任一阶段 fail → run 级 fail，stage 指到失败阶段
        assert snap["state"] == "fail"
        assert snap["stage"] == "unit"
        assert snap["detail"] == "pytest failed"

    def test_six_stages_full_sequence(self, client):
        for stage in devswap.STAGES:
            _post(client, stage=stage, state="running")
            _post(client, stage=stage, state="ok")
        snap = client.get("/api/devswap/progress").json()["run"]
        assert all(s == "ok" for s in snap["stages"].values())
        # finish ok → run 级 ok
        assert snap["state"] == "ok"
        assert snap["stage"] == "finish"

    def test_terminal_stage_is_idempotent(self, client):
        _post(client, stage="preflight", state="running")
        assert _post(client, stage="preflight", state="fail", detail="x").json()["changed"]
        # fail 之后同 stage 任何更新都被忽略（不许回退/重开）
        r = _post(client, stage="preflight", state="running", detail="retry")
        assert r.json().get("changed") is False
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["stages"]["preflight"] == "fail"
        assert snap["detail"] == "x"

    def test_unknown_stage_from_direct_update_ignored(self, client):
        r = devswap.update_progress(
            devswap.ProgressReport(run_id="r1", stage="weird", state="running")
        )
        assert r.get("rejected") == "unknown_stage"


class TestFlatView:
    """GET/广播的扁平 current-line 契约（前端 normalizeDevSwapRun 只认顶层
    run_id/stage/state——嵌套快照曾让整条链路在前端入口被丢弃）。"""

    def test_build_pct_surfaces_top_level(self, client):
        for s in ("quiesce", "unit"):
            _post(client, stage=s, state="running")
            _post(client, stage=s, state="ok")
        _post(client, stage="build_runtime", state="running", detail="PyInstaller", pct=55)
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["stage"] == "build_runtime"
        assert snap["state"] == "running"
        assert snap["pct"] == 55
        assert snap["detail"] == "PyInstaller"

    def test_finish_rolled_back(self, client):
        for s in ("quiesce", "unit", "build_runtime", "preflight", "switch", "finish"):
            _post(client, stage=s, state="running")
            _post(client, stage=s, state="ok" if s != "finish" else "rolled_back")
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["state"] == "rolled_back"
        assert snap["stage"] == "finish"

    def test_sid_attached_first_non_null_wins(self, client):
        r1 = client.post("/api/devswap/progress", json={
            "run_id": "r-sid", "stage": "quiesce", "state": "running", "sid": "sess-a"})
        assert r1.json()["changed"]
        # curl 通道不带 sid（None）不能抹掉已落的 sid
        _post(client, run_id="r-sid", stage="quiesce", state="ok")
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["sid"] == "sess-a"

    def test_started_at_present(self, client):
        _post(client, stage="quiesce", state="running")
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["started_at"] == pytest.approx(time.time(), abs=5)


class TestFinishWatcher:
    """watch_swap_finish：重启后的新进程补报 finish 终态（state.json 跳变）。"""

    def _write_state(self, home, state, *, new="ginno-runtime.vabc.1", sid="sess-1"):
        import json as j
        import pathlib

        d = pathlib.Path(home) / "dev-override"
        d.mkdir(parents=True, exist_ok=True)
        (d / "state.json").write_text(j.dumps({
            "state": state, "new": new, "old": "ginno-runtime.vold.0",
            "sha": "abc123", "session_id": sid,
        }))

    def test_pending_then_verified_reports_ok(self, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setenv("GINNO_HOME", str(tmp_path))
        self._write_state(tmp_path, "pending")
        # 终态在下一次轮询前写入（poll 0.01s）
        import threading

        def flip():
            time.sleep(0.05)
            self._write_state(tmp_path, "verified")

        threading.Thread(target=flip, daemon=True).start()
        asyncio.run(devswap.watch_swap_finish(poll_s=0.01, budget_s=2.0))
        snap = client_free_snapshot()
        assert snap["run"]["state"] == "ok"
        assert snap["run"]["stage"] == "finish"
        assert snap["run"]["sid"] == "sess-1"
        assert snap["run"]["run_id"] == "ginno-runtime.vabc.1"

    def test_pending_then_rolled_back_factory(self, tmp_path, monkeypatch):
        import asyncio
        import threading

        monkeypatch.setenv("GINNO_HOME", str(tmp_path))
        self._write_state(tmp_path, "pending")

        def flip():
            time.sleep(0.05)
            self._write_state(tmp_path, "rolled_back_factory", sid="")

        threading.Thread(target=flip, daemon=True).start()
        asyncio.run(devswap.watch_swap_finish(poll_s=0.01, budget_s=2.0))
        snap = client_free_snapshot()
        assert snap["run"]["state"] == "rolled_back"
        assert snap["run"]["sid"] is None

    def test_no_pending_seen_no_report(self, tmp_path, monkeypatch):
        import asyncio

        monkeypatch.setenv("GINNO_HOME", str(tmp_path))
        # 启动时已是历史终态（普通重启）：不得重复报
        self._write_state(tmp_path, "verified")
        asyncio.run(devswap.watch_swap_finish(poll_s=0.01, budget_s=0.1))
        assert client_free_snapshot()["run"] is None


def client_free_snapshot() -> dict:
    return devswap.get_snapshot()


class TestDedup:
    def test_exact_duplicate_signature_deduped(self, client):
        r1 = _post(client, stage="build_runtime", state="running", detail="PyInstaller")
        r2 = _post(client, stage="build_runtime", state="running", detail="PyInstaller")
        assert r1.json()["changed"] is True
        assert r2.json().get("duplicate") is True
        assert r2.json()["changed"] is False

    def test_new_run_id_resets_snapshot(self, client):
        _post(client, run_id="run-a", stage="quiesce", state="ok")
        _post(client, run_id="run-b", stage="quiesce", state="running")
        snap = client.get("/api/devswap/progress").json()["run"]
        assert snap["run_id"] == "run-b"
        assert snap["stages"]["quiesce"] == "running"

    def test_detail_change_not_deduped(self, client):
        _post(client, stage="build_runtime", state="running", detail="step1")
        r = _post(client, stage="build_runtime", state="running", detail="step2", pct=50)
        assert r.json()["changed"] is True


class TestGetSnapshot:
    def test_no_run_returns_null(self, client):
        assert client.get("/api/devswap/progress").json()["run"] is None

    def test_snapshot_shape(self, client):
        _post(client, stage="quiesce", state="running")
        snap = client.get("/api/devswap/progress").json()["run"]
        # 扁平 current-line 契约：stages 是 stage→state 的精简映射
        assert set(devswap.STAGES) == set(snap["stages"].keys())
        assert snap["stage"] == "quiesce"
        assert snap["state"] == "running"
        assert snap["ts"] == pytest.approx(time.time(), abs=5)


class TestBroadcast:
    def test_stage_switch_broadcasts_and_throttles_pct(self, client, monkeypatch):
        # 空 event loop 下 _broadcast_if_due 直接 no-op 返回；这里单测节流窗口
        # 本身：模拟已广播过的时间戳。
        devswap._last_broadcast_at = time.monotonic()
        monkeypatch.setattr(devswap, "_BROADCAST_MIN_INTERVAL_S", 60.0)
        assert devswap._should_broadcast_now(immediate=False) is False
        assert devswap._should_broadcast_now(immediate=True) is True

    def test_update_progress_returns_broadcast_flag(self, client):
        r = _post(client, stage="build_runtime", state="running", detail="a")
        assert "changed" in r.json() and "broadcast" in r.json()

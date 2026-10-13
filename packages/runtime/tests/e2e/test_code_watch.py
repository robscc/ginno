"""代码面板文件看守（code.watch → stat 看守 → code.changed，2026-10-13）。

bash 写文件不产生 code.changed（只有文件工具带结果标记），看守补齐外部
修改的自动跟随。复现口径：WS 注册会话工作区一个文件 → 进程外改写它 →
3s 看守周期内收到与文件工具同形的 code.changed。
"""

from __future__ import annotations

import queue
import threading
import time

import pytest

from ginno_runtime import server

pytestmark = pytest.mark.e2e


def test_code_watch_emits_on_external_write(client, patch_build_model):
    # 会话创建要解析模型——隔离 home 无 provider 配置，必须打模型补丁
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    sid = client.post(
        "/api/sessions", json={"project_slug": "default", "workspace": "/tmp/wf-ws"}
    ).json()["id"]
    # 会话工作区根下的目标文件（root=session 的相对路径 target.txt）
    from ginno_runtime import paths

    target = paths.session_files_dir("default", sid) / "target.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("v1", encoding="utf-8")

    with client.websocket_connect(f"/api/ws/sessions/{sid}") as ws:
        q: queue.Queue = queue.Queue()
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    q.put(ws.receive_json())
                except Exception:
                    q.put(None)
                    return

        threading.Thread(target=drain, args=(), daemon=True).start()
        try:
            ws.send_json(
                {"type": "code.watch", "files": [{"root": "session", "path": "target.txt"}]}
            )
            time.sleep(0.5)  # 注册落基线（不发声）
            while not q.empty():  # 清掉注册期杂帧
                q.get_nowait()
            # 进程外改写（不经任何 Ginno 工具——bash/sed 同类）
            target.write_text("v2 - written outside ginno", encoding="utf-8")
            deadline = time.time() + 8
            got = None
            while time.time() < deadline:
                try:
                    ev = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if ev and ev.get("event") == "code.changed":
                    got = ev
                    break
            assert got is not None, "no code.changed within 8s of external write"
            assert got.get("path") == str(target)
            assert got.get("op") == "edit"
            assert got.get("version")  # size:mtime_ns
        finally:
            stop.set()

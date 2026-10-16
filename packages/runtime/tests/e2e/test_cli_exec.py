"""ginno exec — headless CLI 的 e2e(benchmark harness 入口)。

子进程拉起 `python -m ginno_runtime exec`,隔离 GINNO_HOME + 脚本化 fake
LLM,验证三件事:

1. 完整 headless turn 的 stream-json 输出(init / assistant / result);
2. subagent 级联(spawn → 静默等待 → 全树 terminal);
3. 隔离性:隔离 home 里没有全局 MEMORY.md 内容,settings 被合成为唯一
   exec provider。

远端(attach)模式在 test_cli_exec_remote.py 单独覆盖。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

# 脚本:turn1 spawn 一个 subagent,turn2 收尾文本。ScriptedChatModel 每个
# session 从头回放,子会话也会 spawn(直到 depth-2 结构性截断),正好覆盖
# 多级级联 + 静默等待。
_SCRIPTS = [
    {
        "tool_calls": [
            {
                "name": "spawn_subagent",
                "args": {
                    "goal": "Count to three and report the number.",
                    "constraints": "Stay silent otherwise.",
                    "acceptance": "Number 3 is reported.",
                },
            }
        ]
    },
    {"content": "Main turn done."},
]


def _run_exec(tmp_path: Path, scripts_obj, extra_env: dict | None = None) -> tuple[int, list[dict]]:
    env = dict(os.environ)
    scripts_file = tmp_path / "scripts.json"
    scripts_file.write_text(json.dumps(scripts_obj), encoding="utf-8")
    env.update(
        {
            "GINNO_FAKE_LLM": "1",
            "GINNO_FAKE_LLM_SCRIPTS": str(scripts_file),
            "GINNO_EXEC_BASE_URL": "http://127.0.0.1:1",
            "GINNO_EXEC_API_KEY": "k",
            "GINNO_EXEC_MODEL": "fake-m",
            "GINNO_EXEC_NO_ATTACH": "1",  # 远端探测与本测试无关
            "GINNO_EXEC_HOME": str(tmp_path / "home"),  # 固定 home 便于断言
            "GINNO_RUNTIME_PORT": "0",  # 探活也不会命中
        }
    )
    env.update(extra_env or {})
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ginno_runtime",
            "exec",
            "--cwd",
            str(tmp_path),
            "--settle-timeout",
            "60",
            "--",
            "delegate it",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=180,
    )
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith("{")]
    return proc.returncode, events


def test_exec_happy_path(tmp_path):
    code, events = _run_exec(tmp_path, _SCRIPTS)
    assert code == 0
    kinds = [(e["type"], e.get("subtype")) for e in events]
    assert ("system", "init") in kinds
    assert ("assistant", None) in kinds
    result = next(e for e in events if e["type"] == "result")
    assert result["is_error"] is False
    assert result["text"].startswith("Main turn done.")
    # spawn 级联至少发生一次,且全部 subagent 事件有 session_id。
    spawned = [e for e in events if e.get("subtype") == "subagent_spawned"]
    assert spawned, f"expected subagent spawn events, got {kinds}"
    assert all(e.get("session_id") for e in spawned)


def test_exec_isolated_home(tmp_path):
    """隔离 home:合成 settings 只有 exec provider,不落个人状态。"""
    code, events = _run_exec(tmp_path, [{"content": "hi"}])
    assert code == 0
    home = tmp_path / "home"
    assert home.is_dir()
    settings = json.loads((home / "settings.json").read_text())
    assert [c["id"] for c in settings["model_configs"]] == ["exec"]
    assert settings["bypass_permissions"] is True
    assert settings["context"]["external_agents_enabled"] is False


def test_exec_no_world_state(tmp_path):
    """--no-world-state 关掉注入层后 turn 照常完成。"""
    env = dict(os.environ)
    scripts_file = tmp_path / "scripts.json"
    scripts_file.write_text(json.dumps([{"content": "quiet"}]), encoding="utf-8")
    env.update(
        {
            "GINNO_FAKE_LLM": "1",
            "GINNO_FAKE_LLM_SCRIPTS": str(scripts_file),
            "GINNO_EXEC_BASE_URL": "http://127.0.0.1:1",
            "GINNO_EXEC_API_KEY": "k",
            "GINNO_EXEC_MODEL": "fake-m",
            "GINNO_EXEC_NO_ATTACH": "1",
            "GINNO_EXEC_HOME": str(tmp_path / "home"),
        }
    )
    proc = subprocess.run(
        [
            sys.executable, "-m", "ginno_runtime", "exec", "--cwd", str(tmp_path),
            "--no-world-state", "--settle-timeout", "60", "--", "quiet",
        ],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=180,
    )
    assert proc.returncode == 0
    settings = json.loads((tmp_path / "home" / "settings.json").read_text())
    assert settings["context"]["world_state"] is False

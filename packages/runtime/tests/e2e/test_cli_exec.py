"""ginno exec — headless CLI 的 e2e(benchmark harness 入口)。

子进程拉起 `python -m ginno_runtime exec`,隔离 GINNO_HOME + 脚本化 fake
LLM,验证三件事:

1. 完整 headless turn 的 stream-json 输出(init / assistant / result);
2. subagent 级联(spawn → 静默等待 → 全树 terminal);
3. 隔离性:隔离 home 里没有全局 MEMORY.md 内容,settings 被合成为唯一
   exec provider。

远端(attach)模式在 test_cli_exec_remote.py 单独覆盖。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from ginno_runtime import jsonx as json

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
    # 终帧字段完整性（对外合约，docs/headless.md 帧目录）：九个字段一个不缺。
    for key in (
        "subtype", "is_error", "duration_ms", "num_turns",
        "session_id", "text", "preview", "error", "usage",
    ):
        assert key in result, f"result frame missing `{key}`"
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


# --------------------------------------------------------------------------- #
# stdin prompt 模式（Multica runtime 接入：Windows 单参数 32K 上限）
# --------------------------------------------------------------------------- #


def test_exec_prompt_stdin(tmp_path):
    """--prompt-stdin：从 stdin 读指令，不带 `--`；任务描述里有任意内容。"""
    env = dict(os.environ)
    scripts_file = tmp_path / "scripts.json"
    scripts_file.write_text(json.dumps([{"content": "stdin ok"}]), encoding="utf-8")
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
            "--settle-timeout", "60", "--prompt-stdin",
        ],
        input="A very long task description\nwith multiple lines — 日本語もOK\n",
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    events = [
        json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith("{")
    ]
    result = next(e for e in events if e["type"] == "result")
    assert result["is_error"] is False
    assert result["text"].startswith("stdin ok")


def test_exec_prompt_stdin_conflicts_with_dashdash(tmp_path):
    """--prompt-stdin 与 `-- <instruction>` 同时给出 → usage 错误（exit 2）。"""
    env = dict(os.environ)
    env.update({"GINNO_FAKE_LLM": "1", "GINNO_EXEC_HOME": str(tmp_path / "home")})
    proc = subprocess.run(
        [
            sys.executable, "-m", "ginno_runtime", "exec", "--cwd", str(tmp_path),
            "--prompt-stdin", "--", "pick one",
        ],
        input="stdin prompt", capture_output=True, text=True, env=env,
        cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 2
    assert "--prompt-stdin" in proc.stderr


def test_exec_prompt_missing_exits_2(tmp_path):
    """既没有 `-- <instruction>` 也没有 --prompt-stdin → usage 错误（exit 2）。"""
    env = dict(os.environ)
    env.update({"GINNO_FAKE_LLM": "1", "GINNO_EXEC_HOME": str(tmp_path / "home")})
    proc = subprocess.run(
        [sys.executable, "-m", "ginno_runtime", "exec", "--cwd", str(tmp_path)],
        input="", capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 2
    assert "instruction required" in proc.stderr


def test_exec_missing_env_exits_2(tmp_path):
    """非 fake-LLM 模式缺 GINNO_EXEC_* 三件套 → exit 2（usage/配置错误）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GINNO_EXEC_")}
    # 缺 env 检查在 attach 决策之后（远端模式用 app 自带 provider，env 可缺省），
    # 所以这里必须禁掉 attach，否则开发机上会连到真 sidecar 而不是走 exit 2。
    env["GINNO_EXEC_NO_ATTACH"] = "1"
    env["GINNO_EXEC_HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [
            sys.executable, "-m", "ginno_runtime", "exec", "--cwd", str(tmp_path),
            "--", "hi",
        ],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 2
    assert "missing env" in proc.stderr


# --------------------------------------------------------------------------- #
# tool 轨迹补全：tool_use 带真实 input，随后 user 帧带 tool_result
# --------------------------------------------------------------------------- #


def test_exec_tool_trajectory(tmp_path):
    """脚本一个 write_file 调用：assistant 帧的 tool_use.input 是真实参数，
    之后一条 type:"user" 帧带对应 tool_use_id 的 tool_result；文件真的落盘。"""
    code, events = _run_exec(
        tmp_path,
        [
            {
                "tool_calls": [
                    {
                        "name": "write_file",
                        "args": {"path": "note.txt", "content": "hello trajectory"},
                    }
                ]
            },
            {"content": "wrote it"},
        ],
    )
    assert code == 0
    assistant = next(e for e in events if e["type"] == "assistant")
    tool_uses = [b for b in assistant["message"]["content"] if b["type"] == "tool_use"]
    assert tool_uses, [b.get("type") for b in assistant["message"]["content"]]
    assert tool_uses[0]["name"] == "write_file"
    assert tool_uses[0]["input"] == {"path": "note.txt", "content": "hello trajectory"}
    user = next(e for e in events if e["type"] == "user")
    trs = [b for b in user["message"]["content"] if b["type"] == "tool_result"]
    assert [t["tool_use_id"] for t in trs] == [t["id"] for t in tool_uses]
    assert all(t["content"] for t in trs), trs
    assert (tmp_path / "note.txt").read_text() == "hello trajectory"
    # tool_use 与 tool_result 的先后顺序：user 帧必须在其 assistant 帧之后。
    idx = {id(e): i for i, e in enumerate(events)}
    assert idx[id(user)] > idx[id(assistant)]


# --------------------------------------------------------------------------- #
# --home 持久化（v2 resume 打底）
# --------------------------------------------------------------------------- #


def test_exec_home_persistence(tmp_path, monkeypatch):
    """显式 --home：session meta / checkpointer / usage 全落该 home；
    二次 exec 后仍能按 session_id 找回上一会话（没有状态偷偷写到别处）。"""
    home = tmp_path / "persistent-home"
    code1, events1 = _run_exec(
        tmp_path,
        # usage_metadata 随脚本注入——usage_store 只在模型上报 usage 时落账。
        [
            {
                "content": "first run",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }
        ],
        extra_env={"GINNO_EXEC_HOME": str(home)},
    )
    assert code1 == 0
    sid1 = next(e for e in events1 if e["type"] == "result")["session_id"]
    assert sid1

    # 全部状态都在显式 home 下。
    assert (home / "settings.json").is_file()
    sessions_dir = home / "projects" / "default" / "sessions"
    assert sessions_dir.is_dir(), sorted(p.name for p in home.rglob("*") if p.is_dir())
    assert (sessions_dir / f"{sid1}.json").is_file()  # checkpointer
    assert list((home / "usage").glob("requests-*.jsonl")), "usage ledger not in --home"

    # 二次 exec（同一 home）：上一会话仍可按 session_id 找回。
    code2, events2 = _run_exec(
        tmp_path, [{"content": "second run"}], extra_env={"GINNO_EXEC_HOME": str(home)}
    )
    assert code2 == 0
    sid2 = next(e for e in events2 if e["type"] == "result")["session_id"]
    assert sid2 != sid1
    monkeypatch.setenv("GINNO_HOME", str(home))
    from ginno_runtime.session_meta import _find_meta

    found = _find_meta(sid1)
    assert found and found[0].get("id") == sid1, "prior session not recoverable from --home"


def test_exec_version_flag(tmp_path):
    """`ginno --version` 输出 `ginno x.y.z`（独立安装的可执行性入口）。

    走 console script 入口（pyproject [project.scripts] ginno = cli:main）——
    `python -m ginno_runtime --version` 是 PyInstaller/服务入口，不带 --version。
    """
    env = dict(os.environ)
    env["GINNO_EXEC_HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; from ginno_runtime.cli import main; main()", "--version",
        ],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert proc.returncode == 0
    out = proc.stdout.strip()
    assert out.startswith("ginno ") and out.split()[1][0].isdigit()

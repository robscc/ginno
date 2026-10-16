"""agent_node 的 degenerate-final-reply recovery(graph.py)。

thinking 模型偶尔以 reasoning-only 消息收尾:content 空、无 tool_calls、
finish=stop——图视为正常 END,turn 静默结束。修复:有界注入提醒消息再问,
产出可见回复为止。本测试用脚本化 fake 模型确定性复现:turn1 空 content →
期望 nudge 后取到 script[1] 的文本。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _run_exec(tmp_path: Path, scripts_obj) -> subprocess.CompletedProcess:
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
            "GINNO_EXEC_NO_ATTACH": "1",
            "GINNO_EXEC_HOME": str(tmp_path / "home"),
        }
    )
    return subprocess.run(
        [
            sys.executable, "-m", "ginno_runtime", "exec",
            "--cwd", str(tmp_path), "--settle-timeout", "30", "--", "do the thing",
        ],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=120,
    )


def test_empty_reply_gets_nudged(tmp_path):
    proc = _run_exec(tmp_path, [{"content": ""}, {"content": "final answer"}])
    assert proc.returncode == 0, proc.stderr[-500:]
    result = [json.loads(l) for l in proc.stdout.splitlines()
              if l.strip().startswith("{") and '"result"' in l][-1]
    assert result["text"] == "final answer"
    # nudge 消息应已落盘(checkpointer 重放一致):history 里能查到提醒。
    # 走 REST 太重,这里断言 usage 之外的关键行为即可——final text 非空
    # 本身证明第二次 ainvoke 发生过(script[0] 是空的)。


def test_all_empty_replies_still_end_gracefully(tmp_path):
    """3 次 nudge 全空 → 照旧正常 END(exit 0,空 text,不崩)。"""
    proc = _run_exec(tmp_path, [{"content": ""}, {"content": ""}, {"content": ""},
                                {"content": ""}, {"content": ""}])
    assert proc.returncode == 0, proc.stderr[-500:]
    result = [json.loads(l) for l in proc.stdout.splitlines()
              if l.strip().startswith("{") and '"result"' in l][-1]
    assert result["text"] == ""
    assert result["is_error"] is False

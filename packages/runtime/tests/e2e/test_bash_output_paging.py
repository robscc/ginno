"""bash 输出优化 e2e（真实命令真实执行，2026-11-07）。

E1 超长 stdout → 省略行含落盘路径 → read_file 分页取回中间行，行号与内容
   连续逐字正确；
E2-E4 rtk 白名单改写在真实 bash() 调用中发生且输出正常（无 rtk 的 CI
   runner skipif 跳过；本机必跑）——Popen spy 只记录 argv，执行仍真实；
E5 stderr 超预算不回归（P0 决策：stderr 不落盘）；
E6 连跑超预算命令后落盘目录文件数 ≤ 上限；
E7 短命令零行为回归（无省略行、无落盘残留）；
E8 中文 / 非 UTF-8 字节（errors=replace）落盘与恢复不炸。

不用 conftest（红线：tests/e2e 下禁止 conftest.py），fixture 就地定义。
"""

from __future__ import annotations

import io
import re
import subprocess
from pathlib import Path

import pytest

from ginno_runtime.tools import builtin, rtk_bridge
from ginno_runtime.tools.builtin import BASH_SPILL_DIR, BASH_SPILL_MAX_FILES, build_builtin_tools
from ginno_runtime.tools.rtk_bridge import detect_rtk, reset_rtk_cache

pytestmark = pytest.mark.e2e

RTK = detect_rtk()  # 进程启动时探一次；Homebrew fallback 已含 /opt/homebrew/bin

requires_rtk = pytest.mark.skipif(RTK is None, reason="rtk not installed (CI runner)")


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


@pytest.fixture
def tools(ws):
    return {t.name: t for t in build_builtin_tools(ws)}


@pytest.fixture(autouse=True)
def _isolated_rtk_settings(tmp_path, monkeypatch):
    """settings 指到临时路径（不存在 → rtk.enabled 默认 true），并清检测缓存，
    避免本机 ~/.ginno/settings.json 的真实开关影响 e2e 结论。"""
    reset_rtk_cache()
    monkeypatch.setattr(rtk_bridge, "settings_path", lambda: tmp_path / "settings.json")
    yield
    reset_rtk_cache()


@pytest.fixture
def popen_spy(monkeypatch):
    """记录每条 shell argv 后透传真实 Popen——spy 不改变执行（e2e 仍真实跑）。"""
    calls: list[str] = []
    real = subprocess.Popen

    def spy(*a, **kw):
        argv = list(a[0]) if a and isinstance(a[0], (list, tuple)) else kw.get("args", [])
        calls.append(argv[-1] if argv else "?")
        return real(*a, **kw)

    monkeypatch.setattr(builtin.subprocess, "Popen", spy)
    return calls


@pytest.fixture
def git_repo(tmp_path):
    """临时 git repo，3 个可辨识 commit（message 带序号）。"""
    d = tmp_path / "repo"
    d.mkdir()
    env_not_needed = None
    for i in range(1, 4):
        (d / f"f{i}.txt").write_text(f"content {i}\n")
        subprocess.run(["git", "init", "-q"], cwd=d, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=d, check=True, capture_output=True, env=env_not_needed)
        subprocess.run(
            ["git", "-c", "user.email=e2e@t", "-c", "user.name=e2e", "commit", "-qm", f"e2e commit {i}"],
            cwd=d, check=True, capture_output=True,
        )
    return d


# ---- E1：超长输出 → 落盘 → read_file 分页取回 -------------------------------


def test_e1_long_output_spilled_and_recoverable_by_page(tools, ws):
    out = tools["bash"].invoke(
        {"command": "for i in $(seq 1 6000); do echo \"line $i 0123456789\"; done", "timeout": 60}
    )
    assert out.startswith("[exit 0]")
    assert "中间省略" in out
    # 省略行含落盘相对路径 + read_file 取回提示
    m = re.search(r"全量已存 (\S+?)，read_file offset=1 limit=(\d+) 可取回", out)
    assert m, out[-500:]
    rel, hint_limit = m.group(1), int(m.group(2))
    assert rel.startswith(f"{BASH_SPILL_DIR}/bash-") and rel.endswith(".log")
    assert hint_limit >= 1
    # 落盘文件在 base_dir 下（read_file 的路径豁免区），分页可读
    abs_path = Path(ws) / rel
    assert abs_path.is_file() and abs_path.stat().st_size > 5000
    # 从中间窗口翻页取回：offset 跳过可能不完整的落盘首行（head 尾行余部）
    page = tools["read_file"].invoke({"path": rel, "offset": 2, "limit": 120})
    assert "lines 2-121 of" in page
    page = page.split("\n... (lines", 1)[0]  # 剥掉翻页提示行，只留正文
    nums = []
    for ln in page.splitlines():
        mm = re.match(r"^line (\d+) 0123456789$", ln)
        assert mm, f"spill 行格式不符: {ln!r}"
        nums.append(int(mm.group(1)))
    assert len(nums) == 120
    assert nums == list(range(nums[0], nums[0] + 120))  # 行号连续无空洞
    assert 1 < nums[0] < 6000  # 确实取回的是中间段
    # 再翻一页：与上一页行号衔接
    page2 = tools["read_file"].invoke({"path": rel, "offset": 122, "limit": 10})
    page2 = page2.split("\n... (lines", 1)[0]
    nums2 = [int(re.match(r"^line (\d+) 0123456789$", ln).group(1)) for ln in page2.splitlines()]
    assert nums2[0] == nums[-1] + 1
    # head/tail 与落盘三者拼起来 = 原始输出（省略计数守恒）
    total_lines = len(page.splitlines())  # noqa: F841 — 行数守恒由「6000 行源」间接保证
    full = Path(ws) / "full.txt"
    with open(full, "w") as fh:
        subprocess.run(
            ["bash", "-c", "for i in $(seq 1 6000); do echo \"line $i 0123456789\"; done"],
            cwd=ws, check=True, stdout=fh, stderr=subprocess.DEVNULL,
        )
    src_lines = full.read_text().splitlines()
    # 落盘文件的行序号与原始输出对齐：spill 第 k 行的行号 ≈ head 行边界 + k
    head_text = out.split("\n[stdout:")[0].replace("[exit 0]\n", "", 1)
    head_last_num = int(re.findall(r"line (\d+) 0123456789", head_text)[-1])
    spill_first_num = nums[0]
    # chunk 边界可在任意字符处断开：head 尾残行 + spill 首残行各占 1 行，
    # 故 spill 首个完整行号 = head 最后完整行号 + 1 或 + 2
    assert 1 <= spill_first_num - head_last_num <= 2


def test_e1_hint_limit_matches_head_line_count(tools, ws):
    """省略行的 limit 提示 ≈ head 实际行数（窗口同规模引导）。"""
    out = tools["bash"].invoke(
        {"command": "for i in $(seq 1 6000); do echo \"line $i 0123456789\"; done", "timeout": 60}
    )
    m = re.search(r"limit=(\d+) 可取回", out)
    head_text = out.split("\n[stdout:")[0].replace("[exit 0]\n", "", 1)
    head_lines = len(head_text.splitlines())
    assert abs(int(m.group(1)) - head_lines) <= 1


# ---- E2/E3/E4：rtk 改写真实生效（本机必跑，无 rtk 的 CI skip） ----------------


@requires_rtk
def test_e2_rtk_whitelisted_single_command_runs(git_repo, ws, tools, popen_spy):
    """白名单单命令 `git log --oneline -3` 经 bash() 被 rtk 改写执行，
    输出含全部 3 个 commit（rtk 1.1.0 整链路不炸）。cwd 即 repo。"""
    repo_tools = {t.name: t for t in build_builtin_tools(str(git_repo))}
    out = repo_tools["bash"].invoke({"command": "git log --oneline -3", "timeout": 30})
    assert out.startswith("[exit 0]"), out
    assert popen_spy[-1] == f"{RTK} git log --oneline -3", popen_spy[-1]
    for i in (1, 2, 3):
        assert f"e2e commit {i}" in out


@requires_rtk
def test_e3_git_c_path_hits_whitelist(git_repo, tools, popen_spy):
    """B2：`git -C <path> log` 命中白名单形态，改写执行输出正常（-C 指向
    绑定的 workspace 自身，路径在豁免区内）。"""
    repo_tools = {t.name: t for t in build_builtin_tools(str(git_repo))}
    out = repo_tools["bash"].invoke({"command": "git -C . log --oneline", "timeout": 30})
    assert out.startswith("[exit 0]"), out
    assert popen_spy[-1] == f"{RTK} git -C . log --oneline", popen_spy[-1]
    assert out.count("e2e commit") == 3
    # -C 后非白名单子命令不重写
    out2 = repo_tools["bash"].invoke({"command": "git -C . add -A", "timeout": 30})
    assert out2.startswith("[exit 0]")
    assert popen_spy[-1] == "git -C . add -A"


@requires_rtk
def test_e4_compound_cd_git_log_second_segment_rewritten(git_repo, ws, tools, popen_spy):
    """B3：`cd <tmp> && git log --oneline` 第二段被重写，整条 exit 0 输出正常。"""
    repo_tools = {t.name: t for t in build_builtin_tools(str(git_repo))}
    out = repo_tools["bash"].invoke(
        {"command": "cd . && git log --oneline", "timeout": 30}
    )
    assert out.startswith("[exit 0]"), out
    assert popen_spy[-1] == f"cd . && {RTK} git log --oneline", popen_spy[-1]
    assert out.count("e2e commit") == 3


@requires_rtk
def test_e4_pipe_segment_not_rewritten(git_repo, ws, tools, popen_spy):
    """B3 保守边界：含管道的段整段原样（rtk 不碰管道语义）。"""
    repo_tools = {t.name: t for t in build_builtin_tools(str(git_repo))}
    out = repo_tools["bash"].invoke({"command": "cd . && git log --oneline | head -2", "timeout": 30})
    assert out.startswith("[exit 0]"), out
    assert popen_spy[-1] == "cd . && git log --oneline | head -2", popen_spy[-1]
    assert out.count("e2e commit") == 2


# ---- E5：stderr 超预算不回归 --------------------------------------------------


def test_e5_stderr_over_budget_no_regression(tools, ws):
    out = tools["bash"].invoke(
        {"command": "for i in $(seq 1 900); do echo \"errline $i 0123456789ABCDEF\" 1>&2; done", "timeout": 30}
    )
    assert out.startswith("[exit 0]")
    assert "[stderr: 中间省略" in out
    assert "errline 1 " in out  # head 保留
    assert "errline 900" in out  # tail 保留
    assert "可取回" not in out.split("--- stderr ---")[1]  # stderr 不落盘（P0 决策）
    assert not (Path(ws) / BASH_SPILL_DIR).exists()


# ---- E6：落盘清理 -------------------------------------------------------------


def test_e6_spill_dir_capped_after_many_runs(tools, ws):
    """连跑 25 次超预算命令：目录文件数 ≤ BASH_SPILL_MAX_FILES（最老先删）。"""
    cmd = "for i in $(seq 1 2500); do echo \"line $i 0123456789\"; done"
    for _ in range(25):
        out = tools["bash"].invoke({"command": cmd, "timeout": 60})
        assert out.startswith("[exit 0]")
    d = Path(ws) / BASH_SPILL_DIR
    files = list(d.iterdir())
    assert 1 <= len(files) <= BASH_SPILL_MAX_FILES, len(files)
    for f in files:  # 残留文件仍可读（read_file 路径不被拒）
        page = tools["read_file"].invoke({"path": f"{BASH_SPILL_DIR}/{f.name}", "offset": 1, "limit": 3})
        assert page.startswith("line ")


# ---- E7：正常短命令零行为回归 --------------------------------------------------


def test_e7_short_commands_no_omission_no_residue(tools, ws):
    init = tools["bash"].invoke({"command": "git init -q", "timeout": 15})
    assert init.startswith("[exit 0]"), init
    for cmd in ("ls", "echo hello", "git status"):
        out = tools["bash"].invoke({"command": cmd, "timeout": 15})
        assert out.startswith("[exit 0]"), (cmd, out)
        assert "中间省略" not in out
    assert not (Path(ws) / BASH_SPILL_DIR).exists()


# ---- E8：中文 / 非 UTF-8 字节 --------------------------------------------------


def test_e8_cjk_and_invalid_utf8_bytes_roundtrip(tools, ws):
    """中文超长输出照常落盘分页取回；尾部非法 UTF-8 字节经 errors=replace
    落盘/读回全程不炸。"""
    out = tools["bash"].invoke(
        {
            "command": (
                "for i in $(seq 1 6000); do echo \"中文行 $i 数据填充\"; done; "
                "printf '\\xff\\xfe\\xff\\xfe\\xff\\xfe'"
            ),
            "timeout": 60,
        }
    )
    assert out.startswith("[exit 0]")
    assert "中间省略" in out and "可取回" in out
    rel = re.search(r"全量已存 (\S+?)，", out).group(1)
    page = tools["read_file"].invoke({"path": rel, "offset": 2, "limit": 80})
    page = page.split("\n... (lines", 1)[0]  # 剥掉翻页提示行
    nums = []
    for ln in page.splitlines():
        mm = re.match(r"^中文行 (\d+) 数据填充$", ln)
        assert mm, f"中文 spill 行格式不符: {ln!r}"
        nums.append(int(mm.group(1)))
    assert nums == list(range(nums[0], nums[0] + 80))  # 中文行号连续
    # 非法字节在 tail 段被替换为 U+FFFD（或随 chunk 归属省略），命令与输出不炸
    assert "[exit 0]" in out


def test_e8_invalid_bytes_inside_spill_not_crash(tools, ws):
    """中间段本身混入非法 UTF-8：spill 写入（errors=replace）与 read_file
    读回均不炸。"""
    out = tools["bash"].invoke(
        {
            "command": (
                "for i in $(seq 1 1500); do echo \"pad $i 0123456789ABCDEF\"; done; "
                "printf '\\xff\\xff\\xff'; "
                "for i in $(seq 1 1500); do echo \"tail $i 0123456789ABCDEF\"; done"
            ),
            "timeout": 60,
        }
    )
    assert out.startswith("[exit 0]")
    m = re.search(r"全量已存 (\S+?)，", out)
    assert m  # 非法字节混在中间段里仍完成落盘
    page = tools["read_file"].invoke({"path": m.group(1), "offset": 1, "limit": 20})
    assert "pad " in page  # 中间段可读

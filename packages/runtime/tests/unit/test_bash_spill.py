"""bash 中间段落盘 + 分页恢复单测（builtin.py 2026-11-07）。

_drain_pipe 丢弃的中间段流式写盘到 base_dir/.ginno-bash-out/（懒创建），
_compose_capped 省略行给出 read_file 取回提示；落盘失败/超大小上限静默
降级回旧省略行格式。预算常量产品值不动——单测 monkeypatch 小预算以
低成本触发「超预算」路径（e2e 用真实常量真跑超长命令）。
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from ginno_runtime.tools import builtin
from ginno_runtime.tools.builtin import (
    BASH_SPILL_DIR,
    BASH_SPILL_MAX_FILES,
    _compose_capped,
    _drain_pipe,
    _open_spill,
    _sweep_spill_dir,
    build_builtin_tools,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return Path(d)


def _state():
    return {
        "head": [], "tail": __import__("collections").deque(),
        "head_len": 0, "tail_len": 0, "total": 0, "spill_path": None,
    }


# ---- _drain_pipe：落盘内容 = 被丢弃的中间段（流式、懒创建） ------------------
#
# 注意 head 按 chunk 粒度超shoot（既有行为：首 read(8192) 整块进 head，
# head_len 可到 cap_head+8191）——测试数据必须让首个 chunk 超过 cap_head，
# 后续 chunk 才走 tail/丢弃路径。


def _run_drain(chunks: str, cap_head: int, cap_tail: int, state, opener=None):
    t = threading.Thread(
        target=_drain_pipe,
        args=(io.StringIO(chunks), cap_head, cap_tail, state, opener),
    )
    t.start()
    t.join(10)


def test_drain_spills_dropped_middle(ws):
    """head 满后 tail 滚出的部分逐字落盘；文件在 base_dir 下、相对路径入 state。

    tail 按 chunk 粒度滚动（既有语义：popleft 整块）。数据三块：
    read1 8192H → head；read2 (808H+7384M) → tail 8192 > 5000 → 整块落盘；
    read3 (1616M+30T) → tail 保留。
    """
    st = _state()
    opener = lambda: _open_spill(ws)  # noqa: E731
    _run_drain("H" * 9000 + "M" * 9000 + "T" * 30, 100, 5000, st, opener)
    assert st["total"] == 18030
    assert st["head_len"] == 8192  # 首 chunk 整块（既有超shoot语义）
    assert st["spill_path"].startswith(f"{BASH_SPILL_DIR}/bash-")
    spilled = (ws / BASH_SPILL_DIR / Path(st["spill_path"]).name).read_text()
    assert spilled == "H" * 808 + "M" * 7384  # 被挤出的整个中间 chunk，逐字
    assert "".join(st["tail"]) == "M" * 1616 + "T" * 30


def test_drain_no_spill_file_when_under_budget(ws):
    """懒创建：不超预算（无丢弃）绝不调 opener——短命令零残留。"""
    called = {"n": 0}

    def opener():
        called["n"] += 1
        return _open_spill(ws)

    st = _state()
    _run_drain("short output", 100, 50, st, opener)
    assert called["n"] == 0
    assert st["spill_path"] is None
    assert not (ws / BASH_SPILL_DIR).exists()


def test_drain_opener_returns_none_degrades_silently(ws):
    """opener 返回 None（mkdir/open 失败已吞掉）→ 纯省略行，绝不抛。"""
    st = _state()
    opener = lambda: None  # noqa: E731
    _run_drain("H" * 9000 + "M" * 9000, 100, 5000, st, opener)
    assert st["spill_path"] is None
    out = _compose_capped(st, "stdout")
    assert "中间省略 8192 chars]" in out  # 整块中间 chunk；旧格式无恢复提示
    assert "可取回" not in out


def test_open_spill_oserror_returns_none(tmp_path):
    """opener 内部 OSError（父目录是文件）→ 返回 None → 调用方降级。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    assert _open_spill(blocker) is None  # blocker/.ginno-bash-out mkdir → NotADirectoryError

    st = _state()
    _run_drain("H" * 9000 + "M" * 80, 100, 50, st, lambda: _open_spill(blocker))
    assert st["spill_path"] is None
    assert "可取回" not in _compose_capped(st, "stdout")


def test_drain_spill_write_failure_abandons_file(ws, monkeypatch):
    """写中途 OSError（磁盘满等）→ 关闭并删除半成品，退回纯省略行。"""
    monkeypatch.setattr(builtin, "BASH_SPILL_MAX_FILE_BYTES", 10**9)
    st = _state()
    opened = _open_spill(ws)
    assert opened is not None
    fh, rel, abs_path = opened
    writes = {"n": 0}

    class BoomFH:
        def write(self, s):
            writes["n"] += 1
            if writes["n"] > 1:
                raise OSError(28, "No space left on device")
            fh.write(s)

        def flush(self):
            pass

        def close(self):
            fh.close()

    opener = lambda: (BoomFH(), rel, abs_path)  # noqa: E731
    # 数据跨多个 dropped chunk（StringIO read(8192) 按块切）：第一块写成功、
    # 第二块炸 → abandon。
    _run_drain("H" * 9000 + "M" * 9000 + "T" * 30, 100, 50, st, opener)
    assert st["spill_path"] is None  # abandon 置 None
    assert not abs_path.exists()  # 半成品已删
    assert "可取回" not in _compose_capped(st, "stdout")


def test_drain_spill_size_cap_deletes_file(ws, monkeypatch):
    """累计写入超 BASH_SPILL_MAX_FILE_BYTES → 删文件退化为纯省略行。"""
    monkeypatch.setattr(builtin, "BASH_SPILL_MAX_FILE_BYTES", 40)
    st = _state()
    opener = lambda: _open_spill(ws)  # noqa: E731
    _run_drain("H" * 9000 + "M" * 200 + "T" * 10, 100, 50, st, opener)
    assert st["spill_path"] is None
    assert list((ws / BASH_SPILL_DIR).iterdir()) == []  # 超限文件已删
    assert "可取回" not in _compose_capped(st, "stdout")


# ---- _compose_capped：省略行新格式 ------------------------------------------


def test_compose_capped_with_spill_hint():
    st = {
        "head": ["line1\nline2\nline3\n"], "tail": ["line99\n"],
        "head_len": 18, "tail_len": 7, "total": 1000,
        "spill_path": f"{BASH_SPILL_DIR}/bash-abcd1234.log",
    }
    out = _compose_capped(st, "stdout")
    assert (
        "[stdout: 中间省略 975 chars → 全量已存 "
        ".ginno-bash-out/bash-abcd1234.log，read_file offset=1 limit=3 可取回]"
    ) in out
    assert out.startswith("line1\nline2\nline3\n")
    assert out.endswith("line99\n")


def test_compose_capped_without_spill_keeps_legacy_format():
    st = {
        "head": ["h"], "tail": ["t"], "head_len": 1, "tail_len": 1,
        "total": 100, "spill_path": None,
    }
    assert _compose_capped(st, "stderr") == "h\n[stderr: 中间省略 98 chars]\nt"


def test_compose_capped_under_budget_returns_head():
    st = {"head": ["all"], "tail": [], "head_len": 3, "tail_len": 0,
          "total": 3, "spill_path": None}
    assert _compose_capped(st, "stdout") == "all"


# ---- 清理策略 ----------------------------------------------------------------


def test_sweep_keeps_max_files_deletes_oldest(ws):
    d = ws / BASH_SPILL_DIR
    d.mkdir(parents=True)
    files = []
    for i in range(BASH_SPILL_MAX_FILES + 5):
        p = d / f"bash-old{i:02d}.log"
        p.write_text("x")
        import os

        os.utime(p, (1000 + i, 1000 + i))  # mtime 单调：编号越大越新
        files.append(p)
    keep = d / "bash-keep.log"
    keep.write_text("x")
    _sweep_spill_dir(d, keep)
    left = set(d.iterdir())
    assert len(left) == BASH_SPILL_MAX_FILES  # 25 old + keep = 26 → 删最老 6 个
    assert keep in left
    for p in files[:6]:
        assert p not in left  # 最老的 6 个被删
    for p in files[6:]:
        assert p in left  # 其余全保留


def test_sweep_under_limit_is_noop(ws):
    d = ws / BASH_SPILL_DIR
    d.mkdir(parents=True)
    (d / "a.log").write_text("x")
    _sweep_spill_dir(d, d / "a.log")  # 不抛即过
    assert (d / "a.log").exists()


# ---- bash() 端到端（小预算 monkeypatch + 真实命令） ---------------------------


@pytest.fixture
def small_budget(monkeypatch):
    monkeypatch.setattr(builtin, "BASH_OUT_HEAD", 200)
    monkeypatch.setattr(builtin, "BASH_OUT_TAIL", 100)
    monkeypatch.setattr(builtin, "BASH_ERR_HEAD", 200)
    monkeypatch.setattr(builtin, "BASH_ERR_TAIL", 100)


def test_bash_end_to_end_spill_and_readback(ws, small_budget):
    tools = {t.name: t for t in build_builtin_tools(str(ws))}
    # 700 行 × ~18 chars ≈ 12.6k：跨多个 read(8192) chunk（head 按块超shoot
    # 8192），后续块走 tail/丢弃 → 触发落盘
    out = tools["bash"].invoke(
        {"command": "for i in $(seq 1 700); do echo \"line $i 0123456789\"; done", "timeout": 15}
    )
    assert "[exit 0]" in out
    assert "中间省略" in out and "可取回" in out
    assert f"{BASH_SPILL_DIR}/bash-" in out
    # 相对路径可被 read_file 直接读取（base_dir 豁免路径拒绝）
    rel = out.split("全量已存 ")[1].split("，")[0]
    page = tools["read_file"].invoke({"path": rel, "offset": 1, "limit": 5})
    assert page.startswith("line ")  # 落盘首行即中间段开头（head 尾行可能不完整）
    assert "line " in page


def test_bash_short_command_no_spill_residue(ws, small_budget):
    tools = {t.name: t for t in build_builtin_tools(str(ws))}
    out = tools["bash"].invoke({"command": "echo hello; echo done", "timeout": 10})
    assert "[exit 0]" in out and "hello" in out
    assert "中间省略" not in out
    assert not (ws / BASH_SPILL_DIR).exists()


def test_bash_stderr_over_budget_no_spill(ws, small_budget):
    """stderr 超预算仍走省略行（P0 决策：stderr 不落盘），命令不砸。"""
    tools = {t.name: t for t in build_builtin_tools(str(ws))}
    out = tools["bash"].invoke(
        {"command": "for i in $(seq 1 700); do echo \"err $i 0123456789\" 1>&2; done", "timeout": 15}
    )
    assert "[exit 0]" in out
    assert "[stderr: 中间省略" in out
    assert "可取回" not in out.split("--- stderr ---")[1]
    assert not (ws / BASH_SPILL_DIR).exists()  # stderr 不触发落盘


def test_bash_spill_dir_pruned_from_glob_and_grep(ws, small_budget):
    """落盘目录进 _WALK_IGNORE_DIRS：glob/grep 默认不搜内部恢复文件。"""
    assert BASH_SPILL_DIR in builtin._WALK_IGNORE_DIRS
    tools = {t.name: t for t in build_builtin_tools(str(ws))}
    tools["bash"].invoke(
        {"command": "for i in $(seq 1 60); do echo \"line $i unique-spill-token\"; done", "timeout": 15}
    )
    g = tools["glob_files"].invoke({"pattern": f"{BASH_SPILL_DIR}/**"})
    assert g == "" or BASH_SPILL_DIR not in g
    grep = tools["grep_files"].invoke({"pattern": "unique-spill-token"})
    assert "unique-spill-token" not in grep

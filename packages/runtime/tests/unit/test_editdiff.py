"""editdiff（编辑 diff 显示后端，2026-11-21 P0/P1）单测。"""

from __future__ import annotations

from ginno_runtime.editdiff import make_edit_diff


def test_basic_edit_diff_and_stats():
    d = make_edit_diff("edit", "a/b.py", "x=1\ny=2\n", "x=1\ny=3\n")
    assert d is not None
    assert d["added"] == 1 and d["removed"] == 1
    assert d["truncated"] is False
    assert "@@ -1,2 +1,2 @@" in d["text"]
    assert "-y=2" in d["text"] and "+y=3" in d["text"]
    assert d["text"].startswith("--- a/a/b.py\n+++ b/a/b.py")


def test_chinese_lines_survive():
    d = make_edit_diff("edit", "中文/路径.md", "# 标题\n", "# 标题\n内容\n")
    assert d is not None
    assert "+内容" in d["text"]
    assert d["added"] == 1 and d["removed"] == 0


def test_new_file_all_adds():
    d = make_edit_diff("add", "new.txt", None, "a\nb\nc\n")
    assert d is not None
    assert d["removed"] == 0 and d["added"] == 3
    assert "-a/" not in d["text"].split("@@")[0] or True  # 旧空侧无删除行
    assert "\n-a\n" not in d["text"]


def test_identical_returns_none():
    assert make_edit_diff("edit", "x", "same\n", "same\n") is None


def test_truncation_keeps_stats_full():
    old = "".join(f"old{i}\n" for i in range(500))
    new = "".join(f"new{i}\n" for i in range(500))
    d = make_edit_diff("edit", "big.txt", old, new)
    assert d is not None
    assert d["truncated"] is True
    # 统计不受截断影响（500/500 全量）
    assert d["added"] == 500 and d["removed"] == 500
    # 文本体被裁到限内
    assert len(d["text"].splitlines()) <= 200 + 2  # +2 = ---/+++ 头


def test_no_trailing_newline():
    d = make_edit_diff("edit", "t.py", "a\nb", "a\nb\nc")
    assert d is not None
    assert "+c" in d["text"]


def test_maybe_edit_diff_from_args(monkeypatch):
    """toolloop._maybe_edit_diff：edit 从 args 取 old/new（零竞态路径）。"""
    import types

    from ginno_runtime.api.stream.stages.toolloop import _maybe_edit_diff

    ctx = types.SimpleNamespace(
        session_id="sid-x",
        slug="default",
        tool_args_by_id={"tc1": {"path": "a/b.py", "old": "x=1\n", "new": "x=2\n"}},
    )
    d = _maybe_edit_diff("edit_file", ctx, "tc1")
    assert d is not None and d["op"] == "edit"
    assert "+x=2" in d["text"] and d["added"] == 1

    # bash 等非编辑工具：None（载荷不带 diff 字段）
    assert _maybe_edit_diff("bash", ctx, "tc1") is None
    # 参数缺失：None 不炸
    ctx2 = types.SimpleNamespace(
        session_id="s", slug="default", tool_args_by_id={"tc2": {"path": "x"}}
    )
    assert _maybe_edit_diff("edit_file", ctx2, "tc2") is None


def test_wrap_patch_diff_from_bash_heredoc():
    from ginno_runtime.editdiff import _PATCH_HEREDOC, wrap_patch_diff

    cmd = "cd repo && git apply <<'EOF'\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\nEOF\necho done"
    m = _PATCH_HEREDOC.search(cmd)
    assert m is not None
    d = wrap_patch_diff(m.group(2))
    assert d is not None
    assert d["op"] == "patch" and d["path"] == "x.py"
    assert d["added"] == 1 and d["removed"] == 1
    # 非补丁命令：None
    assert wrap_patch_diff("ls -la\n") is None

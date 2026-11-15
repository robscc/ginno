"""A2A 执行侧强制闸门（a2a/guard.py）单测——安全关键，表驱动覆盖矩阵。

权威验收：ginno-node-headless-design.md §2.4 + a2a-connector-design.md §11.3.3，
主设计 #31（双侧拒 readonly 写）/ #32（exec_root 越界 fail-loud）/ #33（full 仍拒改配置）
/ #34（readonly 假安全感警告文案存在）。

纪律：**绝不 mock 被测函数本身**——check_tool / check_capability / resolve_exec_root /
ensure_within_exec_root 全部真调；exec_root 用 tmp_path 真实目录 + 真实符号链接。
矩阵期望值由独立 oracle（_expected，编码设计规格）给出，与实现解耦。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ginno_runtime.a2a import guard
from ginno_runtime.a2a.guard import (
    CAP_LEVELS,
    DEFAULT_EXEC_ROOT,
    READONLY_WARNING,
    ExecRootError,
    ExecRootViolation,
    GuardDecision,
    check_capability,
    check_tool,
    classify_tool,
    ensure_within_exec_root,
    resolve_exec_root,
)

pytestmark = pytest.mark.unit

# caps 按显式授予集合（逐级追加）；完整能力 = L0..L3 全给。
CAPS = {
    "L0": ["L0"],
    "L1": ["L0", "L1"],
    "L2": ["L0", "L1", "L2"],
    "L3": ["L0", "L1", "L2", "L3"],
}
TRUSTS = ("readonly", "normal", "full")

# 矩阵工具面：读 / 写 / shell / 配置。shell 用写命令 `touch x`（代表「shell 作为写工具」，
# readonly 应拒）；readonly 只读白名单的放行 nuance 单独测。target_path=None 以隔离
# trust×caps 逻辑（exec_root 边界另有专组真实路径用例）。
MATRIX_TOOLS = {
    "read": ("read_file", {}),
    "write": ("write_file", {}),
    "shell": ("bash", {"command": "touch x"}),
    "config": ("update_settings", {}),
}


def _expected(trust: str, caps_level: str, tool: str) -> tuple[bool, str | None]:
    """独立 oracle：按设计规格给出 (allowed, denied_code)。不依赖 guard 实现。

    判定优先级与规格一致：配置写恒拒 → caps 越级 → trust 工具面。
    """
    if tool == "config":
        return False, "config_write_forbidden"          # #33：任何 trust/caps 都拒
    if tool == "read":
        return True, None                               # L0 在所有 cumulative caps 里
    if tool == "write":                                 # 需 L2
        if caps_level not in ("L2", "L3"):
            return False, "capability_not_granted"
        if trust == "readonly":
            return False, "readonly_write_forbidden"
        return True, None
    if tool == "shell":                                 # 需 L1；命令是写命令 touch
        if caps_level not in ("L1", "L2", "L3"):
            return False, "capability_not_granted"
        if trust == "readonly":
            return False, "readonly_shell_forbidden"
        return True, None
    raise AssertionError(f"unknown tool key {tool}")


def _matrix_rows():
    for trust in TRUSTS:
        for caps_level in CAP_LEVELS:
            for tool in MATRIX_TOOLS:
                allowed, code = _expected(trust, caps_level, tool)
                yield pytest.param(
                    trust, caps_level, tool, allowed, code,
                    id=f"{trust}-{caps_level}-{tool}")


@pytest.mark.parametrize("trust,caps_level,tool,allowed,code", list(_matrix_rows()))
def test_trust_caps_tool_matrix(trust, caps_level, tool, allowed, code):
    """trust × caps × 工具类型 完整矩阵（3×4×4=48 组合）。"""
    tool_name, kwargs = MATRIX_TOOLS[tool]
    decision = check_tool(trust, CAPS[caps_level], tool_name, **kwargs)
    assert isinstance(decision, GuardDecision)
    assert decision.allowed is allowed, (
        f"{trust}/{caps_level}/{tool}: expected allowed={allowed}, "
        f"got {decision.allowed} ({decision.code}: {decision.reason})")
    assert bool(decision) is allowed
    if not allowed:
        assert decision.code == code
        assert decision.reason  # 拒绝必须给出可读理由
    else:
        assert decision.code == "ok"


# --------------------------------------------------------------------------- #
# #33：full 仍拒改配置（trust 与配置权限正交）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool_name", [
    "update_settings", "set_config", "write_config", "apply_config",
    "write_mcp_config", "add_mcp_server", "install_skills", "uninstall_skill",
    "delete_model_config", "kb_wiki_put_config", "reset_provider_config",
])
@pytest.mark.parametrize("trust", TRUSTS)
def test_config_write_forbidden_at_every_trust(trust, tool_name):
    # 即使 full + 完整 caps，配置写仍拒（#33：full ≠ 可改配置）。
    decision = check_tool(trust, CAPS["L3"], tool_name)
    assert decision.allowed is False
    assert decision.code == "config_write_forbidden"


def test_full_trust_does_not_unlock_config():
    # 显式对照：full 能写普通文件，但同一 trust 下配置写被拒。
    assert check_tool("full", CAPS["L3"], "write_file").allowed is True
    assert check_tool("full", CAPS["L3"], "update_settings").allowed is False


def test_config_read_is_r0_allowed():
    # 配置只读（R0）对所有 trust 放行（§11.3.3 轴二：一律 R0 只读）。
    for trust in TRUSTS:
        assert check_tool(trust, CAPS["L0"], "get_config").allowed is True


# --------------------------------------------------------------------------- #
# #31：readonly 写双侧拒绝（派发前 + 执行侧复核）
# --------------------------------------------------------------------------- #
def test_readonly_touch_denied_both_sides():
    # 派发前
    dispatch = check_tool("readonly", CAPS["L3"], "bash", command="touch x")
    # 执行侧复核（同一闸门再调一次，必须同样拒绝——无 accept-then-ignore）
    execution = check_tool("readonly", CAPS["L3"], "bash", command="touch x")
    assert dispatch.allowed is False
    assert execution.allowed is False
    assert dispatch.code == execution.code == "readonly_shell_forbidden"


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "apply_patch"])
def test_readonly_file_write_denied_both_sides(tool_name):
    dispatch = check_tool("readonly", CAPS["L3"], tool_name)
    execution = check_tool("readonly", CAPS["L3"], tool_name)
    assert dispatch.allowed is False
    assert execution.allowed is False
    assert dispatch.code == execution.code == "readonly_write_forbidden"


def test_readonly_channel_send_denied():
    # 渠道发送属写工具，readonly 拒；normal/full 放行。
    assert check_tool("readonly", CAPS["L3"], "send_message").allowed is False
    assert check_tool("normal", CAPS["L3"], "send_message").allowed is True
    assert check_tool("full", CAPS["L3"], "send_image").allowed is True


def test_execution_side_path_recheck_fail_loud(tmp_path):
    # 派发侧对 root 内路径放行；执行侧对越界路径 fail-loud——两侧独立强制。
    root = tmp_path / "ws"
    root.mkdir()
    inside = root / "ok.txt"
    assert check_tool("normal", CAPS["L3"], "write_file",
                      exec_root=str(root), target_path=str(inside)).allowed is True
    outside = tmp_path / "outside.txt"
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root(str(outside), str(root))


# --------------------------------------------------------------------------- #
# caps 阶梯（越级拒绝 + trust 约束 caps）
# --------------------------------------------------------------------------- #
def test_check_capability_membership():
    assert check_capability(["L0"], "L0").allowed is True
    assert check_capability(["L0"], "L1").allowed is False
    assert check_capability(CAPS["L3"], "L3").allowed is True
    for level in CAP_LEVELS:
        assert check_capability(CAPS["L3"], level).allowed is True


def test_check_capability_fail_closed():
    assert check_capability([], "L0").allowed is False
    assert check_capability(None, "L0").allowed is False
    assert check_capability(["L0"], "L9").code == "unknown_capability"
    assert check_capability(["L0"], "L1").code == "capability_not_granted"


def test_readonly_with_full_caps_still_cannot_write():
    # 关键正交约束：caps 含 L3 也不越过 trust——readonly 仍拒写。
    decision = check_tool("readonly", CAPS["L3"], "write_file")
    assert decision.allowed is False
    assert decision.code == "readonly_write_forbidden"


def test_caps_over_level_denied_even_full_trust():
    # full trust 但只授予 L0：写（需 L2）越级拒。
    decision = check_tool("full", CAPS["L0"], "write_file")
    assert decision.allowed is False
    assert decision.code == "capability_not_granted"


def test_unknown_trust_rejected():
    assert check_tool("admin", CAPS["L3"], "read_file").code == "unknown_trust"
    assert check_tool("", CAPS["L3"], "read_file").code == "unknown_trust"


def test_unclassified_tool_fail_closed():
    decision = check_tool("full", CAPS["L3"], "totally_unknown_tool_xyz")
    assert decision.allowed is False
    assert decision.code == "unclassified_tool"


# --------------------------------------------------------------------------- #
# readonly shell 只读白名单（设计 nuance）+ #34 警告
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", ["ls", "ls -la", "cat foo.txt", "git status",
                                     "git log --oneline", "pip list", "head -n 5 x"])
def test_readonly_whitelisted_read_commands_allowed(command):
    decision = check_tool("readonly", CAPS["L1"], "bash", command=command)
    assert decision.allowed is True, decision.reason


@pytest.mark.parametrize("command", ["touch x", "rm foo", "echo hi", "sudo ls",
                                     "lsof", "git push", None, ""])
def test_readonly_nonwhitelisted_shell_denied(command):
    decision = check_tool("readonly", CAPS["L3"], "bash", command=command)
    assert decision.allowed is False
    assert decision.code == "readonly_shell_forbidden"


@pytest.mark.parametrize("command", ["ls; rm x", "ls | rm x", "ls && rm x",
                                     "ls > /etc/passwd", "cat $(rm x)", "ls\nrm x"])
def test_readonly_shell_metachar_injection_denied(command):
    # 纵深防御：链式/重定向注入被拒。但这不是安全边界——警告如实声明（#34）。
    decision = check_tool("readonly", CAPS["L3"], "bash", command=command)
    assert decision.allowed is False
    assert decision.code == "readonly_shell_forbidden"


def test_readonly_warning_exists_and_honest():
    # #34：验收确认警告文案存在，含「仅防误写」语义，不假装能拦注入。
    assert isinstance(READONLY_WARNING, str)
    assert READONLY_WARNING.strip()
    assert "仅防误写" in READONLY_WARNING
    assert "不防恶意" in READONLY_WARNING
    assert "ls; rm" in READONLY_WARNING


def test_readonly_warning_rides_on_shell_rejection():
    # 拒绝理由里带上警告，调用方/工具描述可直接引用。
    decision = check_tool("readonly", CAPS["L3"], "bash", command="touch x")
    assert "仅防误写" in decision.reason


# --------------------------------------------------------------------------- #
# exec_root 边界：≥5 种穿越手法实证（#32 fail-loud，绝不静默修正）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def root(tmp_path):
    r = tmp_path / "ws"
    r.mkdir()
    (tmp_path / "outside").mkdir()
    return r


def test_traversal_dotdot(root, tmp_path):
    # 手法 1：`..` 穿越。
    target = str(root / ".." / "outside" / "secret.txt")
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root(target, str(root))
    d = check_tool("normal", CAPS["L3"], "write_file",
                   exec_root=str(root), target_path=target)
    assert d.allowed is False and d.code == "exec_root_violation"


def test_traversal_absolute_outside(root, tmp_path):
    # 手法 2：绝对路径直接越界。
    target = str(tmp_path / "outside" / "secret.txt")
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root(target, str(root))


def test_traversal_symlink_escape(root, tmp_path):
    # 手法 3：root 内符号链接指向 root 外。
    link = root / "evil_link"
    os.symlink(tmp_path / "outside", link)
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root(str(link / "secret.txt"), str(root))
    # 链接本身作为目标也越界。
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root(str(link), str(root))


def test_traversal_relative_normalized_escape(root, tmp_path):
    # 手法 4：相对路径归一化后越界（sub/../../outside）。
    (root / "sub").mkdir()
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root("sub/../../outside/secret.txt", str(root))


def test_traversal_symlink_file_escape(root, tmp_path):
    # 手法 5：指向 root 外 *文件* 的符号链接。
    outside_file = tmp_path / "outside" / "data.txt"
    outside_file.write_text("sensitive")
    link = root / "file_link"
    os.symlink(outside_file, link)
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root(str(link), str(root))


def test_traversal_deep_dotdot_then_escape(root, tmp_path):
    # 手法 6：先深入再回退越界（a/b/../../../outside）。
    (root / "a" / "b").mkdir(parents=True)
    with pytest.raises(ExecRootViolation):
        ensure_within_exec_root("a/b/../../../outside/x", str(root))


def test_legit_inside_paths_allowed(root):
    # root 内合法路径放行：绝对 / 相对 / 深层 / 恰好 root。
    assert ensure_within_exec_root(str(root / "sub" / "f.txt"), str(root))
    assert ensure_within_exec_root("sub/f.txt", str(root))
    assert ensure_within_exec_root(str(root), str(root))
    # `..` 回退后仍在 root 内 → 放行（不是盲目拒 `..`，只拒越界）。
    (root / "a" / "b").mkdir(parents=True)
    resolved = ensure_within_exec_root("a/b/../../c.txt", str(root))
    assert resolved == str((root / "c.txt").resolve())


def test_symlink_inside_root_allowed(root):
    # root 内符号链接指向 root 内另一处 → 放行（归一化后仍在边界内）。
    (root / "real").mkdir()
    os.symlink(root / "real", root / "inner_link")
    resolved = ensure_within_exec_root(str(root / "inner_link" / "f.txt"), str(root))
    assert resolved == str((root / "real" / "f.txt").resolve())


def test_check_tool_target_without_root_fail_closed(root):
    d = check_tool("normal", CAPS["L3"], "write_file", target_path=str(root / "x"))
    assert d.allowed is False and d.code == "exec_root_violation"


def test_check_tool_read_also_root_bound(root, tmp_path):
    # 入站读取同样限 root 内（§2.5：避免文件系统被远程读穿）。
    inside = check_tool("readonly", CAPS["L0"], "read_file",
                        exec_root=str(root), target_path=str(root / "a.txt"))
    assert inside.allowed is True
    outside = check_tool("readonly", CAPS["L0"], "read_file",
                         exec_root=str(root), target_path=str(tmp_path / "outside" / "s"))
    assert outside.allowed is False and outside.code == "exec_root_violation"


# --------------------------------------------------------------------------- #
# resolve_exec_root 归一化 + 非法配置 fail-loud
# --------------------------------------------------------------------------- #
def test_resolve_default():
    resolved = resolve_exec_root(None)
    assert Path(resolved).is_absolute()
    assert resolved.endswith("ginno-a2a-workspace")
    assert resolved == str(Path(DEFAULT_EXEC_ROOT).expanduser().resolve())
    assert resolve_exec_root("") == resolved


def test_resolve_expanduser_and_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_exec_root("relroot") == str((tmp_path / "relroot").resolve())
    assert resolve_exec_root("~") == str(Path.home().resolve())


def test_resolve_symlink_root_canonical(tmp_path):
    real = tmp_path / "real_root"
    real.mkdir()
    link = tmp_path / "link_root"
    os.symlink(real, link)
    assert resolve_exec_root(str(link)) == str(real.resolve())


def test_resolve_existing_file_root_fails(tmp_path):
    f = tmp_path / "not_a_dir"
    f.write_text("x")
    with pytest.raises(ExecRootError):
        resolve_exec_root(str(f))


def test_resolve_nonexistent_dir_ok(tmp_path):
    # root 可以尚不存在（执行层负责创建），不因此 fail。
    p = tmp_path / "will_create"
    assert resolve_exec_root(str(p)) == str(p.resolve())


# --------------------------------------------------------------------------- #
# classify_tool 分类 sanity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,category", [
    ("read_file", "read"), ("glob_files", "read"), ("grep_files", "read"),
    ("write_file", "write"), ("edit_file", "write"), ("apply_patch", "write"),
    ("bash", "shell"), ("repl", "shell"),
    ("send_message", "send"), ("send_image", "send"),
    ("update_settings", "config_write"), ("install_skills", "config_write"),
    ("get_config", "read"), ("workflow_run", "agent_op"),
    ("no_such_tool", "unknown"),
])
def test_classify_tool(name, category):
    cat, _cap = classify_tool(name)
    assert cat == category


def test_decision_is_frozen_and_bool():
    d = GuardDecision.permit()
    assert bool(d) is True
    with pytest.raises(Exception):
        d.allowed = False  # frozen dataclass 不可变

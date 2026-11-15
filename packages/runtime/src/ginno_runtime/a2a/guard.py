"""A2A 执行侧强制：trust / caps / exec_root 三重闸门（安全关键模块）。

权威设计：``docs/design/ginno-node-headless-design.md`` §2.4 与
``docs/design/a2a-connector-design.md`` §11.3.3（两个正交轴）、验收 #31/#32/#33/#34。

**为什么存在**：P0 的 ``peers.py`` 只 *存储* trust/caps，派发与执行侧都不复核。
没有本模块，「配置了 readonly 却实际能写」就是 accept-then-ignore 漏洞。
node 一旦有执行层就 *必须* 强制（§2.4）。

三个正交约束，缺一不可：

1. **trust 轴（能做什么）** —— ``readonly`` 拒一切写工具（文件写 / shell / 渠道发送），
   shell 仅放行只读命令前缀白名单；``normal`` 允许 exec_root 内写；``full`` 仍拒改配置
   （#33：full ≠ 可改配置，执行权限与配置权限分开授予）。
2. **caps 阶梯（L0–L3，与 trust 正交）** —— L0 观测 / L1 只读执行 / L2 文件传取 /
   L3 完整 turn。**越级拒绝**：请求的能力不在 caps 列表里即拒。caps 受 trust 约束：
   readonly 即使 caps 含 L3 也不能写。caps 按 *显式授予集合* 做成员判定（fail-closed）：
   完整能力请授予 ``["L0","L1","L2","L3"]``（PeerRecord 默认 ``["L0"]``，逐级追加）。
3. **exec_root 边界** —— 默认 ``~/ginno-a2a-workspace``。越界 **fail-loud 拒绝，绝不静默
   修正**（#32）。防 ``..`` 穿越、符号链接、绝对路径越界、相对路径归一化后越界。

**双侧强制（#31）**：派发前调 :func:`check_tool`（返回结构化 decision，调用方决定转
403 还是工具错误）；执行前再调一次 :func:`check_tool`，并用 :func:`ensure_within_exec_root`
做 fail-loud 路径复核。两侧共用同一套判定逻辑，保证一致、无 accept-then-ignore。

**readonly 的诚实警告（#34）**：前缀匹配拦不住管道/子命令注入（``ls; rm``）。
readonly **仅防误写，不防恶意**。:data:`READONLY_WARNING` 必须被工具描述引用。
本模块在白名单之外额外拒绝 shell 元字符（纵深防御），但 *不假装* 这是安全边界。

本模块只依赖标准库，自包含、可被执行层（``a2a/inbound_exec.py``）直接复用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
TRUST_LEVELS = ("readonly", "normal", "full")
CAP_LEVELS = ("L0", "L1", "L2", "L3")

#: 入站执行 root 边界默认值（主设计 §11.3.1 / settings 表）。位于用户 home，
#: 而非 ~/.ginno 状态目录——这是给对端干活的工作区，不是 Ginno 元数据。
DEFAULT_EXEC_ROOT = "~/ginno-a2a-workspace"

#: readonly 只读命令前缀白名单（主设计 settings 表 readonly_cmd_prefixes 默认值）。
#: 按 *词边界* 前缀匹配（``ls`` 不会误配 ``lsof``）。**仅防误写，不防恶意**。
READONLY_CMD_PREFIXES = (
    "ls", "cat", "head", "tail", "wc", "du", "df", "ps",
    "git status", "git log", "git diff", "pip list",
)

#: readonly 假安全感警告（#34）。工具描述必须引用；测试断言其存在且含「仅防误写」。
READONLY_WARNING = (
    "readonly 信任级别仅防误写，不防恶意：只读命令白名单是前缀匹配，"
    "拦不住管道/子命令注入（如 `ls; rm`）。它不是安全边界，"
    "真敏感机器建议只在可信网内挂载。"
)

# 拒绝码（结构化 decision.code，调用方可据此映射 403 / 工具错误）。
CODE_OK = "ok"
CODE_UNKNOWN_TRUST = "unknown_trust"
CODE_UNKNOWN_CAPABILITY = "unknown_capability"
CODE_CAPABILITY_NOT_GRANTED = "capability_not_granted"
CODE_CONFIG_WRITE_FORBIDDEN = "config_write_forbidden"   # #33：任何 trust 都拒
CODE_READONLY_WRITE_FORBIDDEN = "readonly_write_forbidden"
CODE_READONLY_SHELL_FORBIDDEN = "readonly_shell_forbidden"
CODE_EXEC_ROOT_VIOLATION = "exec_root_violation"          # #32：越界
CODE_UNCLASSIFIED_TOOL = "unclassified_tool"              # fail-closed：未识别工具

# shell 元字符：readonly 命令含任一则拒（纵深防御，拦常见链式/重定向注入）。
# 注意：这 *不是* 完备的注入防御，READONLY_WARNING 已如实声明。
_SHELL_META = (";", "|", "&", "`", "$(", ">", "<", "\n", "\r")

# --------------------------------------------------------------------------- #
# 工具分类（trust 轴的工具面）
# --------------------------------------------------------------------------- #
# 读 / 观测（L0）。入站读取同样限定在 exec_root 内（§2.5：避免文件系统被远程读穿）。
_READ_TOOLS = frozenset({
    "read_file", "glob_files", "grep_files", "list_skills", "parse_document",
    "web_search", "web_fetch", "browser_read_page", "browser_page_text",
    "browser_find", "browser_console", "browser_network", "browser_tabs_context",
    "workflow_list", "workflow_get", "workflow_run_status", "bg_list", "bg_logs",
    # 配置只读（R0）：所有 trust 一律 R0 只读（§11.3.3 轴二）。响应脱敏（#29）
    # 是响应层的职责，不在本闸门。
    "get_config", "get_settings", "config_schema", "get_model_configs",
    "list_model_config_models",
})

# 文件写（L2 = 文件传取的 put 侧）。readonly 拒；normal/full 限 exec_root 内。
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})

# shell / 代码执行（L1）。readonly 仅只读前缀白名单；normal/full 限 exec_root 内。
_SHELL_TOOLS = frozenset({"bash", "repl"})

# 渠道发送（L3，主动外发）。readonly 拒；normal/full 放行。
_SEND_PREFIX = "send_"

# 配置 *写* 检测（#33）：配置关键词 + 变更动词。命中即任何 trust 都拒。
# 这是纵深防御——主防线是路由层根本不注册配置写路由（#28 → 404）。
_CONFIG_KEYWORDS = ("config", "settings", "mcp", "skill", "provider")
_CONFIG_MUTATION_VERBS = (
    "set", "update", "write", "add", "install", "uninstall", "remove", "delete",
    "apply", "put", "create", "edit", "amend", "save", "reset", "revoke", "register",
)

# 已知的主动型 agent 操作（L3）。readonly 拒；normal/full 放行。未列出的工具
# 一律 fail-closed 拒绝（CODE_UNCLASSIFIED_TOOL）——执行层要暴露新工具须先扩展分类。
_AGENT_OP_TOOLS = frozenset({
    "workflow_run", "workflow_create", "workflow_step", "workflow_dry_run",
    "workflow_propose_edit", "bg_run", "bg_stop", "bg_remove", "delegate_agent",
    "create_subagent_plan", "amend_plan", "cancel_plan", "use_skill", "ask_user",
    "render_widget", "attach_ref", "artifact_register", "a2a_delegate", "a2a_task",
})

# 工具类别 → 所需 caps 级别。
_CAP_BY_CATEGORY = {
    "read": "L0",
    "shell": "L1",
    "write": "L2",
    "send": "L3",
    "agent_op": "L3",
    "config_read": "L0",
}


def classify_tool(tool_name: str) -> tuple[str, str | None]:
    """把工具名映射为 ``(category, required_cap)``。

    配置写优先判定（任何 trust 都拒）；未识别工具返回 ``("unknown", None)``，
    由 :func:`check_tool` fail-closed 拒绝。分类是 *显式白名单*：宁可拒错，不可放行。
    """
    name = (tool_name or "").strip().lower()
    if not name:
        return "unknown", None

    # 配置写最先判：full 也拒（#33）。配置关键词 + 变更动词即命中。
    if any(k in name for k in _CONFIG_KEYWORDS) and any(
            v in name for v in _CONFIG_MUTATION_VERBS):
        return "config_write", None

    if name in _READ_TOOLS:
        return "read", _CAP_BY_CATEGORY["read"]
    if name in _WRITE_TOOLS:
        return "write", _CAP_BY_CATEGORY["write"]
    if name in _SHELL_TOOLS:
        return "shell", _CAP_BY_CATEGORY["shell"]
    if name.startswith(_SEND_PREFIX):
        return "send", _CAP_BY_CATEGORY["send"]
    if name in _AGENT_OP_TOOLS:
        return "agent_op", _CAP_BY_CATEGORY["agent_op"]
    return "unknown", None


# --------------------------------------------------------------------------- #
# 结构化判定
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GuardDecision:
    """一次闸门判定的结果。``allowed`` 为假时 ``code``/``reason`` 说明拒绝原因。

    不抛异常——调用方（派发层 / 执行层）自行决定转 HTTP 403 还是工具错误。
    exec_root 越界的 fail-loud 变体见 :func:`ensure_within_exec_root`。
    """

    allowed: bool
    reason: str
    code: str

    def __bool__(self) -> bool:  # 便于 `if decision:` 直用
        return self.allowed

    @classmethod
    def permit(cls, reason: str = "allowed") -> GuardDecision:
        return cls(True, reason, CODE_OK)

    @classmethod
    def deny(cls, code: str, reason: str) -> GuardDecision:
        return cls(False, reason, code)


class GuardError(Exception):
    """本模块 fail-loud 异常的基类。"""


class ExecRootError(GuardError):
    """exec_root 配置非法（无法归一化为绝对目录路径）。"""


class ExecRootViolation(ExecRootError):
    """目标路径越出 exec_root 边界（#32）。fail-loud，绝不静默修正。"""

    def __init__(self, target: str, root: str) -> None:
        self.target = target
        self.root = root
        super().__init__(
            f"path {target!r} escapes exec_root {root!r}; refusing (no silent fix)")


# --------------------------------------------------------------------------- #
# caps 阶梯
# --------------------------------------------------------------------------- #
def check_capability(caps: list[str] | None, required: str) -> GuardDecision:
    """caps 成员判定（越级拒绝）。``required`` 必须显式在 ``caps`` 授予集合里。

    fail-closed：空 caps / 未知 required 一律拒。caps 按显式集合授予（完整能力
    给 ``["L0","L1","L2","L3"]``）；不做隐式阶梯推断，避免「授予 L3 就默认放开一切」
    的越权歧义。trust 约束在 :func:`check_tool` 里叠加（readonly 有 L3 也不能写）。
    """
    if required not in CAP_LEVELS:
        return GuardDecision.deny(
            CODE_UNKNOWN_CAPABILITY, f"unknown capability level {required!r}")
    granted = {str(c) for c in (caps or [])}
    if required in granted:
        return GuardDecision.permit(f"capability {required} granted")
    return GuardDecision.deny(
        CODE_CAPABILITY_NOT_GRANTED,
        f"capability {required} not in granted caps {sorted(granted)}")


# --------------------------------------------------------------------------- #
# exec_root 边界
# --------------------------------------------------------------------------- #
def resolve_exec_root(raw: str | None) -> str:
    """归一化 exec_root 配置为绝对规范路径；非法则 fail-loud 抛 :class:`ExecRootError`。

    ``None``/空 → 默认 :data:`DEFAULT_EXEC_ROOT`。展开 ``~``、解析符号链接与 ``..``。
    root 本身是边界，不存在「越界」；此处的 fail-loud 针对 *非法配置*（无法解析、
    或指向一个已存在的非目录）。目录可以尚不存在（执行层负责创建）。
    """
    candidate = (raw or "").strip() or DEFAULT_EXEC_ROOT
    try:
        root = Path(candidate).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as e:
        raise ExecRootError(f"cannot resolve exec_root {candidate!r}: {e}") from e
    if not str(root) or not root.is_absolute():
        raise ExecRootError(f"exec_root {candidate!r} did not resolve to an absolute path")
    if root.exists() and not root.is_dir():
        raise ExecRootError(f"exec_root {candidate!r} exists but is not a directory")
    return str(root)


def _resolve_within(target_path: str, exec_root: str) -> Path:
    """共享边界逻辑：把 target 归一化后校验落在 exec_root 内，越界抛 ExecRootViolation。

    相对路径以 exec_root 为基（入站 cwd 即 root）；``resolve()`` 同时吃掉 ``..`` 与
    符号链接，故 ``..`` 穿越 / 符号链接逃逸 / 绝对路径越界 / 归一化后越界都被拦截。
    派发侧（check_tool）与执行侧（ensure_within_exec_root）共用本函数 → 双侧一致。
    """
    root = Path(exec_root).expanduser().resolve()
    t = Path(target_path).expanduser()
    if not t.is_absolute():
        t = root / t
    resolved = t.resolve()
    if resolved != root and root not in resolved.parents:
        raise ExecRootViolation(str(resolved), str(root))
    return resolved


def ensure_within_exec_root(target_path: str, exec_root: str) -> str:
    """执行侧 fail-loud 路径复核（#32）。越界抛 :class:`ExecRootViolation`，绝不静默修正。

    返回归一化后的绝对路径。执行层在真正落盘 / 起进程前调用。
    """
    return str(_resolve_within(target_path, exec_root))


# --------------------------------------------------------------------------- #
# readonly shell 白名单
# --------------------------------------------------------------------------- #
def _readonly_shell_ok(command: str | None) -> bool:
    """readonly 下 shell 命令是否放行：只读前缀白名单 + 元字符纵深防御。

    无命令 → 拒（无法验证只读，fail-closed）。含 shell 元字符 → 拒（拦 ``ls; rm``
    这类链式注入）。否则按词边界前缀匹配白名单。**这仅防误写，不防恶意**——
    见 :data:`READONLY_WARNING`，白名单不是安全边界。
    """
    if not command or not command.strip():
        return False
    cmd = command.strip()
    if any(meta in cmd for meta in _SHELL_META):
        return False
    for prefix in READONLY_CMD_PREFIXES:
        if cmd == prefix or cmd.startswith(prefix + " ") or cmd.startswith(prefix + "\t"):
            return True
    return False


# --------------------------------------------------------------------------- #
# 工具级三重闸门
# --------------------------------------------------------------------------- #
def check_tool(
    trust: str,
    caps: list[str] | None,
    tool_name: str,
    *,
    exec_root: str | None = None,
    target_path: str | None = None,
    command: str | None = None,
) -> GuardDecision:
    """派发前 / 执行前复用的工具级闸门：trust × caps × exec_root 三重强制。

    判定顺序（fail-closed）：配置写 → caps 越级 → trust 工具面 → exec_root 边界。
    返回结构化 :class:`GuardDecision`（不抛异常）；exec_root 越界在此返回拒绝 decision，
    执行侧若要 fail-loud 请改用 :func:`ensure_within_exec_root`。

    参数：
    - ``trust``：readonly/normal/full；未知值拒（不静默降级）。
    - ``caps``：显式授予的能力集合；所需级别不在其中即越级拒。
    - ``tool_name``：被派发/执行的工具名。
    - ``exec_root``：root 边界；给 ``target_path`` 时必给（否则无法校验边界）。
    - ``target_path``：工具作用的路径（shell 为 cwd）。提供即校验落在 exec_root 内。
    - ``command``：shell 命令串；readonly 下据此做只读白名单判定。
    """
    if trust not in TRUST_LEVELS:
        return GuardDecision.deny(
            CODE_UNKNOWN_TRUST, f"unknown trust level {trust!r}")

    category, required_cap = classify_tool(tool_name)

    # 1) 配置写：任何 trust 都拒（#33：full ≠ 可改配置）。最先判，绝不因 full 放行。
    if category == "config_write":
        return GuardDecision.deny(
            CODE_CONFIG_WRITE_FORBIDDEN,
            f"tool {tool_name!r} mutates config; forbidden at every trust level "
            "(full != config-write, design #33)")

    # 未识别工具：fail-closed。执行层要暴露新工具须先扩展 classify_tool。
    if category == "unknown":
        return GuardDecision.deny(
            CODE_UNCLASSIFIED_TOOL,
            f"tool {tool_name!r} is not classified; refusing (fail-closed)")

    # 2) caps 越级：所需能力不在授予集合即拒。
    cap_decision = check_capability(caps, required_cap)
    if not cap_decision.allowed:
        return cap_decision

    # 3) trust 工具面。
    if category in ("read", "config_read"):
        pass  # 所有 trust 均可读（R0）。
    elif category == "shell":
        if trust == "readonly" and not _readonly_shell_ok(command):
            return GuardDecision.deny(
                CODE_READONLY_SHELL_FORBIDDEN,
                f"readonly shell rejected for {tool_name!r}: command not in read-only "
                f"prefix whitelist (or carries shell metacharacters). {READONLY_WARNING}")
    elif category in ("write", "send", "agent_op"):
        if trust == "readonly":
            return GuardDecision.deny(
                CODE_READONLY_WRITE_FORBIDDEN,
                f"trust=readonly forbids write/send/active tool {tool_name!r}")
    # normal/full：write/send/agent_op/shell 放行（边界在第 4 步校验）。

    # 4) exec_root 边界：给了 target_path 就校验（读也限 root 内，§2.5）。
    if target_path is not None:
        if exec_root is None:
            return GuardDecision.deny(
                CODE_EXEC_ROOT_VIOLATION,
                "target_path given without exec_root; cannot verify boundary (fail-closed)")
        try:
            root = resolve_exec_root(exec_root)
            _resolve_within(target_path, root)
        except ExecRootError as e:
            return GuardDecision.deny(CODE_EXEC_ROOT_VIOLATION, str(e))

    return GuardDecision.permit(
        f"tool {tool_name!r} allowed (trust={trust}, cap={required_cap})")


__all__ = [
    "TRUST_LEVELS",
    "CAP_LEVELS",
    "DEFAULT_EXEC_ROOT",
    "READONLY_CMD_PREFIXES",
    "READONLY_WARNING",
    "CODE_OK",
    "CODE_UNKNOWN_TRUST",
    "CODE_UNKNOWN_CAPABILITY",
    "CODE_CAPABILITY_NOT_GRANTED",
    "CODE_CONFIG_WRITE_FORBIDDEN",
    "CODE_READONLY_WRITE_FORBIDDEN",
    "CODE_READONLY_SHELL_FORBIDDEN",
    "CODE_EXEC_ROOT_VIOLATION",
    "CODE_UNCLASSIFIED_TOOL",
    "GuardDecision",
    "GuardError",
    "ExecRootError",
    "ExecRootViolation",
    "classify_tool",
    "check_capability",
    "resolve_exec_root",
    "ensure_within_exec_root",
    "check_tool",
]

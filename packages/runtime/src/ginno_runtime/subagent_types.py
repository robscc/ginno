"""Subagent 类型注册表（P3 契约 1）：``~/.ginno/agents/subagents/*.md``。

一个类型文件 = 一种可委派的 subagent 形态：

.. code-block:: markdown

    ---
    name: researcher          # 唯一，kebab-case（spawn_subagent(agent_type=…) 的取值）
    description: 只读调研…     # 主 agent 路由依据（写进系统提示的类型清单）
    tools_allow:              # 可选，fnmatch 白名单；与父 persona tools_allow 求交
      - read_file
      - grep_files
    model:                    # 可选，"provider_id/model" 或裸模型名；留空继承父
    ---
    markdown body = persona 补充系统提示（spawn 时拼在 standalone brief 前）

容错契约：坏文件（frontmatter 缺失 / YAML 解析失败 / 缺 name / name 非法 /
重名）逐个跳过并 log warning，绝不拖垮其余类型，更不拖垮 spawn。

缓存：进程内缓存按目录状态（文件名 + mtime_ns + size）失效——增删改类型文件
后下一次读取自动生效，无需重启，也无需显式热重载开关。每次 ``spawn`` 和每
次系统提示构建只付一次 ``glob + stat`` 的代价。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

_log = logging.getLogger(__name__)

# kebab-case：小写字母/数字开头，允许内部连字符。大写名字自动 lowercase 后
# 再校验——宽容拼写、严守命名空间。
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass
class SubagentType:
    name: str
    description: str
    tools_allow: list[str] = field(default_factory=list)
    model: str = ""
    body: str = ""
    source: str = ""


# 进程内缓存（见模块 docstring）。_LOCK 序列化「stat → 解析 → 换缓存」，
# 两个并发 spawn 不会各自解析一遍。
_CACHE: dict[str, SubagentType] | None = None
_DIR_STATE: list | None = None
_LOCK = threading.Lock()


def subagents_dir() -> Path:
    return paths.agents_dir() / "subagents"


def _as_pattern_list(value) -> list[str]:
    """frontmatter tools_allow 容错：list 或逗号分隔字符串都接受。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(value).strip()]


def _dir_state() -> list:
    """[(name, mtime_ns, size), …] — 目录内容指纹；目录不存在 = 空注册表。"""
    d = subagents_dir()
    if not d.is_dir():
        return []
    out: list = []
    for p in sorted(d.glob("*.md")):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((p.name, st.st_mtime_ns, st.st_size))
    return out


def _parse_file(p: Path) -> SubagentType | None:
    """One file → one type, or None (skip + warn) on any malformation."""
    from .knowledge.frontmatter import split_frontmatter

    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        _log.warning("subagent_type_unreadable file=%s error=%s", p.name, e)
        return None
    meta, body = split_frontmatter(raw)
    if not meta:
        _log.warning("subagent_type_skipped file=%s reason=no-frontmatter", p.name)
        return None
    name = str(meta.get("name") or "").strip().lower()
    if not name:
        _log.warning("subagent_type_skipped file=%s reason=missing-name", p.name)
        return None
    if not _NAME_RE.match(name):
        _log.warning(
            "subagent_type_skipped file=%s reason=bad-name name=%r", p.name, name
        )
        return None
    tools = _as_pattern_list(meta.get("tools_allow"))
    if meta.get("tools_allow") is not None and not tools:
        _log.warning(
            "subagent_type_empty_tools_allow file=%s name=%s（视为不收紧）", p.name, name
        )
    return SubagentType(
        name=name,
        description=str(meta.get("description") or "").strip(),
        tools_allow=tools,
        model=str(meta.get("model") or "").strip(),
        body=(body or "").strip(),
        source=str(p),
    )


def load_subagent_types(force: bool = False) -> dict[str, SubagentType]:
    """All valid types keyed by name. Tolerant: malformed files are skipped
    with a warning (never raise); duplicate names keep the FIRST (sorted file
    order) and warn about the loser."""
    global _CACHE, _DIR_STATE
    state = _dir_state()
    with _LOCK:
        if not force and _CACHE is not None and _DIR_STATE == state:
            return _CACHE
        types: dict[str, SubagentType] = {}
        for p in sorted(subagents_dir().glob("*.md")):
            t = _parse_file(p)
            if t is None:
                continue
            if t.name in types:
                _log.warning(
                    "subagent_type_duplicate name=%s keep=%s dropped=%s",
                    t.name, types[t.name].source, p.name,
                )
                continue
            types[t.name] = t
        _CACHE = types
        _DIR_STATE = state
        return types


def get_subagent_type(name: str) -> SubagentType | None:
    return load_subagent_types().get((name or "").strip().lower())


def describe_types() -> list[dict]:
    """[{name, description}] sorted by name — the system-prompt routing list
    and the unknown-type error's 可用清单 both render from this."""
    return [
        {"name": t.name, "description": t.description}
        for t in sorted(load_subagent_types().values(), key=lambda t: t.name)
    ]


def restrict_for_meta(meta: dict | None) -> list[str]:
    """The tools_allow patterns a session's own type imposes (session rebuild
    path: the registry is re-read fresh, so a type edited between restarts
    re-tightens; a type DELETED since spawn loosens back to the parent persona
    — documented degradation, the meta stays the record of what was asked)."""
    if not meta:
        return []
    name = str(((meta.get("subagent") or {}).get("agent_type")) or "").strip()
    if not name:
        return []
    t = get_subagent_type(name)
    return list(t.tools_allow) if t else []


def resolve_type_model(st: SubagentType | None, fallback: tuple[str, str]) -> tuple[str, str]:
    """(provider, model) after applying a type's ``model`` frontmatter.

    ``provider/model`` wins only when the prefix names a KNOWN config id;
    anything else (bare model name, unknown prefix) rides the parent's
    provider — a typo'd provider must not silently break the spawn.
    """
    provider, model_name = fallback
    if st is None or not st.model:
        return provider, model_name
    raw = st.model.strip()
    if "/" in raw:
        p, m = raw.split("/", 1)
        try:
            from . import providers as prov_mod

            known = any(c.get("id") == p for c in prov_mod.load_configs())
        except Exception:
            known = False
        if known and m:
            return p, m
        _log.warning(
            "subagent_type_model_fallback type=%s model=%r（前缀不是已知 provider）",
            st.name, raw,
        )
    return provider, raw


# --------------------------------------------------------------------------- #
# 种子类型（开箱可用；只在目录不存在或为空时写入）
# --------------------------------------------------------------------------- #
# 与 agents 注册表（dev/research/writer 种子）同一惯例：空注册表不是「加载
# 失败」而是「没定义过任何类型」，出厂就该有可用的默认形态。工具名取
# build_builtin_tools 的六个内置（read_file/write_file/edit_file/glob_files/
# grep_files/bash）+ web_search/web_fetch。
_SEED_TYPES: dict[str, str] = {
    "explore": """---
name: explore
description: 只读调研。代码库探查、资料搜集、定位实现与依赖关系，只读不改动任何文件。适合「先摸清现状再决定怎么做」的委派。
tools_allow:
  - read_file
  - glob_files
  - grep_files
  - web_search
  - web_fetch
---
你是只读调研型子代理：只使用读取类工具（read_file / glob_files / grep_files
/ web_search / web_fetch）。不要尝试写文件或执行会改变文件系统的命令。

工作方式：先广后深——先定位相关文件与入口，再逐个读关键片段确认，最后给出
带文件路径与行号的结论。拿不准的地方明确标注「未验证」，不要用推测填空。
""",
    "researcher": """---
name: researcher
description: 联网研究。跨来源检索、交叉验证、带引用地汇总某个主题的最新信息。适合需要时效性与多方来源的委派。
tools_allow:
  - web_search
  - web_fetch
  - read_file
  - glob_files
  - grep_files
---
你是联网研究型子代理：以 web_search / web_fetch 为主获取信息，用多个独立来源
交叉验证后再下结论。

纪律：每条结论标注来源（标题 + 链接）与时间；来源单一或时效不明时显式标注
不确定性；检索失败（限流/被拦）时换引擎或改走直接抓取，并在报告里说明哪部分
没能覆盖。不要编造数据。
""",
    "reviewer": """---
name: reviewer
description: 代码/方案评审。只读审查实现或设计，给出可核对的问题清单与结论；可以跑测试或 lint 取证，但不改动代码。
tools_allow:
  - read_file
  - glob_files
  - grep_files
  - bash
---
你是评审型子代理：找出正确性问题（不是风格偏好），每条给出位置（文件:行）、
失败场景与严重度。可以运行测试/lint/类型检查来取证，但不要修改任何文件。

结论先行：先给整体判断，再列问题清单（按严重度排序）。没有问题就明说「未发现
问题」，不要为了凑数报无关紧要的观感。
""",
    "implementer": """---
name: implementer
description: 并行实现。在明确的文件范围内独立完成一块编码任务（新增文件或改动约定好的模块），适合可切分且互不冲突的实现工作。
tools_allow:
  - read_file
  - write_file
  - edit_file
  - glob_files
  - grep_files
  - bash
---
你是实现型子代理：在 goal 约定的范围内直接改代码，完成后自测（跑相关测试或
最小验证），并在最终报告里给出：改了哪些文件、关键取舍、验证方式与结果、
遗留问题。

边界纪律：只动 goal 指定范围内的文件；发现需要改动范围外的代码时，在报告里
说明而不是直接改。与其它并行子代理共同工作时，避免碰不属于你的文件。
""",
}


def ensure_seeded() -> None:
    """把种子类型落到磁盘——只在目录不存在或没有任何 ``*.md`` 时写入。

    幂等：用户删掉某个种子文件后不会被重新写回（目录非空即跳过），
    与 agents 注册表的 ensure_seeded 同语义。"""
    d = subagents_dir()
    try:
        if d.is_dir() and any(d.glob("*.md")):
            return
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        _log.warning("subagent_types_seed_mkdir_failed dir=%s", d, exc_info=True)
        return
    for name, content in _SEED_TYPES.items():
        p = d / f"{name}.md"
        try:
            if not p.exists():
                p.write_text(content, encoding="utf-8")
                _log.info("subagent_type_seeded name=%s path=%s", name, p)
        except OSError:
            _log.warning("subagent_type_seed_write_failed name=%s", name, exc_info=True)

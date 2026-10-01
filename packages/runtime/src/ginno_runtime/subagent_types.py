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

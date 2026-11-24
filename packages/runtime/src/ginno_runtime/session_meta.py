"""Session metadata (per-project ``sessions/_index.json``) access helpers.

Shared by the sessions, usage, files and streaming modules — kept out of
server.py so api/ router modules can use them without importing the app
module (which would create an import cycle).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from ginno_runtime import jsonx as json

from . import paths
from .server_shared import _SESSIONS

# 2026-10-05 事故:20 路并发委托各自「读-改-写」同一 _index.json,最后写的
# 抱着过期快照把其余条目全部踩掉(索引只剩 1 条、侧栏清空)。索引所有写
# 路径必须串行(进程内 threading 锁足够——调用方横跨事件循环与委托 bg
# 线程)+ 原子落盘(tmp+rename),读端永远只见完整文件。
_IDX_LOCK = threading.RLock()

# 读缓存(P2):_session_meta_list 是侧栏/usage/流式等热路径的重复读点,
# 每次都 read_text+orjson.loads。按「文件 stat 签名」缓存解析结果——键是
# 索引文件绝对路径(天然区分 home/project),值是 (签名, 解析结果)。
# 签名取 (st_dev, st_ino, st_mtime_ns, st_size):
#   * _write_index 走 tmp+os.replace → inode 必变,外部原子替换同样失效;
#   * mtime_ns+size 兜住同 inode 的就地改写;
#   * 旧 inode 复用(极端:删后重建恰好同 inode)由 mtime_ns 区分。
# 出参一律走 _copy_meta_items 深一层拷贝:调用方(patch/update_all/backfill
# 及 api 层)会原地改条目或嵌套 labels 列表,绝不能污染缓存。写入路径在
# _write_index 内主动刷新缓存(持有 _IDX_LOCK,与读端同锁无竞态);解析
# 失败/文件缺失不缓存,每次照旧走磁盘,损坏自愈语义不变。
_IDX_CACHE: dict[str, tuple[tuple[int, int, int, int], list[dict]]] = {}


def _idx_signature(p) -> tuple[int, int, int, int] | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)


def _copy_meta_items(items: list[dict]) -> list[dict]:
    """条目字典浅拷贝 + 一层列表值拷贝(labels 等),防调用方改写污染缓存。"""
    return [
        {k: (list(v) if isinstance(v, list) else v) for k, v in m.items()}
        if isinstance(m, dict)
        else m
        for m in items
    ]


def _cache_evict(p) -> None:
    _IDX_CACHE.pop(str(p), None)


def _write_index(slug: str, items: list[dict]) -> None:
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    p = paths.session_index_path(slug)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
    os.replace(tmp, p)
    # 写后只清不填:若在 os.replace 与 stat 之间有外部写者替换了文件,
    # 预填会把旧内容挂到新签名上。清除后下一次读付一次 parse 即可。
    _cache_evict(p)


def _session_meta_rewrite_all(slug: str, metas: list[dict]) -> None:
    """整表替换(heal/迁移路径专用)——同样走锁,免得绕开串行约束。"""
    with _IDX_LOCK:
        _write_index(slug, metas)


def backfill_session_types() -> int:
    """一次性迁移:历史会话的 ``type`` 缺省(main 当时没有标注)补写
    ``"main"``。幂等——特殊类型行不动、已补写过的表零写入;返回补写数。
    启动时调用(server lifespan),收敛后每次启动都只是读一遍。"""
    total = 0
    for p in paths.home().glob("projects/*/sessions/_index.json"):
        slug = p.parent.parent.name
        metas = _session_meta_list(slug)
        changed = False
        for m in metas:
            if not str(m.get("type") or "").strip():
                m["type"] = "main"
                changed = True
                total += 1
        if changed:
            _session_meta_rewrite_all(slug, metas)
    return total


def _session_meta_list(slug: str) -> list[dict]:
    p = paths.session_index_path(slug)
    key = str(p)
    sig = _idx_signature(p)
    if sig is None:
        # 文件不存在(或 stat 失败):清掉可能的陈旧条目,返回空表。
        with _IDX_LOCK:
            _IDX_CACHE.pop(key, None)
        return []
    with _IDX_LOCK:
        hit = _IDX_CACHE.get(key)
    if hit is not None and hit[0] == sig:
        return _copy_meta_items(hit[1])
    try:
        items = json.loads(p.read_text() or "[]")
    except json.JSONDecodeError:
        # 损坏不缓存:每次照旧走磁盘,heal/重写落盘后签名变化自然恢复。
        with _IDX_LOCK:
            _IDX_CACHE.pop(key, None)
        return []
    # 用「读前」签名入缓存——读后文件若已被替换,签名不匹配下次自然重读,
    # 不会把旧内容挂到新签名上。
    with _IDX_LOCK:
        _IDX_CACHE[key] = (sig, _copy_meta_items(items))
    return _copy_meta_items(items)


def _session_meta_update_all(slug: str, update: Callable[[dict], dict]) -> None:
    """Apply per-session field updates to the latest index in one transaction."""
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        changed = False
        for item in items:
            patch = update(item)
            if patch:
                item.update(patch)
                changed = True
        if changed:
            _write_index(slug, items)


def _session_meta_upsert(slug: str, entry: dict) -> None:
    with _IDX_LOCK:
        items = [m for m in _session_meta_list(slug) if m.get("id") != entry["id"]]
        items.insert(0, entry)
        _write_index(slug, items)


def _session_meta_patch(slug: str, session_id: str, patch: dict, *, touch: bool = True) -> dict | None:
    """``touch=False``：不 bump ``updated``——侧栏按 updated 排序，像「已读」
    这类读取型写入不得改变会话位置。"""
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        target = None
        for m in items:
            if m.get("id") == session_id:
                m.update({k: v for k, v in patch.items() if v is not None})
                if touch:
                    m["updated"] = time.time()
                target = m
        if target is None:
            return None
        _write_index(slug, items)
        return target


def _session_meta_remove(slug: str, session_id: str) -> bool:
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        kept = [m for m in items if m.get("id") != session_id]
        if len(kept) == len(items):
            return False
        _write_index(slug, kept)
        return True


def _session_meta_children(slug: str, session_id: str) -> list[dict]:
    """Direct subagent children (``parent_session_id == session_id``) from the
    on-disk index — the source of truth for the parent links (subagent-design.md
    §4); the in-memory ``_SESSION_CHILDREN`` mirror is only a fast path."""
    return [
        m for m in _session_meta_list(slug) if m.get("parent_session_id") == session_id
    ]


def _session_meta_descendants(slug: str, session_id: str) -> list[dict]:
    """All subagent descendants, depth-first (parent/child links form a tree —
    the depth cap guarantees acyclicity)."""
    out: list[dict] = []

    def _walk(pid: str) -> None:
        for m in _session_meta_children(slug, pid):
            out.append(m)
            _walk(m["id"])

    _walk(session_id)
    return out


def subagent_depth_of(meta: dict | None) -> int:
    """Subagent depth encoded in a session meta: 0/1/2 for subagent sessions,
    -1 for a main conversation (no subagent fields)."""
    if not meta or meta.get("type") != "subagent":
        return -1
    try:
        return int(meta.get("depth"))
    except (TypeError, ValueError):
        return -1


def _find_meta(session_id: str) -> tuple[dict, str] | None:
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m, slug
    return None


def _resolve_session_meta(session_id: str) -> dict | None:
    """Find a session's meta (with project_slug/workspace) in memory or on disk."""
    s = _SESSIONS.get(session_id)
    if s:
        # 内存条目 → meta 形状投影（2026-11-21 修复模型 chip 显示）：
        # 条目带着 model 对象（ChatOpenAI dump）、graph、以及内部键名
        # model_provider/model_name/session_id——原样返回让 GET/PATCH
        # 出口的 model 变成对象、provider 缺失、id 缺失，与磁盘分支
        # （provider/model/id 字符串）形状漂移。前端 store merge 后
        # ModelChipMenu 的 label = session.model（对象）→ 活跃会话
        # （含 running subagent）点开/换绑 agent 后模型名渲染异常。
        # 这里统一投影成磁盘 meta 的键形状；调用方均为「读 meta 字段」
        # 型（无人依赖条目里的 model/graph 对象——对象一律走 _SESSIONS）。
        out = {k: v for k, v in s.items() if k not in ("model", "graph")}
        out.setdefault("id", session_id)
        out.setdefault("provider", s.get("model_provider"))
        out.setdefault("model", s.get("model_name"))
        return out
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for m in _session_meta_list(slug):
            if m.get("id") == session_id:
                return m
    return None


def resolve_session_model(
    session_id: str, session: dict | None = None
) -> tuple[str, str]:
    """The (provider_config_id, model_name) a session's usage should be
    attributed to — the ONE resolution chain every context-window consumer
    must use (seed + WS frame + usage log rows).

    In-memory session registry first (``model_provider`` / ``model_name``,
    resolved at create/rebuild time), then the on-disk session meta
    (``provider`` / ``model``) for sessions the registry no longer holds
    (sidecar restarted, session not yet touched). Before this helper the WS
    frame read the registry only, so a restarted sidecar attributed live
    calls to provider="" → the ring fell back to a 200k window while the
    GET seed resolved the real one from disk — same session, two windows,
    occupancy jumping between them (2026-11-07). Never raises; ("", "")
    only when neither source knows the session.
    """
    s = session if session is not None else _SESSIONS.get(session_id)
    provider = (s or {}).get("model_provider") or ""
    model = (s or {}).get("model_name") or ""
    if not provider or not model:
        try:
            found = _find_meta(session_id)
        except Exception:  # noqa: BLE001 — 显示辅助，解析失败即空
            found = None
        if found:
            provider = provider or found[0].get("provider") or ""
            model = model or found[0].get("model") or ""
    return provider, model



def _session_meta_set_tags(
    slug: str,
    session_id: str,
    icon: str | None,
    labels: list[str],
    *,
    touch: bool = False,
) -> dict | None:
    """Session label/icon（session-label-icon-design.md §1）——两字段全量覆盖。

    meta 存储键用 ``tag_icon``：meta 的 ``icon`` 键自创建起就是 agent 图标
    快照（usage 展示 / 会话重建等消费方），不能被用户打标覆盖；API/list 面
    统一透出为 ``label_icon``。``labels`` 直接存字符串数组（≤3、单个 ≤20
    字符的校验在 api/sessions.py 端点做）。写路径与 _session_meta_patch 同
    通道（_IDX_LOCK 串行 + 原子落盘）；``touch=False``：打标是纯视觉操作，
    不得改变侧栏排序。
    """
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        for m in items:
            if m.get("id") == session_id:
                if icon is None:
                    m.pop("tag_icon", None)  # null = 清除（键整个移除）
                else:
                    m["tag_icon"] = icon
                m["labels"] = list(labels)
                if touch:
                    m["updated"] = time.time()
                _write_index(slug, items)
                return m
        return None


def _tags_fields(meta: dict) -> dict:
    """List/get 响应统一装饰：老会话缺字段时透出 null/[]（前端免判空）。"""
    return {"label_icon": meta.get("tag_icon"), "labels": meta.get("labels") or []}


def _session_slug(session_id: str) -> str | None:
    s = _SESSIONS.get(session_id)
    if s:
        return s["project_slug"]
    found = _find_meta(session_id)
    return found[1] if found else None


def session_is_scheduled(session_id: str) -> bool:
    """该会话是否来自定时任务执行（影子会话 ``type="scheduled"``）。

    定时任务是 headless 的一次性会话：不加载 Claude Mods（插件 hooks /
    mods 总线）——它们面向交互用户，且 plugin hooks 的 grants 默认 ask，
    危险命令会在每次任务执行时弹「Mods 授权拦截」toast（2026-10-28）。
    内存表优先（运行中的会话必然在），落盘 meta 兜底；任何异常都按
    「不是定时会话」处理，绝不影响主流程。"""
    try:
        s = _SESSIONS.get(session_id)
        if s is not None:
            return s.get("type") == "scheduled"
        m = _resolve_session_meta(session_id)
        return bool(m) and m.get("type") == "scheduled"
    except Exception:  # noqa: BLE001 — 判定失败按普通会话处理
        return False


def mark_channel_meta(meta: dict, channel: str) -> bool:
    """给渠道会话补 ``type="channel"`` + ``channel`` 标记（幂等）。

    channels.ensure_channel_session（新会话落标 / 旧会话补标）与 adapter
    启动迁移调用。``meta`` 可以是内存条目（主键 session_id）也可以是磁盘
    索引条目（主键 id）——统一解析 id 后走 _session_meta_patch 串行落盘
    （磁盘索引是前端列表的事实来源），内存条目同步补键。touch=False：
    补标记不得把会话顶到侧栏顶端。返回是否实际补写（重入为 False）。"""
    sid = str(meta.get("id") or meta.get("session_id") or "")
    if not sid:
        return False
    if meta.get("type") == "channel" and meta.get("channel") == channel:
        return False
    slug = meta.get("project_slug") or _session_slug(sid) or "default"
    _session_meta_patch(slug, sid, {"type": "channel", "channel": channel}, touch=False)
    meta["type"] = "channel"
    meta["channel"] = channel
    return True

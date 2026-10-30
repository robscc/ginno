"""关联会话(linked sessions)投影与意图存储(topbar-unification.md §3)。

概念:像挂目录一样把**独立会话**挂进当前对话的视野——不是父子从属,
detach 只解除视野关联。派生为主、存储最小:

- **auto 派生**(运行时投影,不落新库):
  - spawned/delegated —— 本项目索引里 ``parent_session_id == me`` 的孩子
    (subagent/delegation 落盘时写入,同项目);
  - assigned/watching —— 动作发生时在**发起方** meta 落 ``linked_auto``
    戳(assign 不在发送方留任何记录、subscribers 在目标方,反向派生需要
    跨项目全量扫,设计明确禁止);
- **用户意图**(仅这两个字段是新增存储):
  - ``linked_user: [session_id]`` —— 手动挂载(P2);
  - ``linked_hidden: [session_id]`` —— detach 过的 auto 成员抑制(同源
    再发生不自动回来);
- **incoming**(谁在派我):目标方视角 ``subscribers``(watch 现成)+
  ``in_links`` 戳(assign 时落)。

所有写走 _IDX_LOCK 串行 + touch=False(视野变更不得 bump 侧栏排序)。
"""

from __future__ import annotations

import time

from . import paths
from .server_shared import _SESSIONS
from .session_meta import (
    _IDX_LOCK,
    _find_meta,
    _session_meta_list,
    _write_index,
)
from .worker_bus.registry import TERMINAL_PROJECTED, _mtype, project_status

# 戳/手动挂的容量上限——关联数是个位数量级,防的是异常循环写爆索引。
_LINK_CAP = 32

# rel 展示优先级:结构性关系 > 动作戳 > 手动(mounted 最后写入直接覆盖,
# 但 hidden 抑制只作用于 auto 成员)。
_PRECEDENCE = {"mounted": 0, "delegated": 1, "spawned": 2, "assigned": 3, "watching": 4}


def _resolve_slug(session_id: str) -> str | None:
    """slug 解析:内存条目缺 project_slug(异常形态)不炸——退回磁盘扫描。"""
    s_mem = _SESSIONS.get(session_id)
    slug = (s_mem or {}).get("project_slug")
    if not slug:
        found = _find_meta(session_id)
        slug = found[1] if found else None
    return slug


def stamp_link(owner_id: str, other_id: str, rel: str, *, field: str = "linked_auto") -> bool:
    """在 ``owner_id`` 的 meta ``field`` 里追加 ``{id, rel, ts}`` 戳(幂等:
    同 id 同 rel 只刷 ts;同 id 不同 rel 按 _PRECEDENCE 留一个——展示层
    一行一 rel)。assign 回执成功后调(sender=linked_auto/assigned,
    target=in_links);watch 注册成功后调(subscriber=linked_auto/watching)。
    失败(会话不存在等)静默 False——戳是视野增强,不阻塞主流程。"""
    if not owner_id or not other_id or owner_id == other_id:
        return False
    slug = _resolve_slug(owner_id)
    if not slug:
        return False
    now = int(time.time())
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        entry = next((m for m in items if m.get("id") == owner_id), None)
        if entry is None:
            return False
        cur = [e for e in (entry.get(field) or []) if isinstance(e, dict)]
        kept = [e for e in cur if e.get("id") != other_id]
        prev = next((e for e in cur if e.get("id") == other_id), None)
        if prev is not None and _PRECEDENCE.get(str(prev.get("rel")), 99) <= _PRECEDENCE.get(rel, 99):
            rel = str(prev.get("rel") or rel)  # 旧关系更强,保留
        kept.append({"id": other_id, "rel": rel, "ts": now})
        if len(kept) > _LINK_CAP:
            kept = kept[-_LINK_CAP:]
        entry[field] = kept
        _write_index(slug, items)
    # 内存条目同步(mark_channel_meta 同款):_SESSIONS 里的副本不能比磁盘旧
    s = _SESSIONS.get(owner_id)
    if s is not None:
        s[field] = [e for e in (s.get(field) or []) if not (isinstance(e, dict) and e.get("id") == other_id)] + [
            {"id": other_id, "rel": rel, "ts": now}
        ]
    return True


def _member_row(tid: str, rel: str, tmeta: dict | None) -> dict:
    """成员行(live_status 用磁盘 meta 投影;meta 缺失按 missing 仍给行——
    面板里可见可 detach,而不是整行消失)。"""
    if tmeta is None:
        return {"id": tid, "title": tid[:8], "type": "unknown", "rel": rel,
                "live_status": "missing", "updated": None, "terminal": True}
    st = project_status(tid, tmeta)
    return {
        "id": tid,
        "title": str(tmeta.get("title") or tid[:8]),
        "type": _mtype(tmeta),
        "rel": rel,
        "live_status": st,
        "updated": tmeta.get("updated"),
        "terminal": st in TERMINAL_PROJECTED,
    }


def linked_report(session_id: str) -> dict:
    """``GET /api/sessions/{id}/linked`` 的投影主体。

    members = 派生(spawned/delegated 同项目 parent 扫描 + linked_auto 戳)
    ∪ linked_user,减 linked_hidden(只抑制 auto 成员);incoming = 我的
    subscribers + in_links。排序:活着的(working/waiting)置顶,其余按
    updated 倒序。磁盘是 meta 字段的事实来源(_SESSIONS 副本可能不含新键)。"""
    found = _find_meta(session_id)
    if not found:
        return {"ok": False, "error": "unknown session"}
    meta, slug = found

    rels: dict[str, str] = {}
    # 1) spawned/delegated:同项目 parent_session_id 扫描(subagent 落盘、
    #    delegation_sessions 落盘都写这个键;跨项目孩子不存在)。
    metas = _session_meta_list(slug)
    for m in metas:
        if m.get("parent_session_id") == session_id and m.get("id"):
            rels[str(m["id"])] = "delegated" if _mtype(m) == "delegation" else "spawned"
    # 1b) 同项目订阅派生:谁的 subscribers 里有我(同一份索引,零额外 I/O)。
    #     历史 watch 没有订阅方戳(戳只在动作时落——2026-10-30 实机踩过:
    #     旧订阅的 members 空),这里兜底;新订阅已有戳,watching 优先级最
    #     低不覆盖既有 rel。跨项目历史订阅靠启动对账 backfill_linked_stamps。
    for m in metas:
        mid = str(m.get("id") or "")
        if not mid or mid == session_id:
            continue
        subs = m.get("subscribers") or []
        if any(isinstance(s, dict) and s.get("session_id") == session_id for s in subs):
            rels.setdefault(mid, "watching")
    # 2) assigned/watching:动作戳
    for e in meta.get("linked_auto") or []:
        if isinstance(e, dict) and e.get("id"):
            tid = str(e["id"])
            if tid not in rels or _PRECEDENCE.get(str(e.get("rel")), 99) < _PRECEDENCE.get(rels[tid], 99):
                rels[tid] = str(e.get("rel") or "watching")
    # 3) mounted:手动挂最后落(用户意图最强)
    for tid in meta.get("linked_user") or []:
        if tid:
            rels[str(tid)] = "mounted"
    # 4) hidden 抑制(只压 auto;用户又手动挂回来的不算)
    hidden = {str(h) for h in (meta.get("linked_hidden") or []) if h}
    rows: list[dict] = []
    for tid, rel in rels.items():
        if rel != "mounted" and tid in hidden:
            continue
        if tid == session_id:
            continue
        tfound = _find_meta(tid)
        rows.append(_member_row(tid, rel, tfound[0] if tfound else None))
    rows.sort(key=lambda r: (
        0 if r["live_status"] in ("working", "waiting") else 1,
        -(r.get("updated") or 0),
    ))

    # incoming:谁在看我/派我(去重;方向由字段决定)
    incoming: dict[str, str] = {}
    for s in meta.get("subscribers") or []:
        if isinstance(s, dict) and s.get("session_id"):
            incoming[str(s["session_id"])] = "watching_me"
    for e in meta.get("in_links") or []:
        if isinstance(e, dict) and e.get("id"):
            incoming.setdefault(str(e["id"]), "assigned_me")
    inc_rows: list[dict] = []
    for tid, rel in incoming.items():
        if tid == session_id:
            continue
        tfound = _find_meta(tid)
        inc_rows.append(_member_row(tid, rel, tfound[0] if tfound else None))
    inc_rows.sort(key=lambda r: -(r.get("updated") or 0))

    live = sum(1 for r in rows if r["live_status"] in ("working", "waiting"))
    return {
        "ok": True,
        "members": rows,
        "incoming": inc_rows,
        "live": live,
    }


def mount_linked(session_id: str, target: str) -> dict:
    """手动挂载:target 进 ``linked_user``、出 ``linked_hidden``。"""
    if not target or target == session_id:
        return {"ok": False, "error": "invalid target"}
    if not _find_meta(target):
        return {"ok": False, "error": "unknown target"}
    return _mutate_lists(session_id, add_user=target)


def detach_linked(session_id: str, target: str) -> dict:
    """解除关联:user 成员移出 ``linked_user``;auto 成员落 ``linked_hidden``
    (同源再发生不自动回来)。一律无 confirm——无破坏性,会话本体不动。"""
    if not target or target == session_id:
        return {"ok": False, "error": "invalid target"}
    return _mutate_lists(session_id, hide_auto=target)


def _mutate_lists(session_id: str, *, add_user: str | None = None, hide_auto: str | None = None) -> dict:
    """linked_user/linked_hidden 的原子读-改-写(锁内,touch 语义 = 不 bump
    updated——视野变更不改变侧栏排序)。内存副本同步。"""
    slug = _resolve_slug(session_id)
    if not slug:
        return {"ok": False, "error": "unknown session"}
    with _IDX_LOCK:
        items = _session_meta_list(slug)
        entry = next((m for m in items if m.get("id") == session_id), None)
        if entry is None:
            return {"ok": False, "error": "unknown session"}
        users = [str(u) for u in (entry.get("linked_user") or []) if u]
        hidden = [str(h) for h in (entry.get("linked_hidden") or []) if h]
        if add_user:
            if add_user not in users:
                users.append(add_user)
            if len(users) > _LINK_CAP:
                users = users[-_LINK_CAP:]
            hidden = [h for h in hidden if h != add_user]
        if hide_auto:
            if hide_auto in users:
                # user 成员 detach = 只移出 linked_user(它本就不是 auto 派生,
                # 不落 hidden——否则未来真实 auto 关联会被误抑制)
                users = [u for u in users if u != hide_auto]
            elif hide_auto not in hidden:
                hidden.append(hide_auto)
                if len(hidden) > _LINK_CAP:
                    hidden = hidden[-_LINK_CAP:]
        entry["linked_user"] = users
        entry["linked_hidden"] = hidden
        _write_index(slug, items)
    s = _SESSIONS.get(session_id)
    if s is not None:
        s["linked_user"] = users
        s["linked_hidden"] = hidden
    return {"ok": True}


def backfill_linked_stamps() -> int:
    """启动对账:历史 watch 订阅只在目标方留 subscribers(戳是后来才加的,
    只在新动作落),订阅方 ``linked_auto`` 缺戳 → 投影 members 漏行。全局扫
    一遍索引把缺的补上(跨项目订阅也覆盖——linked_report 的 1b 兜底只看
    本项目)。幂等:只写缺戳的,收敛后每次启动零写入。返回补写数。"""
    all_metas: list[dict] = []
    by_id: dict[str, dict] = {}
    for p in paths.home().glob("projects/*/sessions/_index.json"):
        for m in _session_meta_list(p.parent.parent.name):
            all_metas.append(m)
            if m.get("id"):
                by_id[str(m["id"])] = m
    added = 0
    for m in all_metas:
        tid = str(m.get("id") or "")
        if not tid:
            continue
        for s in m.get("subscribers") or []:
            if not (isinstance(s, dict) and s.get("session_id")):
                continue
            sub_id = str(s["session_id"])
            if sub_id == tid or sub_id not in by_id:
                continue
            cur = [e for e in (by_id[sub_id].get("linked_auto") or []) if isinstance(e, dict)]
            if any(str(e.get("id")) == tid for e in cur):
                continue  # 已有戳(任意 rel)——不重复写
            if stamp_link(sub_id, tid, "watching"):
                added += 1
    return added

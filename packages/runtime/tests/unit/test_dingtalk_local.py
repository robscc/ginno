"""钉钉连接器本地逻辑单测：sessions 映射表（F2）+ menus 菜单（F8）。

Semantics under test:
- SessionMapper: bind/get/unbind 往返、持久化跨实例、upsert、损坏容错（备份后重建）；
- menus: load 坏项跳过、match（前缀/全等/大小写/未命中）、render 变量替换、
  help/is_builtin。
"""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.channels.dingtalk.menus import (
    MenuItem,
    help_markdown,
    is_builtin,
    load_menus,
    match,
    render,
)
from ginno_runtime.channels.dingtalk.sessions import SessionMapper

pytestmark = pytest.mark.unit


# ---- SessionMapper（F2）---------------------------------------------------


def _mapper(tmp_path):
    return SessionMapper(tmp_path / "dingtalk_sessions.json")


def test_mapper_bind_get_roundtrip(tmp_path):
    """bind 后 get 命中返回 ginno session；未绑定组合返回 None。"""
    m = _mapper(tmp_path)
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g1", nick="张三")

    assert m.get(staff_id="s1", conversation_id="cid1") == "g1"
    # 键是 (staff_id, conversation_id) 组合：任一不符都不命中。
    assert m.get(staff_id="s2", conversation_id="cid1") is None
    assert m.get(staff_id="s1", conversation_id="cid2") is None


def test_mapper_persistence_roundtrip(tmp_path):
    """落盘 JSON 结构符合设计，且新实例 load 后映射可见（跨进程语义）。"""
    m = _mapper(tmp_path)
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g1", nick="张三")

    store = tmp_path / "dingtalk_sessions.json"
    data = json.loads(store.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"mappings"}
    entry = data["mappings"][0]
    assert entry["staff_id"] == "s1"
    assert entry["conversation_id"] == "cid1"
    assert entry["ginno_session"] == "g1"
    assert entry["nick"] == "张三"
    assert entry["bound_at"]
    assert entry["last_active"]

    # 新实例（模拟渠道重启）加载同一文件后能查到。
    m2 = SessionMapper(store)
    m2.load()
    assert m2.get(staff_id="s1", conversation_id="cid1") == "g1"


def test_mapper_upsert_updates_session_and_timestamps(tmp_path):
    """同键重复 bind 是 upsert：换 session、刷 last_active，不产生第二条。"""
    m = _mapper(tmp_path)
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g1")
    first = m.all()[0]
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g2", nick="张三")

    entries = m.all()
    assert len(entries) == 1
    assert m.get(staff_id="s1", conversation_id="cid1") == "g2"
    assert entries[0]["nick"] == "张三"
    # bound_at 首绑定格；last_active 每次 bind 刷新（秒级时间戳，>= 首绑）。
    assert entries[0]["bound_at"] == first["bound_at"]
    assert entries[0]["last_active"] >= first["last_active"]


def test_mapper_unbind_by_ginno_session(tmp_path):
    """unbind 按 ginno session 删除映射；其它 session 的映射不受影响。"""
    m = _mapper(tmp_path)
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g1")
    m.bind(staff_id="s2", conversation_id="cid2", ginno_session="g2")

    m.unbind("g1")

    assert m.get(staff_id="s1", conversation_id="cid1") is None
    assert m.get(staff_id="s2", conversation_id="cid2") == "g2"
    # 删除也落盘：重启后仍不存在。
    m2 = SessionMapper(tmp_path / "dingtalk_sessions.json")
    m2.load()
    assert m2.get(staff_id="s1", conversation_id="cid1") is None


def test_mapper_all_returns_copies(tmp_path):
    """all() 返回拷贝：外部改动不回写内部状态。"""
    m = _mapper(tmp_path)
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g1")
    m.all()[0]["ginno_session"] = "hacked"
    assert m.get(staff_id="s1", conversation_id="cid1") == "g1"


def test_mapper_corrupt_store_recovered(tmp_path):
    """损坏 store：load 不抛异常，坏文件备份留档，内存重建为空表。"""
    store = tmp_path / "dingtalk_sessions.json"
    store.write_text("{not valid json", encoding="utf-8")

    m = SessionMapper(store)
    m.load()  # 不抛异常即通过

    assert m.all() == []
    assert m.get(staff_id="s1", conversation_id="cid1") is None
    assert (tmp_path / "dingtalk_sessions.json.corrupt.bak").read_text(
        encoding="utf-8"
    ) == "{not valid json"
    # 重建后可继续正常 bind。
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g1")
    assert json.loads(store.read_text(encoding="utf-8"))["mappings"][0]["ginno_session"] == "g1"


def test_mapper_bad_structure_recovered(tmp_path):
    """结构损坏（mappings 非列表）同样走备份重建路径。"""
    store = tmp_path / "dingtalk_sessions.json"
    store.write_text(json.dumps({"mappings": "nope"}), encoding="utf-8")

    m = SessionMapper(store)
    m.load()

    assert m.all() == []
    assert (tmp_path / "dingtalk_sessions.json.corrupt.bak").exists()


def test_mapper_missing_store_is_empty(tmp_path):
    """store 不存在：load 后空表，不产生任何文件。"""
    m = _mapper(tmp_path)
    m.load()
    assert m.all() == []
    assert not (tmp_path / "dingtalk_sessions.json").exists()


def test_mapper_no_tmp_file_left_behind(tmp_path):
    """原子写不残留 .tmp 中间文件（半截文件不会成为下一次 load 的输入）。"""
    m = _mapper(tmp_path)
    m.bind(staff_id="s1", conversation_id="cid1", ginno_session="g1")
    assert not (tmp_path / "dingtalk_sessions.json.tmp").exists()


# ---- menus（F8）-----------------------------------------------------------


def test_load_menus_filters_bad_entries():
    """坏项（非 dict / 缺字段 / 空串 / 非 menus 键）跳过，合法项保留默认值。"""
    config = {
        "menus": [
            {"trigger": "日报", "title": "每日总结", "prompt": "总结 {date} 的工作"},
            "not-a-dict",
            {"trigger": "周报", "title": "周总结", "prompt": "写周报", "new_session": True},
            {"trigger": "", "title": "空触发词", "prompt": "x"},
            {"trigger": "缺prompt", "title": "x"},
            {"trigger": "缺title", "prompt": "x"},
            {"trigger": 123, "title": "x", "prompt": "x"},
        ]
    }
    items = load_menus(config)
    assert [i.trigger for i in items] == ["日报", "周报"]
    assert items[0].new_session is False  # 缺省 False
    assert items[1].new_session is True


def test_load_menus_tolerates_missing_or_bad_key():
    assert load_menus({}) == []
    assert load_menus({"menus": "oops"}) == []


def test_match_exact_prefix_case_and_miss():
    """全等、/trigger 前缀、大小写不敏感、空白容忍、未命中返回 None。"""
    menus = [MenuItem(trigger="Report", title="日报", prompt="p")]
    assert match(menus, "report") is menus[0]        # 全等 + 大小写不敏感
    assert match(menus, "  REPORT  ") is menus[0]    # 去首尾空白
    assert match(menus, "/report") is menus[0]       # /trigger 前缀
    assert match(menus, "/Report") is menus[0]       # 前缀同样不区分大小写
    assert match(menus, "report extra") is None      # 无前缀时不误吞后缀
    assert match(menus, "/report 今天") is menus[0]  # 前缀命中允许尾随内容
    assert match(menus, "其他") is None
    assert match(menus, "") is None
    assert match(menus, "   ") is None


def test_match_first_hit_wins():
    """遍历顺序即配置顺序，首个命中生效。"""
    a = MenuItem(trigger="x", title="A", prompt="a")
    b = MenuItem(trigger="x", title="B", prompt="b")
    assert match([a, b], "/x") is a


def test_render_replaces_variables():
    item = MenuItem(
        trigger="日报", title="日报", prompt="给 {staff_name} 写 {date} {time} 的日报"
    )
    out = render(item, staff_name="张三", date="2026-10-21", time="09:30")
    assert out == "给 张三 写 2026-10-21 09:30 的日报"


def test_render_defaults_to_now_and_keeps_unknown_vars():
    """date/time 缺省取当前本地时间；未知 {var} 原样保留不抛错。"""
    item = MenuItem(trigger="t", title="T", prompt="{date} {time} {staff_name} {unknown}")
    out = render(item)
    assert "{unknown}" in out          # 未知变量原样保留
    assert "{date}" not in out and "{time}" not in out and "{staff_name}" not in out
    # 缺省 date/time 形如 2026-10-21 / 09:30。
    date_part, time_part, _, _ = out.split(" ", 3)
    assert len(date_part) == 10 and date_part[4] == "-" and date_part[7] == "-"
    assert len(time_part) == 5 and time_part[2] == ":"


def test_help_markdown_lists_triggers_and_titles():
    menus = [
        MenuItem(trigger="日报", title="每日总结", prompt="p"),
        MenuItem(trigger="周报", title="周报生成", prompt="p"),
    ]
    md = help_markdown(menus)
    assert "日报" in md and "每日总结" in md
    assert "周报" in md and "周报生成" in md
    assert md.startswith("**可用菜单**")
    # 每个菜单一行 markdown 列表项。
    bullet_lines = [ln for ln in md.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) == 2


def test_help_markdown_empty():
    assert "直接" in help_markdown([])


def test_is_builtin():
    assert is_builtin("/help") is True
    assert is_builtin("菜单") is True
    assert is_builtin("  /HELP  ") is True   # 空白 + 大小写
    assert is_builtin("/help me") is False
    assert is_builtin("菜单1") is False
    assert is_builtin("") is False

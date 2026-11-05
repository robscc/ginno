"""Issue #13/#14/#15 — checkpoint 并发与恢复契约验证。

- #13 同 ID 消息改写（graph.py model_copy 编辑工具调用）必须在重启重建后保真；
  delta 检测对同 ID 改写降级为 full snapshot。
- #15 多 FileCheckpointer 实例并发写同一 session、torn tail、full/delta 链
  跨实例（重启）恢复。
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from ginno_runtime import jsonx as json
from ginno_runtime.checkpointer import FileCheckpointer, load_entries

pytestmark = pytest.mark.unit


def _cfg(thread_id="s1", checkpoint_id=None):
    c = {"configurable": {"thread_id": thread_id}}
    if checkpoint_id:
        c["configurable"]["checkpoint_id"] = checkpoint_id
    return c


def _delta_mode(isolated_home):
    (isolated_home / "settings.json").write_text(
        json.dumps({"context": {"checkpoint_mode": "delta"}}))


def _cp_with_msgs(parent_config, msgs):
    cp = empty_checkpoint()
    cp["channel_values"] = {"messages": list(msgs), "workspace": "/tmp/ws"}
    if parent_config:
        cp["parent_config"] = parent_config
    return cp


def _modes(isolated_home, thread="s1"):
    raw = load_entries(isolated_home / "projects" / "p" / "sessions" / f"{thread}.jsonl")
    return [c.get("mode") for c in raw]


def _ai(tool_args: dict, tc_id: str = "t1") -> AIMessage:
    return AIMessage(
        content="",
        id="a1",
        tool_calls=[{"name": "bash", "args": tool_args, "id": tc_id, "type": "tool_call"}],
    )


# --------------------------------------------------------------------------- #
# Issue #13 — same-ID rewrite must survive restart/reconstruction
# --------------------------------------------------------------------------- #
def test_same_id_tool_args_rewrite_survives_reopen(isolated_home):
    """graph.py tools 节点用 model_copy 保留 message id 改写 tool_calls args。
    delta 检测必须识别同 ID 改写并降级 full，否则重启后恢复的是旧参数。"""
    _delta_mode(isolated_home)
    cp = FileCheckpointer(project_slug="p")

    ai = _ai({"cmd": "ls"})
    r1 = cp.put(_cfg(), _cp_with_msgs(None, [ai]),
                {"source": "loop", "step": 0, "writes": {}}, {})

    # mod 改写参数：同 id、同 message，仅 args 变化；追加 ToolMessage
    ai_edited = _ai({"cmd": "ls -la"})
    tool = ToolMessage(content="out", tool_call_id="t1", name="bash")
    cp.put(_cfg(checkpoint_id=r1["configurable"]["checkpoint_id"]),
           _cp_with_msgs(r1, [ai_edited, tool]),
           {"source": "loop", "step": 1, "writes": {}}, {})

    # 模拟重启：全新实例从盘上重建
    fresh = FileCheckpointer(project_slug="p")
    tup = fresh.get_tuple(_cfg())
    msgs = tup.checkpoint["channel_values"]["messages"]
    assert len(msgs) == 2
    assert msgs[0].id == "a1"
    # 关键断言：改写后的参数在重启重建后仍然在
    assert msgs[0].tool_calls[0]["args"] == {"cmd": "ls -la"}
    assert isinstance(msgs[1], ToolMessage)


def test_same_id_result_takeover_survives_reopen(isolated_home):
    """result 接管场景：AI message 的 tool_calls 被剔除（同 id）+ ToolMessage
    直接生成。重启后不得回弹出原始 tool_calls。"""
    _delta_mode(isolated_home)
    cp = FileCheckpointer(project_slug="p")

    ai = _ai({"cmd": "rm -rf /"})
    r1 = cp.put(_cfg(), _cp_with_msgs(None, [ai]),
                {"source": "loop", "step": 0, "writes": {}}, {})

    ai_taken = AIMessage(content="", id="a1", tool_calls=[])
    tool = ToolMessage(content="denied", tool_call_id="t1", name="bash", status="error")
    cp.put(_cfg(checkpoint_id=r1["configurable"]["checkpoint_id"]),
           _cp_with_msgs(r1, [ai_taken, tool]),
           {"source": "loop", "step": 1, "writes": {}}, {})

    fresh = FileCheckpointer(project_slug="p")
    msgs = fresh.get_tuple(_cfg()).checkpoint["channel_values"]["messages"]
    assert msgs[0].tool_calls == []
    assert msgs[1].content == "denied"


def test_same_id_rewrite_degrades_to_full_entry(isolated_home):
    """同 ID 改写落入 full snapshot（不进 delta 链），链上其余仍是 delta。"""
    _delta_mode(isolated_home)
    cp = FileCheckpointer(project_slug="p")
    m1 = HumanMessage(content="one", id="m1")
    ai = _ai({"cmd": "ls"})
    r1 = cp.put(_cfg(), _cp_with_msgs(None, [m1, ai]),
                {"source": "loop", "step": 0, "writes": {}}, {})
    r2 = cp.put(
        _cfg(checkpoint_id=r1["configurable"]["checkpoint_id"]),
        _cp_with_msgs(r1, [m1, ai, HumanMessage(content="two", id="m2")]),
        {"source": "loop", "step": 1, "writes": {}}, {})
    assert _modes(isolated_home) == ["full", "delta"]

    ai_edited = _ai({"cmd": "ls -la"})
    cp.put(_cfg(checkpoint_id=r2["configurable"]["checkpoint_id"]),
           _cp_with_msgs(r2, [m1, ai_edited, HumanMessage(content="two", id="m2")]),
           {"source": "loop", "step": 2, "writes": {}}, {})
    assert _modes(isolated_home) == ["full", "delta", "full"]


def test_identical_prefix_still_delta(isolated_home):
    """回归护栏：内容真正逐字节一致的共享前缀不得误判为改写（delta 仍生效）。"""
    _delta_mode(isolated_home)
    cp = FileCheckpointer(project_slug="p")
    m1 = HumanMessage(content="keep", id="m1")
    r1 = cp.put(_cfg(), _cp_with_msgs(None, [m1]),
                {"source": "loop", "step": 0, "writes": {}}, {})
    # 重新构造内容相同但对象不同的同 id 消息（langgraph 每步都会重建对象）
    m1b = HumanMessage(content="keep", id="m1")
    cp.put(_cfg(checkpoint_id=r1["configurable"]["checkpoint_id"]),
           _cp_with_msgs(r1, [m1b, HumanMessage(content="more", id="m2")]),
           {"source": "loop", "step": 1, "writes": {}}, {})
    assert _modes(isolated_home) == ["full", "delta"]


# --------------------------------------------------------------------------- #
# Issue #15 — multi-instance / durability
# --------------------------------------------------------------------------- #
def test_two_instances_interleaved_writes_all_visible(isolated_home):
    a = FileCheckpointer(project_slug="p")
    b = FileCheckpointer(project_slug="p")
    cps = [empty_checkpoint() for _ in range(6)]
    for i, c in enumerate(cps):
        (a if i % 2 == 0 else b).put(
            _cfg(), c, {"source": "loop", "step": i, "writes": {}}, {})
    fresh = FileCheckpointer(project_slug="p")
    seen = {fresh.get_tuple(_cfg(checkpoint_id=c["id"])).checkpoint["id"] for c in cps}
    assert seen == {c["id"] for c in cps}


def test_threaded_multi_instance_appends_no_loss(isolated_home):
    """同 session、两个实例、多线程并发 put：逐条可读、条数不少、行不撕裂。
    大 payload（>8KB）迫使 TextIOWrapper 多次 syscall，暴露不加跨实例锁时的
    交错窗口。"""
    a = FileCheckpointer(project_slug="p")
    b = FileCheckpointer(project_slug="p")
    per_thread, n_threads = 15, 4
    written: list[str] = []
    lock = threading.Lock()

    def worker(inst, tag):
        for i in range(per_thread):
            big = HumanMessage(content=f"{tag}-{i}-" + "x" * 9000, id=f"{tag}-{i}")
            c = empty_checkpoint()
            c["channel_values"] = {"messages": [big]}
            inst.put(_cfg(), c, {"source": "loop", "step": i, "writes": {}}, {})
            with lock:
                written.append((c["id"], big.id))

    threads = [threading.Thread(target=worker, args=(a, f"a{t}")) for t in range(2)] + \
              [threading.Thread(target=worker, args=(b, f"b{t}")) for t in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    path = isolated_home / "projects" / "p" / "sessions" / "s1.jsonl"
    entries = load_entries(path)  # 解析失败/内部撕裂会在此或行数断言暴露
    assert len(entries) == per_thread * n_threads
    fresh = FileCheckpointer(project_slug="p")
    seen = set()
    for cid, mid in written:
        tup = fresh.get_tuple(_cfg(checkpoint_id=cid))
        assert tup is not None, f"lost checkpoint {cid}"
        assert tup.checkpoint["channel_values"]["messages"][0].id == mid
        seen.add(cid)
    assert len(seen) == per_thread * n_threads


def test_torn_tail_repaired_by_fresh_instance_then_writable(isolated_home):
    cp = FileCheckpointer(project_slug="p")
    c1 = empty_checkpoint()
    cp.put(_cfg(), c1, {"source": "input", "step": 0, "writes": {}}, {})
    path = isolated_home / "projects" / "p" / "sessions" / "s1.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"op":"cp","checkpoint_id":"torn","mode":"full"')  # 半行，无 \n

    fresh = FileCheckpointer(project_slug="p")
    c2 = empty_checkpoint()
    fresh.put(_cfg(), c2, {"source": "loop", "step": 1, "writes": {}}, {})
    # torn 条目被裁掉（未提交事件只丢它自己），日志保持可解析
    entries = load_entries(path)
    assert [e["checkpoint_id"] for e in entries] == [c1["id"], c2["id"]]
    assert fresh.get_tuple(_cfg()).checkpoint["id"] == c2["id"]


def test_full_delta_chain_survives_reopen(isolated_home):
    _delta_mode(isolated_home)
    cp = FileCheckpointer(project_slug="p")
    msgs = []
    r = None
    for i in range(5):
        msgs.append(HumanMessage(content=f"m{i}", id=f"m{i}"))
        cfg = _cfg(checkpoint_id=r["configurable"]["checkpoint_id"]) if r else _cfg()
        r = cp.put(cfg, _cp_with_msgs(r, list(msgs)),
                   {"source": "loop", "step": i, "writes": {}}, {})
    assert _modes(isolated_home) == ["full"] + ["delta"] * 4

    fresh = FileCheckpointer(project_slug="p")
    tup = fresh.get_tuple(_cfg())
    got = tup.checkpoint["channel_values"]["messages"]
    assert [m.id for m in got] == [f"m{i}" for i in range(5)]
    mid = fresh.get_tuple(_cfg(checkpoint_id=r["configurable"]["checkpoint_id"]))
    assert [m.id for m in mid.checkpoint["channel_values"]["messages"]] == \
        [f"m{i}" for i in range(5)]


def test_async_put_and_sync_put_interleaved_multi_instance(isolated_home):
    """aput（线程池执行）与另一个实例的同步 put 交错，同样不丢条目。"""
    a = FileCheckpointer(project_slug="p")
    b = FileCheckpointer(project_slug="p")

    async def drive():
        ids = []
        for i in range(10):
            c = empty_checkpoint()
            if i % 2 == 0:
                await a.aput(_cfg(), c, {"source": "loop", "step": i, "writes": {}}, {})
            else:
                b.put(_cfg(), c, {"source": "loop", "step": i, "writes": {}}, {})
            ids.append(c["id"])
        return ids

    ids = asyncio.run(drive())
    fresh = FileCheckpointer(project_slug="p")
    for cid in ids:
        assert fresh.get_tuple(_cfg(checkpoint_id=cid)) is not None

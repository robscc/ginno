"""Engine 事件序列 golden test(重构批次 0 契约冻结)。

被测对象:``api/stream/engine.py`` 的 ``_stream_graph()``(经 ``_run_stream``
直驱,``ws=None``,无 socket——帧由可选事件汇 ``event_sink`` 逐帧捕获)。

用途:engine.py(约 1769 行)后续阶段化拆分为 ``stages/`` 时的**验收基准**。
拆分属于纯结构移动,本测试保证每帧「事件类型 + 关键字段 + 帧序」不变;
diff 快照即可看到任何行为漂移。

三个场景:
1. ``plain_text``     —— 纯文本单轮(无工具)。
2. ``tool_call_bash`` —— 带一次 bash 工具调用(``echo hello-golden``)+ 第二步收尾文本。
3. ``multi_chunk``    —— LLM 逐片流式(3 片),片边界 1:1 映射 token.delta 帧
   (``stream.coalesce_ms=0`` 关闭 33ms 合帧,chunk→帧确定性)。

捕获口径
--------
帧捕获点 = engine ``safe_send``(即 ``emit()`` 出口)→ ``event_sink.offer``。
不经过 sink 的通道不计入:``_push_session_event``(turn.py 前置的
context.compacting / 远端拒绝等)与 keepalive(15s 保活帧,秒级 turn 不会触发,
防御性过滤)。golden 里每行 ``EVENT <json>`` 的 json 已做确定性归一:

- 帧级信封键 ``turn_id`` / ``frame_session`` 直接剥离(每帧恒在、值即本轮标识);
- uuid / call-id / fake-msg-id 形态的值 → ``<id#n>`` 占位(**同值同占位**,
  保留 tool.start/args/end 之间的 id 配对关系);
- 时间/用量数值键(ts、elapsed_s、*_tokens)→ ``<n>`` 占位。

重新生成快照(行为有意变更后):

    cd packages/runtime && GOLDEN_UPDATE=1 \\
      uv run --group test --extra docs pytest tests/api/test_engine_golden.py -q

快照文件:``tests/goldens/engine_turn.txt``。
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from ginno_runtime import jsonx as json
from ginno_runtime import server, server_shared
from ginno_runtime.api.stream.event_sink import register_sink, unregister_sink
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "goldens" / "engine_turn.txt"

_UPDATE = os.environ.get("GOLDEN_UPDATE") == "1"

# 每帧恒在的帧级信封键(engine.emit → _ev 注入),不是 payload 契约。
_ENVELOPE_KEYS = {"turn_id", "frame_session"}
# 时间/用量数值键:值随机器/时钟漂移,占位化。
_NUMERIC_KEYS = {
    "ts", "elapsed_s", "duration_ms", "predicted_s", "at", "pct",
    "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_creation_tokens", "reasoning_tokens", "total_tokens",
}
# 非确定性标识值形态:uuid、32hex、fake_/call_ 前缀 id。
_ID_VALUE_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{32}|fake_[0-9a-f]{6,}|call_[0-9a-f]{6,}|[0-9a-f]{24,})$"
)


# --------------------------------------------------------------------------- #
# 捕获 sink:引擎 safe_send 对每个 emit() 出口帧调用 offer(见 event_sink.py)
# --------------------------------------------------------------------------- #
class _FrameSink:
    """逐帧直录版事件汇。不用 TurnEventSink——它对 delta 有 200ms/4096 字符
    节流,golden 要的是逐帧原始序列。"""

    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def offer(self, event: str, data: dict) -> None:
        if not event or event == "keepalive":
            return
        # 快照防御:deep copy,引擎侧后续原地改 payload 不得影响已录帧。
        self.frames.append(
            (event, json.loads(json.dumps(data, ensure_ascii=False, default=str)))
        )


# --------------------------------------------------------------------------- #
# 多片流式 fake:ScriptedChatModel 的纯文本只 yield 一个 chunk,流式场景
# 需要片边界确定性 —— 每片一个 AIMessageChunk,经 engine 逐片成帧。
# --------------------------------------------------------------------------- #
class MultiChunkScripted(ScriptedChatModel):
    """把 ``pieces`` 逐片 yield 的流式变体(复用 bind_tools/_next 基建)。"""

    pieces: list[str] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        # updates 模式的完整 AIMessage(与父类脚本路径解耦:本类纯文本)。
        msg = AIMessage(content="".join(self.pieces), id="fake_golden_stream")
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        for piece in self.pieces:
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=piece, id="fake_golden_stream")
            )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for chunk in self._stream(messages):
            yield chunk


# --------------------------------------------------------------------------- #
# 归一化 + 快照存取
# --------------------------------------------------------------------------- #
def _normalize(obj, ids: dict[str, str]):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _ENVELOPE_KEYS:
                continue
            if k in _NUMERIC_KEYS:
                out[k] = "<n>"
            else:
                out[k] = _normalize(v, ids)
        return out
    if isinstance(obj, list):
        return [_normalize(v, ids) for v in obj]
    if isinstance(obj, str) and _ID_VALUE_RE.match(obj):
        # 同值同占位:tool.start/args/end 的 id 配对关系保留在快照里。
        return ids.setdefault(obj, f"<id{len(ids) + 1}>")
    return obj


def _golden_lines(frames: list[tuple[str, dict]]) -> list[str]:
    ids: dict[str, str] = {}
    lines = []
    for event, data in frames:
        payload = json.dumps(
            _normalize(data, ids), ensure_ascii=False, sort_keys=True,
        )
        lines.append(f"{event} {payload}")
    return lines


_SECTION_RE = re.compile(r"(?ms)^=== scenario: (\S+) ===\n(.*?)(?=^=== scenario: |\Z)")


def _load_golden_sections() -> dict[str, list[str]]:
    if not GOLDEN_PATH.exists():
        return {}
    text = GOLDEN_PATH.read_text(encoding="utf-8")
    return {
        m.group(1): [ln for ln in m.group(2).splitlines() if ln.strip()]
        for m in _SECTION_RE.finditer(text)
    }


def _save_golden_section(name: str, lines: list[str]) -> None:
    """把单个场景段写回快照文件(GOLDEN_UPDATE=1 时调用)。"""
    header = (
        "# Ginno engine golden — _stream_graph 外发事件帧序列契约快照(批次 0)\n"
        "#\n"
        "# 生成命令:\n"
        "#   cd packages/runtime && GOLDEN_UPDATE=1 \\\n"
        "#     uv run --group test --extra docs pytest tests/api/test_engine_golden.py -q\n"
        "#\n"
        "# 归一规范(详见 tests/api/test_engine_golden.py 模块 docstring):\n"
        "#   - 每行一条帧:EVENT <json>(键序 sort_keys);\n"
        "#   - <id#n> = 非确定性标识占位(uuid/call id/fake msg id),同值同占位;\n"
        "#   - <n>    = 时间/用量数值占位(ts/elapsed_s/*_tokens);\n"
        "#   - 帧信封键 turn_id / frame_session 剥离;keepalive 不入快照。\n"
        "# 任何 diff = turn 事件契约漂移 —— 结构性拆分必须保持本文件逐字节不变。\n"
    )
    sections = _load_golden_sections()
    sections[name] = lines
    parts = [header]
    for key in sorted(sections):
        parts.append(f"=== scenario: {key} ===\n" + "\n".join(sections[key]) + "\n")
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text("\n".join(parts), encoding="utf-8")
    # 写后立即回读校验:save/load 必须闭环(空行/换行差异不留到比对期)。
    assert _load_golden_sections()[name] == lines, "golden 段落写读不一致"


def _assert_scenario(name: str, lines: list[str]) -> None:
    if _UPDATE:
        _save_golden_section(name, lines)
        return
    stored = _load_golden_sections().get(name)
    assert stored is not None, (
        f"快照缺场景段 {name!r}:跑 GOLDEN_UPDATE=1 生成 "
        f"(见 {GOLDEN_PATH} 文件头命令)"
    )
    if stored != lines:
        detail = [f"scenario {name} 与快照不一致(重构必须逐帧对齐):"]
        for i in range(max(len(stored), len(lines))):
            exp = stored[i] if i < len(stored) else "<缺行>"
            act = lines[i] if i < len(lines) else "<多出行>"
            mark = " " if exp == act else "!"
            detail.append(f"{mark} 快照[{i}]: {exp}")
            if exp != act:
                detail.append(f"{mark} 实际[{i}]: {act}")
        raise AssertionError("\n".join(detail))


# --------------------------------------------------------------------------- #
# 驱动:REST 建会话(真实路径建 graph/注册表)→ asyncio.run 直驱 _run_stream
# --------------------------------------------------------------------------- #
def _allow_all_and_no_coalesce(monkeypatch) -> None:
    """确定性前置:
    1. settings bypass_permissions=true —— bash 直通权限(测试内工具轮);
    2. 合帧窗口 patch 成 0 —— 每 LLM chunk 一帧 token.delta(chunk→帧 1:1,
       chunk 边界由脚本定死,帧序因此确定)。

    ⚠️ 走 monkeypatch 而非 settings["stream"]["coalesce_ms"]:产品 bug ——
    delta_coalesce.coalesce_window_s() 的 ``from ..paths import settings_path``
    在 api/stream 包层级解析到不存在的 ginno_runtime.api.paths → ImportError
    被静默吞掉 → 该设置从未生效,窗口恒为默认 33ms(2026-11-20 实测证据:
    settings 写 coalesce_ms=0 后 coalesce_window_s() 仍返回 0.033)。
    monkeypatch 的是 test_delta_coalesce.py 注明的官方测试缝。
    """
    from ginno_runtime import paths
    from ginno_runtime.api.stream import delta_coalesce

    monkeypatch.setattr(delta_coalesce, "coalesce_window_s", lambda: 0.0)
    p = paths.settings_path()
    data = json.loads(p.read_text()) if p.exists() else {}
    data["bypass_permissions"] = True
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))


def _drive_turn(session_id: str, text: str) -> list[tuple[str, dict]]:
    """直驱一轮 turn,返回捕获的外发事件帧。ws=None(帧只进 sink)。"""
    from ginno_runtime.api.stream.turn import _run_stream

    session = server._SESSIONS[session_id]
    turn_id = uuid.uuid4().hex
    cfg = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": session.get("project_slug") or "default",
            "agent_id": session.get("agent_id"),
            "turn_id": turn_id,
            "user_text": text,
            "usage_source": "chat",
        }
    }
    sink = _FrameSink()
    register_sink(session_id, sink)
    try:
        asyncio.run(
            _run_stream(
                None, session["graph"], cfg, text, session,
                session.get("agent_id"), origin="deliver",
            )
        )
    finally:
        unregister_sink(session_id, sink)
        # fire-and-forget 兜底:引擎 finally 已 pop;防异常路径残留。
        server_shared._RUNNING_TURNS.pop(session_id, None)
        server_shared._TURN_STOP.pop(session_id, None)
    return sink.frames


# --------------------------------------------------------------------------- #
# 三个 golden 场景（批次 5-2 起双路参数化：legacy = engine._stream_graph 原函数；
# staged = stages/ 四阶段实现，GINNO_STREAM_ENGINE=staged 分流）。
# 两路断言**同一快照段** —— staged 与 legacy 的外发帧序逐字节一致是本套
# 快照对复制式迁移的硬验收。
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("engine", ["legacy", "staged"])
def test_golden_plain_text_turn(client, create_session, monkeypatch, engine):
    """场景 1:纯文本单轮 —— turn.start → token.delta → message.end。"""
    if engine == "staged":
        monkeypatch.setenv("GINNO_STREAM_ENGINE", "staged")
    _allow_all_and_no_coalesce(monkeypatch)
    sid = create_session([script(text="你好,Ginno!这是一条纯文本回复。")])
    frames = _drive_turn(sid, "打个招呼")
    _assert_scenario("plain_text", _golden_lines(frames))


@pytest.mark.parametrize("engine", ["legacy", "staged"])
def test_golden_tool_call_turn(client, create_session, monkeypatch, engine):
    """场景 2:一次 bash 工具调用轮 —— tool.start/args/input/end + 收尾文本。"""
    if engine == "staged":
        monkeypatch.setenv("GINNO_STREAM_ENGINE", "staged")
    _allow_all_and_no_coalesce(monkeypatch)
    sid = create_session(
        [
            script(
                tool_calls=[
                    script_tool_call("bash", {"command": "echo hello-golden"})
                ]
            ),
            script(text="命令已执行,输出如上。"),
        ]
    )
    frames = _drive_turn(sid, "运行 echo 并解读输出")
    _assert_scenario("tool_call_bash", _golden_lines(frames))


@pytest.mark.parametrize("engine", ["legacy", "staged"])
def test_golden_multi_chunk_stream_turn(client, create_session, monkeypatch, engine):
    """场景 3:多片流式轮 —— 3 个 LLM chunk ↔ 3 帧 token.delta(1:1)。"""
    if engine == "staged":
        monkeypatch.setenv("GINNO_STREAM_ENGINE", "staged")
    _allow_all_and_no_coalesce(monkeypatch)
    model = MultiChunkScripted(pieces=["Golden ", "chunk ", "stream!"])
    sid = create_session(model)
    frames = _drive_turn(sid, "流式回复一段话")
    _assert_scenario("multi_chunk", _golden_lines(frames))

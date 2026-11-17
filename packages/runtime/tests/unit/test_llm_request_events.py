"""T3 — LLM 请求级可观测事件（llm.request / llm.response）。

覆盖两层：
1. graph._ainvoke_rate_limited 的埋点：req_id 配对、重试 attempt 递增
   （429 退避 + 流停顿两族）、失败带异常类型名、usage 提取、埋点任何环节
   抛异常都不影响主 turn、事件不含 prompt 正文/密钥。
2. engine custom 分支的翻译：ginno_llm_event 帧 → llm.request / llm.response
   经 emit/safe_send 下发，字段白名单过滤（第二道防泄漏防线）。
3. 两层接线：graph 层真实造帧喂进真实 _stream_graph，端到端配对。

退避加速沿用 test_rate_limit_backoff.py 先例（monkeypatch asyncio.sleep），
并按 goal 约定同时 monkeypatch _RATE_LIMIT_WAIT_S / _STREAM_STALL_WAIT_S。
"""

from __future__ import annotations


import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ginno_runtime import graph as graph_mod
from ginno_runtime import server_shared
from ginno_runtime.api import stream as stream_mod
from ginno_runtime.graph import _ainvoke_rate_limited
from ginno_runtime.graph_models import _llm_call_meta, _llm_usage_of

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
async def _no_sleep(_s):
    return None


class _FakeBound:
    """bind_tools 后的模型替身：__module__ 伪装成 langchain_openai 以验证
    provider 推导；failures 次抛 exc 后返回 result。"""

    __module__ = "langchain_openai"

    def __init__(self, result=None, failures=0, exc=None):
        self.model_name = "glm-4.6"
        self.result = result if result is not None else AIMessage(content="ok")
        self.failures = failures
        self.exc = exc or RuntimeError("APIError: Backend returned 429")
        self.calls = 0

    async def ainvoke(self, msgs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return self.result


@pytest.fixture
def llm_frames(monkeypatch):
    """接管 langgraph.config.get_stream_writer，收集 ginno_llm_event 帧。"""
    frames: list[dict] = []

    def _writer(payload):
        frames.append(payload)

    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: _writer)
    return frames


def _events(frames, kind):
    return [f["ginno_llm_event"] for f in frames
            if f.get("ginno_llm_event", {}).get("kind") == kind]


def _usage_msg():
    return AIMessage(
        content="hello",
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_token_details": {"cache_read": 40},
        },
    )


async def test_engine_translates_llm_frames(llm_frames):
    """engine 翻译分支已移除（2026-11-18）：graph 埋点仍在 custom 总线上发
    ginno_llm_event，但 engine 不再翻译成 llm.request/llm.response 下发。

    原因：bisect1/2 实锤该分支在 CI(ubuntu) 上导致 runtime-tests 63% 后
    卡死 16 分钟被 runner 关停（main 同区间 0.36s；只撤该分支 CI 9m44s
    通过），且用户已要求前端不渲染 llm 行——分支唯一消费方已不存在。
    本测试钉住：graph 侧事件仍发（埋点未删），防未来误删埋点。
    """
    from ginno_runtime import graph as g

    await g._ainvoke_rate_limited(_FakeBound(), [{"role": "user", "content": "hi"}])
    assert llm_frames, "graph 埋点必须在 custom 总线上发 ginno_llm_event"
    kinds = {f.get("ginno_llm_event", {}).get("kind") for f in llm_frames}
    assert kinds >= {"request", "response"}


def test_engine_no_longer_translates_llm_frames():
    """防线哨兵：engine.py 不得再包含 llm 翻译分支（CI 卡死元凶）。

    恢复前置条件（写在注释里给未来的人）：先查清 ubuntu runner 上卡死
    的根因——本地 macOS 全绿、writer 阻塞/flush 互锁/队列有界三个假设
    均已实证否掉，疑与环境相关。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src/ginno_runtime/api/stream/engine.py"
    text = src.read_text(encoding="utf-8")
    assert '"ginno_llm_event" in payload' not in text, (
        "engine 翻译分支是 CI(ubuntu) 63% 卡死元凶（bisect1/2），不得恢复"
    )


# --------------------------------------------------------------------------- #
# graph 层：正常请求
# --------------------------------------------------------------------------- #
async def test_request_response_pair_and_fields(llm_frames):
    bound = _FakeBound(result=_usage_msg())
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    out = await _ainvoke_rate_limited(bound, msgs)
    assert out is bound.result

    reqs, resps = _events(llm_frames, "request"), _events(llm_frames, "response")
    assert len(reqs) == 1 and len(resps) == 1
    req, resp = reqs[0], resps[0]
    # req_id 配对
    assert req["req_id"] and req["req_id"] == resp["req_id"]
    assert req["attempt"] == resp["attempt"] == 1
    # 元数据
    assert req["model"] == "glm-4.6"
    assert req["provider"] == "openai"
    assert req["messages_count"] == 2
    # response 字段
    assert resp["ok"] is True
    assert isinstance(resp["elapsed_ms"], int) and resp["elapsed_ms"] >= 0
    assert "error" not in resp
    u = resp["usage"]
    assert u["input_tokens"] == 100
    assert u["output_tokens"] == 20
    assert u["total_tokens"] == 120
    assert u["cache_read_tokens"] == 40


async def test_usage_absent_when_provider_reports_none(llm_frames):
    bound = _FakeBound(result=AIMessage(content="no usage"))
    await _ainvoke_rate_limited(bound, ["m"])
    resp = _events(llm_frames, "response")[0]
    assert resp["ok"] is True
    assert "usage" not in resp  # 拿不到就省略，不抛不造数


# --------------------------------------------------------------------------- #
# graph 层：重试 attempt 递增（429 退避族 + 流停顿族）
# --------------------------------------------------------------------------- #
async def test_rate_limit_retry_attempts_increment(llm_frames, monkeypatch):
    monkeypatch.setattr(graph_mod, "_RATE_LIMIT_WAIT_S", 0.01)
    monkeypatch.setattr("ginno_runtime.graph.asyncio.sleep", _no_sleep)
    bound = _FakeBound(result=_usage_msg(), failures=2,
                       exc=RuntimeError("APIError: Backend returned 429"))
    out = await _ainvoke_rate_limited(bound, ["m"])
    assert out is bound.result and bound.calls == 3

    reqs, resps = _events(llm_frames, "request"), _events(llm_frames, "response")
    assert [r["attempt"] for r in reqs] == [1, 2, 3]
    assert [r["attempt"] for r in resps] == [1, 2, 3]
    assert [r["ok"] for r in resps] == [False, False, True]
    # 同一逻辑请求：req_id 全程稳定，(req_id, attempt) 逐次配对
    assert len({r["req_id"] for r in reqs + resps}) == 1
    for req, resp in zip(reqs, resps):
        assert req["req_id"] == resp["req_id"]
        assert req["attempt"] == resp["attempt"]
    assert resps[0]["error"] == "RuntimeError"
    assert resps[1]["error"] == "RuntimeError"
    assert "error" not in resps[2]
    assert resps[2]["usage"]["total_tokens"] == 120


async def test_stream_stall_retry_attempts_increment(llm_frames, monkeypatch):
    monkeypatch.setattr(graph_mod, "_STREAM_STALL_WAIT_S", 0.01)
    monkeypatch.setattr("ginno_runtime.graph.asyncio.sleep", _no_sleep)
    bound = _FakeBound(failures=1,
                       exc=RuntimeError("No streaming chunk received for 120s"))
    await _ainvoke_rate_limited(bound, ["m"])
    reqs, resps = _events(llm_frames, "request"), _events(llm_frames, "response")
    assert [r["attempt"] for r in reqs] == [1, 2]
    assert [r["ok"] for r in resps] == [False, True]
    assert resps[0]["error"] == "RuntimeError"


async def test_give_up_after_cap_emits_every_attempt(llm_frames, monkeypatch):
    monkeypatch.setattr(graph_mod, "_RATE_LIMIT_WAIT_S", 0.01)
    monkeypatch.setattr("ginno_runtime.graph.asyncio.sleep", _no_sleep)
    bound = _FakeBound(failures=99, exc=RuntimeError("429 rate limit exceeded"))
    with pytest.raises(RuntimeError):
        await _ainvoke_rate_limited(bound, ["m"])
    assert bound.calls == 6  # 首调 + 5 次退避（既有语义不变）
    reqs, resps = _events(llm_frames, "request"), _events(llm_frames, "response")
    assert len(reqs) == 6 and len(resps) == 6  # 显式计数，防 all() 空列表真空通过
    assert [r["attempt"] for r in reqs] == [1, 2, 3, 4, 5, 6]
    assert [r["attempt"] for r in resps] == [1, 2, 3, 4, 5, 6]
    assert all(r["ok"] is False for r in resps)
    assert all(r["error"] == "RuntimeError" for r in resps)


# --------------------------------------------------------------------------- #
# graph 层：失败与防泄漏
# --------------------------------------------------------------------------- #
async def test_non_retryable_failure_carries_error_type_only(llm_frames):
    secret = "sk-secret-key-12345 user prompt: 我的银行卡密码是 hunter2"
    bound = _FakeBound(failures=99, exc=ValueError(secret))
    with pytest.raises(ValueError):
        await _ainvoke_rate_limited(bound, ["m"])
    resp = _events(llm_frames, "response")[0]
    assert resp["ok"] is False
    assert resp["error"] == "ValueError"  # 只有类型名
    blob = json.dumps(llm_frames, ensure_ascii=False)
    assert "sk-secret-key-12345" not in blob
    assert "hunter2" not in blob
    assert "银行卡" not in blob


async def test_events_never_contain_prompt_content(llm_frames):
    msgs = [
        SystemMessage(content="TOP-SECRET-SYSTEM-PROMPT api_key=sk-aaa111"),
        HumanMessage(content="机密正文：launch code 999888"),
    ]
    await _ainvoke_rate_limited(_FakeBound(result=_usage_msg()), msgs)
    blob = json.dumps(llm_frames, ensure_ascii=False)
    for needle in ("TOP-SECRET-SYSTEM-PROMPT", "sk-aaa111", "机密正文", "999888"):
        assert needle not in blob
    req = _events(llm_frames, "request")[0]
    assert req["messages_count"] == 2  # 只带计数


# --------------------------------------------------------------------------- #
# graph 层：埋点零风险（反证：去掉吞异常保护这些测试会红）
# --------------------------------------------------------------------------- #
async def test_writer_raising_does_not_break_turn(monkeypatch):
    def _boom_writer(_payload):
        raise RuntimeError("observability channel exploded")

    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: _boom_writer)
    bound = _FakeBound(result=_usage_msg())
    out = await _ainvoke_rate_limited(bound, ["m"])
    assert out is bound.result  # 主 turn 照常返回


async def test_writer_factory_raising_does_not_break_turn(monkeypatch):
    def _boom():
        raise RuntimeError("no stream context")

    monkeypatch.setattr("langgraph.config.get_stream_writer", _boom)
    bound = _FakeBound()
    assert await _ainvoke_rate_limited(bound, ["m"]) is bound.result


async def test_meta_extraction_raising_does_not_break_turn(llm_frames, monkeypatch):
    def _boom_meta(_b, _m):
        raise RuntimeError("meta exploded")

    monkeypatch.setattr(graph_mod, "_llm_call_meta", _boom_meta)
    bound = _FakeBound(result=_usage_msg())
    out = await _ainvoke_rate_limited(bound, ["m"])
    assert out is bound.result
    # 埋点降级但仍在发：req_id 存在、元数据留空
    req = _events(llm_frames, "request")[0]
    assert req["req_id"]
    assert "model" not in req


async def test_usage_extraction_raising_still_emits_response(llm_frames):
    class _PoisonResult:
        @property
        def usage_metadata(self):
            raise RuntimeError("usage exploded")

    bound = _FakeBound(result=_PoisonResult())
    out = await _ainvoke_rate_limited(bound, ["m"])
    assert out is bound.result
    resp = _events(llm_frames, "response")[0]
    assert resp["ok"] is True
    assert "usage" not in resp


async def test_no_writer_available_degrades_silently(monkeypatch):
    monkeypatch.setattr("langgraph.config.get_stream_writer", lambda: None)
    bound = _FakeBound()
    assert await _ainvoke_rate_limited(bound, ["m"]) is bound.result


# --------------------------------------------------------------------------- #
# 纯助手单测
# --------------------------------------------------------------------------- #
def test_llm_call_meta_unwraps_binding_and_never_raises():
    class _Inner:
        __module__ = "langchain_anthropic.chat_models"
        model = "claude-sonnet-4-5"

    class _Binding:
        bound = _Inner()

    meta = _llm_call_meta(_Binding(), [1, 2, 3])
    assert meta == {"model": "claude-sonnet-4-5", "provider": "anthropic",
                    "messages_count": 3}

    class _Poison:
        @property
        def bound(self):
            raise RuntimeError("boom")

    assert _llm_call_meta(_Poison(), None) == {}


def test_llm_usage_of_never_raises():
    assert _llm_usage_of(None) is None
    assert _llm_usage_of(object()) is None
    u = _llm_usage_of(_usage_msg())
    assert u["total_tokens"] == 120 and u["cache_read_tokens"] == 40


# --------------------------------------------------------------------------- #
# engine 层：custom 帧翻译 + 白名单（驱动真实 _stream_graph）
# --------------------------------------------------------------------------- #
class _FakeWS:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


class _CustomGraph:
    """只吐 custom 帧的图替身——_stream_graph 的其余链路全真。"""

    def __init__(self, frames):
        self.frames = frames

    def astream(self, input_state, config=None, stream_mode=None):
        async def gen():
            for f in self.frames:
                yield f

        return gen()


def _ws_events(ws, name):
    out = []
    for raw in ws.sent:
        frame = json.loads(raw)
        if frame.get("event") == name:
            out.append(frame)
    return out


async def _drive(frames, sid="llm-obs-engine"):
    ws = _FakeWS()
    server_shared._SESSION_WS[sid] = [ws]
    server_shared._SESSIONS[sid] = {
        "session_id": sid, "project_slug": "default", "workspace": "/tmp",
        "agent_id": "dev", "type": None, "graph": object(),
        "context_folders": [], "primary_folder": None,
    }
    config = {"configurable": {
        "thread_id": sid, "turn_id": f"turn-{sid}", "project_slug": "default",
    }}
    try:
        await stream_mod._stream_graph(_CustomGraph(frames), config,
                                       input_state={"messages": []})
    finally:
        server_shared._SESSION_WS.pop(sid, None)
        server_shared._SESSIONS.pop(sid, None)
    return ws







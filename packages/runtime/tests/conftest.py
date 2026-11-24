"""Shared pytest fixtures for the Ginno runtime test suite.

Isolation model
---------------
Every store, the file checkpointer, and all settings read through
``ginno_runtime.paths.home()``, which honors the ``$GINNO_HOME`` env var at
*call time*. The autouse ``isolated_home`` fixture points ``$GINNO_HOME`` at a
fresh ``tmp_path`` per test and clears the server's process-wide globals, so no
test touches the real ``~/.ginno`` and none leaks state into another.

Two client styles
-----------------
* API + E2E tests are **sync** and use ``fastapi.testclient.TestClient`` (as a
  context manager, so the FastAPI lifespan runs and seeds the home).
* A few unit tests that drive ``build_graph`` directly are **async**
  (``asyncio_mode = "auto"``); never call ``TestClient`` from inside those.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from ginno_runtime import jsonx as json
from ginno_runtime import paths, server, server_shared
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call

# e2e/_sidecar.py is a plain (non-package) module imported by name; expose its
# directory before importing it below. Do NOT recreate tests/e2e/conftest.py —
# a submodule conftest shadows the module name `conftest` on sys.path and
# breaks every `from conftest import ...` in this suite (main CI went red on
# 8d6cb13 for exactly that reason; shared fixtures live HERE, at the root).
_E2E_DIR = Path(__file__).parent / "e2e"
if str(_E2E_DIR) not in sys.path:
    sys.path.insert(0, str(_E2E_DIR))

import _sidecar  # noqa: E402  (sys.path injected above)

# e2e 的 _patch()（test_session_workflow_lifecycle.py）直接赋值三个模块的
# build_model 且**不还原**（无 fixture/teardown/finally），会永久污染整个
# pytest 会话：后跑的 unit 测试拿到别人的 ScriptedChatModel，自己的
# GINNO_FAKE_LLM_SCRIPTS 失效。test_a2a_inbound_exec 的 8 个 guard 用例曾因此
# 在 CI 全红（本地只跑 -m unit 不复现——e2e 段没跑，泄漏源不在）。
# 在 conftest 导入期抓住真实函数，供 isolated_home 每测试前还原。
from ginno_runtime.api import sessions as _sessions_api  # noqa: E402
from ginno_runtime.api import workflows as _wf_api  # noqa: E402

_ORIG_BUILD_MODEL = {
    server: server.build_model,
    _sessions_api: _sessions_api.build_model,
    _wf_api: _wf_api.build_model,
}


# --------------------------------------------------------------------------- #
# Offline tiktoken (session-scoped, autouse)
# --------------------------------------------------------------------------- #
# isolated_home 每测试换空 GINNO_HOME → token_count._cache_dir() 指向空目录
# → tiktoken.get_encoding("o200k_base") 冷缓存时从 openaipublic.blob 下载。
# 无外网（TUN 全接管）的机器上 socket 永久阻塞，曾把 pytest worker 卡到被
# pytest-timeout 杀掉、xdist INTERNALERROR 永不退出。这里在会话开头就把
# token_count 的编码器换成「真实但不联网」的 Encoding：单字节 BPE（256 个
# mergeable ranks）+ GPT-2 分词正则，encode 结果确定性、可断言，且不触发
# 任何网络。需要真实 o200k 词表的测试自行 monkeypatch 本替换
# （token_count._REAL_ENCODER 保留了原实现）。
@pytest.fixture(autouse=True, scope="session")
def _offline_tiktoken():
    """isolated_home 每测试换空 GINNO_HOME → token_count._cache_dir() 指向
    空目录 → tiktoken.get_encoding("o200k_base") 冷缓存时从 openaipublic.blob
    下载词表。无外网（TUN 全接管）的机器上 socket 永久阻塞，曾把 pytest
    worker 卡到被 pytest-timeout 杀掉、xdist INTERNALERROR 永不退出。

    会话开头把 token_count 的编码器换成「真实但不联网」的 Encoding：
    单字节 BPE（256 个 mergeable ranks）+ GPT-2 分词正则 + 一个 special
    token，encode 结果确定、可断言、零网络。确需真实 o200k 词表的测试
    自行 monkeypatch 本替换（先测 token_count._REAL_ENCODER 可用）。"""
    from ginno_runtime import token_count

    try:
        import tiktoken
    except ImportError:  # pragma: no cover — runtime always deps tiktoken
        yield
        return

    ranks = {bytes([b]): b for b in range(256)}
    enc = tiktoken.Encoding(
        name="ginno_test_bytes",
        pat_str=r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}| ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+""",
        mergeable_ranks=ranks,
        special_tokens={"<|endoftext|>": 256},
    )

    token_count._REAL_ENCODER = token_count._encoder
    token_count._encoder = lambda: enc  # type: ignore[assignment]
    token_count._cached_encoder = enc
    token_count._encoder_loaded = True
    yield
    token_count._encoder = token_count._REAL_ENCODER  # type: ignore[assignment]
    token_count._cached_encoder = None
    token_count._encoder_loaded = False


@pytest.fixture(autouse=True)
def _clean_running_turns():
    """每测试前清空运行中 turn 全局（e2e 段残留会污染后跑的 unit/api 断言
    ——test_running_work 曾因此整组 4≠0）。用 clear() 保持对象同一性：
    tasks/inject.py 与 running_work.py 都持有同一 dict 的引用。
    ``_RUNNING_TURNS_AT`` 同清：活性读路径把它当心跳时间戳，残留同名
    条目会把下一个测试里刚 arm 的 turn 误判成 stale。"""
    server_shared._RUNNING_TURNS.clear()
    server_shared._RUNNING_TURNS_AT.clear()
    yield


def as_fake_model(model_or_scripts: Any) -> ScriptedChatModel:
    """Normalize a test's LLM spec into a ScriptedChatModel.

    Accepts a ScriptedChatModel (as-is), a list of AIMessage turns, or a single
    AIMessage — so tests can write ``create_session([script(text="hi")])``.
    """
    if isinstance(model_or_scripts, ScriptedChatModel):
        return model_or_scripts
    if isinstance(model_or_scripts, AIMessage):
        return ScriptedChatModel(scripts=[model_or_scripts])
    if isinstance(model_or_scripts, list):
        return ScriptedChatModel(scripts=list(model_or_scripts))
    return model_or_scripts  # already some other model; pass through


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point $GINNO_HOME at a fresh temp dir and reset server globals per test."""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    # Pin the "auto" locale resolution away from the dev machine (a zh-region
    # macOS flips the product default to zh-CN while the suite asserts the en
    # variants). Tests that exercise auto→zh or a settings/env language patch
    # these explicitly — settings- and env-based resolution still wins over
    # this, so priority semantics stay testable.
    from ginno_runtime import lang

    monkeypatch.setattr(lang._locale_mod, "getdefaultlocale", lambda: (None, None))
    # Process-wide state that the lifespan does NOT reset between tests.
    server._SESSIONS.clear()
    server._USAGE_BY_SESSION.clear()
    server_shared._mcp = None
    server_shared._hooks = None
    # 还原被 e2e _patch() 永久改写的 build_model（见文件头 _ORIG_BUILD_MODEL 注释）。
    # 用 monkeypatch.setattr 而非裸赋值：测试自身若再 patch 也能正确回滚。
    for _mod, _fn in _ORIG_BUILD_MODEL.items():
        monkeypatch.setattr(_mod, "build_model", _fn)
    # Keep the default Playwright MCP out of unrelated tests (it would spawn a
    # headless browser on every server start). ensure_layout only re-seeds the
    # default when mcp.json is missing/empty, so a non-empty stub opts us out.
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    return tmp_path


@pytest.fixture
def seeded_home(isolated_home: Path) -> Path:
    """Isolated home with the standard layout + seed defaults written to disk."""
    paths.ensure_layout()
    return isolated_home


# --------------------------------------------------------------------------- #
# Fake LLM
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_model_factory() -> Callable[..., ScriptedChatModel]:
    """Build a ScriptedChatModel from scripted turns (text and/or tool calls)."""

    def _make(scripts: list) -> ScriptedChatModel:
        return ScriptedChatModel(scripts=scripts)

    return _make


# --------------------------------------------------------------------------- #
# HTTP client + build_model patch
# --------------------------------------------------------------------------- #
@pytest.fixture
def patch_build_model(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], Any]:
    """Patch build_model so create_session/ensure_session AND workflow runs
    use the fake (the two call sites live in different modules)."""

    def _patch(model: Any) -> Any:
        monkeypatch.setattr(
            "ginno_runtime.api.sessions.build_model", lambda *a, **k: model
        )
        monkeypatch.setattr(
            "ginno_runtime.api.workflows.build_model", lambda *a, **k: model
        )
        return model

    return _patch


@pytest.fixture
def client(isolated_home: Path) -> TestClient:
    """FastAPI TestClient with lifespan run (seeds the isolated home)."""
    with TestClient(server.app) as c:
        # Tests default privileged mode OFF so permission ask/deny is observable;
        # production defaults to ON. permission_node reads this live from settings.
        sp = isolated_home / "settings.json"
        if sp.exists():
            s = json.loads(sp.read_text())
            s["bypass_permissions"] = False
            sp.write_text(json.dumps(s))
        yield c


# --------------------------------------------------------------------------- #
# Session + WebSocket helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def create_session(client: TestClient, patch_build_model: Callable) -> Callable:
    """Patch the model and POST /sessions; returns the new session id.

    Signature: create_session(model, *, agent_id="dev", slug="default",
    workspace=None, title=None, workflow_id=None) -> session_id
    """

    def _create(
        model: Any,
        *,
        agent_id: str | None = "dev",
        slug: str = "default",
        workspace: str | None = None,
        title: str | None = None,
        workflow_id: str | None = None,
    ) -> str:
        patch_build_model(as_fake_model(model))
        ws = workspace or str(Path(os.environ["GINNO_HOME"]) / "ws")
        Path(ws).mkdir(parents=True, exist_ok=True)
        body: dict[str, Any] = {"project_slug": slug, "workspace": ws}
        if agent_id is not None:
            body["agent_id"] = agent_id
        if title is not None:
            body["title"] = title
        if workflow_id is not None:
            body["workflow_id"] = workflow_id
        r = client.post("/api/sessions", json=body)
        data = r.json()
        assert r.status_code == 200 and data.get("ok") is not False, data
        return data["id"]

    return _create


class WSConversation:
    """A thin sync wrapper over one WebSocket session connection.

    Usage::

        with ws_conv(session_id) as conv:
            conv.invoke("hello")
            events = conv.recv_until("message.end")
    """

    def __init__(self, client: TestClient, session_id: str) -> None:
        self._client = client
        self.session_id = session_id
        self._cm = None
        self.ws = None

    def __enter__(self) -> "WSConversation":
        self._cm = self._client.websocket_connect(f"/api/ws/sessions/{self.session_id}")
        self.ws = self._cm.__enter__()
        return self

    def __exit__(self, *exc: Any) -> bool | None:
        return self._cm.__exit__(*exc)

    def send(self, obj: dict) -> None:
        self.ws.send_json(obj)

    def recv(self) -> dict:
        return self.ws.receive_json()

    def invoke(
        self,
        message: str,
        agent_id: str | None = None,
        *,
        mentions: list | None = None,
        files: list | None = None,
        images: list | None = None,
    ) -> None:
        payload: dict[str, Any] = {"type": "invoke", "message": message}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if mentions is not None:
            payload["mentions"] = mentions
        if files is not None:
            payload["files"] = files
        if images is not None:
            payload["images"] = images
        self.send(payload)

    def respond_permission(self, decision: str) -> None:
        self.send({"type": "permission_response", "decision": decision})

    def respond_answer(
        self, answer: str = "", option_index: int | None = None, skip: bool = False
    ) -> None:
        """Answer a parked ask_user question (the ambiguity-resolution flow)."""
        self.send(
            {
                "type": "user_answer",
                "answer": answer,
                "option_index": option_index,
                "skip": skip,
            }
        )

    def recv_until(self, *terminal: str) -> list[dict]:
        """Receive events until one of the terminal event names is seen."""
        terminal_set = set(terminal)
        events: list[dict] = []
        while True:
            ev = self.recv()
            events.append(ev)
            if ev.get("event") in terminal_set:
                break
        return events


@pytest.fixture
def ws_conv(client: TestClient) -> Callable[[str], WSConversation]:
    """Factory: ws_conv(session_id) -> WSConversation bound to the test client."""

    def _make(session_id: str) -> WSConversation:
        return WSConversation(client, session_id)

    return _make


# --------------------------------------------------------------------------- #
# Knowledge / LLMWiki fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def kb_vault(isolated_home: Path) -> Path:
    """Create a small Obsidian vault with sample entries and enable knowledge.

    Returns the vault path. Resets the shared indexer cache before and after so
    tests never share index state.
    """
    from ginno_runtime.knowledge.indexer import reset_indexers

    vault = isolated_home / "vault"
    (vault / "Ginno" / "Wiki" / "concepts").mkdir(parents=True)
    (vault / "Ginno" / "Wiki" / "concepts" / "permission.md").write_text(
        "---\ntitle: LangGraph 权限节点\ntags: [arch, permission]\n---\n"
        "# 权限节点\n\n权限节点按 deny→ask→allow 顺序匹配，ask 会触发 interrupt 等待用户确认。\n",
        encoding="utf-8",
    )
    (vault / "Ginno" / "Wiki" / "concepts" / "checkpointer.md").write_text(
        "---\ntitle: 文件 Checkpointer\ntags: [arch, persistence]\n---\n"
        "# Checkpointer\n\n每个 session 一个 JSON 文件，原子写入，支持时间旅行恢复。"
        "相关 [[LangGraph 权限节点]]。\n",
        encoding="utf-8",
    )
    (vault / "Ginno" / "Wiki" / "concepts" / "cooking.md").write_text(
        "---\ntitle: 红烧肉做法\ntags: [cooking]\n---\n# 红烧肉\n\n五花肉焯水加冰糖酱油。\n",
        encoding="utf-8",
    )

    settings = (
        json.loads(paths.settings_path().read_text()) if paths.settings_path().exists() else {}
    )
    settings["knowledge"] = {
        "enabled": True,
        "vault_path": str(vault),
        "inject_top_k": 5,
        "inject_min_score": 0.3,
    }
    paths.settings_path().write_text(json.dumps(settings))
    reset_indexers()
    yield vault
    reset_indexers()


# --------------------------------------------------------------------------- #
# Small event helpers (importable in tests via `from conftest import ...`)
# --------------------------------------------------------------------------- #
def event_names(events: list[dict]) -> list[str]:
    return [e.get("event") for e in events]


def events_of(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


# Re-export builders so tests can `from conftest import script, script_tool_call`.
__all__ = [
    "script",
    "script_tool_call",
    "event_names",
    "events_of",
    "WSConversation",
]


# ---------------------------------------------------------------------------
# e2e sidecar guard（原 tests/e2e/conftest.py，8d6cb13 引入；因 conftest 遮蔽
# 问题上移到根 conftest，见文件头部注释）。
#
# e2e 会话级兜底只清理本进程通过 spawn_sidecar 启动的子进程。
# 各用例的 finally 仍负责各自的清理，包括直接使用 Popen 的源码模式。
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def harvest_leaked_sidecars():
    yield
    _sidecar.stop_spawned_sidecars()

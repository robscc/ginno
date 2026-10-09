"""MCP server config transport normalization.

Configs in the wild spell the HTTP transport several ways ("streamable-http",
"streamable_http", "Streamable_HTTP") and use either "transport" or "type" as
the key. A misspelled transport used to raise ``unknown transport`` at connect
time, silently dropping the server's tools (the 2026-08 web-search server never
registered because its config said ``"type": "streamable_http"``). These tests
pin the normalization so no spelling silently fails.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from ginno_runtime.mcp import registry as reg_mod
from ginno_runtime.mcp.registry import MCPServerConfig, MCPRegistry

pytestmark = pytest.mark.unit


def test_streamable_http_underscore_normalized():
    # The exact shape of the failing web-search server config.
    cfg = MCPServerConfig.from_dict(
        "搜索", {"type": "streamable_http", "url": "http://x/mcp"}
    )
    assert cfg.transport == "streamable-http"


@pytest.mark.parametrize(
    "raw",
    [
        "streamable-http",
        "streamable_http",
        "Streamable_HTTP",
        "STREAMABLE-HTTP",
        " streamable-http ",
    ],
)
def test_streamable_http_spelling_variants(raw):
    cfg = MCPServerConfig.from_dict("s", {"type": raw, "url": "http://x/mcp"})
    assert cfg.transport == "streamable-http"


def test_streamablehttp_no_separator_accepted():
    cfg = MCPServerConfig.from_dict("s", {"type": "streamablehttp", "url": "http://x"})
    assert cfg.transport == "streamablehttp"


@pytest.mark.parametrize(
    "raw,expected",
    [("stdio", "stdio"), ("sse", "sse"), ("http", "http"), ("HTTP", "http")],
)
def test_other_transports_passthrough(raw, expected):
    cfg = MCPServerConfig.from_dict("s", {"type": raw, "url": "http://x", "command": "c"})
    assert cfg.transport == expected


def test_transport_key_wins_over_type():
    cfg = MCPServerConfig.from_dict(
        "s", {"transport": "sse", "type": "stdio", "url": "http://x"}
    )
    assert cfg.transport == "sse"


def test_defaults_to_stdio_when_absent():
    cfg = MCPServerConfig.from_dict("s", {"command": "npx", "args": []})
    assert cfg.transport == "stdio"


def test_normalized_transport_is_dispatchable():
    """The normalized value must match a branch in _LiveServer._run_inner,
    i.e. it is one of the recognized transports (never 'unknown')."""
    recognized = {"stdio", "sse", "streamable-http", "streamablehttp", "http"}
    for raw in ["streamable_http", "streamable-http", "Streamable_HTTP"]:
        cfg = MCPServerConfig.from_dict("s", {"type": raw, "url": "http://x"})
        assert cfg.transport in recognized


# --------------------------------------------------------------------------- #
# Lazy retry of failed connections (2026-08-10 incident: all servers failed
# with a DNS blip at boot and stayed dead for days — nothing retried them).
# --------------------------------------------------------------------------- #
class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = None
        self.inputSchema = None


class _FakeLiveServer:
    """Stands in for _LiveServer: fails the first ``fail_times`` connects."""

    fail_times = 0

    def __init__(self, config) -> None:
        self.config = config
        self.tools: list = []

    async def connect(self) -> None:
        if type(self).fail_times > 0:
            type(self).fail_times -= 1
            raise OSError("simulated DNS failure")
        self.tools = [_FakeTool("tool_a")]

    async def close(self) -> None:
        pass


@pytest.fixture()
def fake_live(monkeypatch):
    _FakeLiveServer.fail_times = 0
    monkeypatch.setattr(reg_mod, "_LiveServer", _FakeLiveServer)
    return _FakeLiveServer


def _registry_with(tmp_path, names=("s1",)) -> MCPRegistry:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    n: {"type": "streamable-http", "url": "http://x/mcp"} for n in names
                }
            }
        )
    )
    return MCPRegistry(config_path=cfg)


async def test_failed_connect_is_tracked_and_recovered(tmp_path, fake_live):
    reg = _registry_with(tmp_path)
    fake_live.fail_times = 1  # first attempt fails, retry succeeds
    await reg.connect_all()
    assert reg.list_tools() == []
    assert reg.failed_servers == ["s1"]
    assert not reg.has_pending_failures(cooldown_s=3600)  # inside cooldown
    assert await reg.retry_failed(cooldown_s=3600) == []  # refused: too soon

    recovered = await reg.retry_failed(cooldown_s=0)
    assert recovered == ["s1"]
    assert reg.list_tools() == ["tool_a"]
    assert reg.failed_servers == []
    assert not reg.has_pending_failures(cooldown_s=0)


async def test_retry_is_noop_when_everything_live(tmp_path, fake_live):
    reg = _registry_with(tmp_path)
    await reg.connect_all()
    assert reg.list_tools() == ["tool_a"]
    assert await reg.retry_failed(cooldown_s=0) == []


async def test_still_failing_server_stays_failed(tmp_path, fake_live):
    reg = _registry_with(tmp_path)
    fake_live.fail_times = 10**9  # never recovers
    await reg.connect_all()
    assert reg.failed_servers == ["s1"]
    assert await reg.retry_failed(cooldown_s=0) == []
    assert reg.failed_servers == ["s1"]


def test_wrapped_names_match_langchain_wrapper():
    """list_wrapped_tools() is the per-turn graph-refresh fingerprint and must
    produce EXACTLY the names _wrap_tool gives the langchain tools — raw
    list_tools() names never equal the wrapped session list (2026-08-10)."""
    from ginno_runtime.mcp.registry import _full_tool_name

    assert _full_tool_name("钉钉文档", "get_document_content") == (
        "mcp_钉钉文档_get_document_content"
    )
    live = reg_mod._LiveServer(MCPServerConfig(name="s1", transport="http", url="http://x"))
    live.tools = [_FakeTool("tool_a"), _FakeTool("tool_b")]
    wrapped = [t.name for t in live.to_langchain_tools()]
    assert wrapped == [_full_tool_name("s1", n) for n in ("tool_a", "tool_b")]


def test_all_langchain_tools_sorted_regardless_of_connect_order(isolated_home):
    """The tools array rides at the FRONT of the provider cache prefix, so its
    order must not depend on server connect order — a reconnect reshuffle
    would invalidate the whole prefix cache (2026-08 cache-rate diagnosis)."""
    reg = MCPRegistry()
    live_b = reg_mod._LiveServer(MCPServerConfig(name="beta", transport="http", url="http://x"))
    live_b.tools = [_FakeTool("zeta"), _FakeTool("alpha")]
    live_a = reg_mod._LiveServer(MCPServerConfig(name="alpha", transport="http", url="http://x"))
    live_a.tools = [_FakeTool("mid")]
    # insertion order deliberately NOT alphabetical
    reg._live = {"beta": live_b, "alpha": live_a}
    names = [t.name for t in reg.all_langchain_tools()]
    assert names == sorted(names)
    # and stable across a reshuffled reconnect order
    reg._live = {"alpha": live_a, "beta": live_b}
    assert [t.name for t in reg.all_langchain_tools()] == names


# --------------------------------------------------------------------------- #
# enabled / disabled_tools（设置页 UI 化契约：服务器级与工具级开关）
# --------------------------------------------------------------------------- #
def _registry_loaded(tmp_path, names=("s1",)) -> MCPRegistry:
    """_registry_with + 立即加载：后续测试要在 connect_all 之前改配置字段。"""
    reg = _registry_with(tmp_path, names=names)
    reg.load()
    return reg


def test_from_dict_switch_fields_default_on():
    cfg = MCPServerConfig.from_dict("s", {"command": "npx"})
    assert cfg.enabled is True
    assert cfg.disabled_tools == []


def test_from_dict_parses_switch_fields():
    cfg = MCPServerConfig.from_dict(
        "s", {"command": "npx", "enabled": False, "disabled_tools": ["a", "b"]}
    )
    assert cfg.enabled is False
    assert cfg.disabled_tools == ["a", "b"]


@pytest.mark.parametrize("raw", [None, "a", {"a": 1}, 42])
def test_from_dict_tolerates_malformed_disabled_tools(raw):
    # 历史手编配置常见 null / 非数组：按空黑名单处理，不能炸 load()
    assert MCPServerConfig.from_dict("s", {"disabled_tools": raw}).disabled_tools == []


def test_from_dict_coerces_disabled_tools_elements():
    assert MCPServerConfig.from_dict("s", {"disabled_tools": [1, "b"]}).disabled_tools == ["1", "b"]


async def test_disabled_server_stays_offline_but_visible_in_status(tmp_path, fake_live):
    reg = _registry_loaded(tmp_path, names=("off", "on"))
    reg.servers["off"].enabled = False
    await reg.connect_all()
    assert set(reg._live) == {"on"}
    # 禁用 ≠ 失败：不进 _failed，不触发 lazy retry 冷却
    assert reg.failed_servers == []
    assert not reg.has_pending_failures(cooldown_s=0)
    st = {s["name"]: s for s in reg.status()}
    assert st["off"]["enabled"] is False
    assert st["off"]["connected"] is False
    assert st["off"]["connectedAt"] is None
    assert st["on"]["enabled"] is True
    # 轻负载（不带 ?tools=1）：不返回明细字段
    assert "toolDetails" not in st["off"]
    assert "disabledTools" not in st["on"]


async def test_disabled_server_is_never_reconnected(tmp_path, fake_live):
    reg = _registry_loaded(tmp_path, names=("off",))
    reg.servers["off"].enabled = False
    await reg.connect_all(only={"off"})  # 行级重试打到禁用服务器也是 no-op
    assert reg._live == {}


def test_disabled_tools_kept_out_of_graph_but_visible_in_status(tmp_path):
    reg = _registry_loaded(tmp_path)
    reg.servers["s1"].disabled_tools = ["tool_b"]
    live = reg_mod._LiveServer(reg.servers["s1"])
    live.tools = [_FakeTool("tool_a"), _FakeTool("tool_b")]
    reg._live["s1"] = live

    # graph 视角：被禁工具不包装（会话图因此拿不到它）
    assert [t.name for t in live.to_langchain_tools()] == ["mcp_s1_tool_a"]
    assert reg.list_wrapped_tools() == ["mcp_s1_tool_a"]
    assert [t.name for t in reg.all_langchain_tools()] == ["mcp_s1_tool_a"]
    # 原始/展示视角：清单仍完整，?tools=1 明细里带 disabledTools 标记数据
    assert reg.list_tools() == ["tool_a", "tool_b"]
    st = {s["name"]: s for s in reg.status(include_tools=True)}["s1"]
    assert st["disabledTools"] == ["tool_b"]
    assert st["tools"] == 2
    assert [d["name"] for d in st["toolDetails"]] == ["tool_a", "tool_b"]


class _Ann:
    def __init__(self, ro: bool = False, destr: bool = False) -> None:
        self.readOnlyHint = ro
        self.destructiveHint = destr


class _AnnTool(_FakeTool):
    """带 annotations 的工具对象（模拟 MCP SDK 的 Tool）。"""

    def __init__(self, name: str, ann=None) -> None:
        super().__init__(name)
        self.description = f"desc {name}"
        if ann is not None:
            self.annotations = ann


def test_status_tool_details_annotation_hints(tmp_path):
    reg = _registry_loaded(tmp_path)
    live = reg_mod._LiveServer(reg.servers["s1"])
    live.tools = [
        _AnnTool("plain"),  # 无 annotations → 缺省容忍
        _AnnTool("ro", _Ann(ro=True)),
        _AnnTool("destr", _Ann(destr=True)),
    ]
    reg._live["s1"] = live
    det = {d["name"]: d for d in reg.status(include_tools=True)[0]["toolDetails"]}
    assert det["plain"]["readOnly"] is False and det["plain"]["destructive"] is False
    assert det["ro"]["readOnly"] is True and det["ro"]["destructive"] is False
    assert det["destr"]["readOnly"] is False and det["destr"]["destructive"] is True
    assert det["ro"]["description"] == "desc ro"


def test_status_connected_at_epoch_or_null(tmp_path):
    reg = _registry_loaded(tmp_path, names=("up", "down"))
    live = reg_mod._LiveServer(reg.servers["up"])
    live.tools = [_FakeTool("tool_a")]
    live.connected_at = 1728472800.5  # 模拟 _run_inner 连接成功后盖的时间戳
    reg._live["up"] = live
    st = {s["name"]: s for s in reg.status()}
    assert st["up"]["connected"] is True
    assert st["up"]["connectedAt"] == 1728472800  # epoch 秒（取整）
    assert st["down"]["connectedAt"] is None


def test_unconfigured_wrapped_names_includes_disabled_server_and_tools(tmp_path):
    reg = _registry_loaded(tmp_path, names=("s1", "s2"))
    reg.servers["s1"].disabled_tools = ["gone_tool"]
    reg.servers["s2"].enabled = False
    names = {"mcp_s1_gone_tool", "mcp_s1_kept", "mcp_s2_any", "mcp_deleted_x"}
    # 禁用（服务器或工具）与删除同侧：允许会话图收缩重建，禁用即刻生效
    assert reg.unconfigured_wrapped_names(names) == {
        "mcp_s1_gone_tool",
        "mcp_s2_any",
        "mcp_deleted_x",
    }


def test_unconfigured_wrapped_names_longest_prefix_wins(tmp_path):
    # 服务器名互为前缀时（s 与 s_sub）必须取最长匹配还原工具名，否则
    # s 的黑名单会误伤 s_sub 的工具（配置序还保证 s 先出现）。
    reg = _registry_loaded(tmp_path, names=("s", "s_sub"))
    reg.servers["s"].disabled_tools = ["sub_tool"]
    assert reg.unconfigured_wrapped_names({"mcp_s_sub_tool"}) == set()
    assert reg.unconfigured_wrapped_names({"mcp_s_sub_tool_x"}) == set()


async def test_reconnect_endpoint_server_param(tmp_path, fake_live, monkeypatch):
    from ginno_runtime import server_shared
    from ginno_runtime.api.config import reconnect_mcp_endpoint

    reg = _registry_loaded(tmp_path, names=("s1", "s2"))
    fake_live.fail_times = 1  # s1（配置序在前）首连失败，s2 成功
    await reg.connect_all()
    assert set(reg.failed_servers) == {"s1"}
    monkeypatch.setattr(server_shared, "_mcp", reg)

    fake_live.fail_times = 0
    res = await reconnect_mcp_endpoint(server="s1")
    assert res["ok"] is True
    assert set(reg._live) == {"s1", "s2"}
    assert reg.failed_servers == []

    # 未配置的名字：ok:false + 错误信息，registry 不动
    res = await reconnect_mcp_endpoint(server="ghost")
    assert res["ok"] is False and "ghost" in res["error"]


async def test_list_mcp_endpoint_tools_param_gates_detail(tmp_path, fake_live, monkeypatch):
    """GET /api/mcp 的明细（disabledTools/toolDetails）只在 ?tools=1 时返回：
    5s 轮询走轻负载，工具 tab/详情才带参（前端契约修正 2026-10-09）。"""
    from ginno_runtime import server_shared
    from ginno_runtime.api.config import list_mcp

    reg = _registry_loaded(tmp_path)
    reg.servers["s1"].disabled_tools = ["tool_b"]
    await reg.connect_all()  # _FakeLiveServer 成功 → tools=[tool_a]
    monkeypatch.setattr(server_shared, "_mcp", reg)

    light = await list_mcp()
    assert light["status"][0]["enabled"] is True
    assert "disabledTools" not in light["status"][0]
    assert "toolDetails" not in light["status"][0]

    full = await list_mcp(tools=True)
    assert full["status"][0]["disabledTools"] == ["tool_b"]
    assert [d["name"] for d in full["status"][0]["toolDetails"]] == ["tool_a"]


# --------------------------------------------------------------------------- #
# sync_configs（PUT /api/mcp 落盘后热更新内存 configs——开关无响应修复）
# --------------------------------------------------------------------------- #
def _raw_config(names_enabled: dict) -> dict:
    """构造 PUT /api/mcp 的请求体：{name: enabled} → mcpServers 条目。"""
    return {
        "mcpServers": {
            n: {"type": "streamable-http", "url": "http://x/mcp", "enabled": e}
            for n, e in names_enabled.items()
        }
    }


async def test_sync_configs_disable_evicts_live_and_status_flips(tmp_path, fake_live):
    """核心回归：PUT enabled=false 后 status 立即翻缺——不再被 5s 轮询翻回。"""
    reg = _registry_loaded(tmp_path, names=("s1", "s2"))
    await reg.connect_all()
    assert set(reg._live) == {"s1", "s2"}

    evicted = reg.sync_configs(_raw_config({"s1": False, "s2": True}))
    await asyncio.sleep(0)  # 让 _spawn_close 的后台收尾任务跑一轮

    assert evicted == ["s1"]
    assert "s1" not in reg._live and "s2" in reg._live
    assert reg.servers["s1"].enabled is False
    # 逐出的失败簿记被清空（禁用不是失败）
    assert "s1" not in reg._failed and "s1" not in reg._last_error
    st = {s["name"]: s for s in reg.status()}
    assert st["s1"]["enabled"] is False and st["s1"]["connected"] is False
    assert st["s2"]["enabled"] is True and st["s2"]["connected"] is True


async def test_sync_configs_enable_then_connect_all_connects(tmp_path, fake_live):
    """enable 后 connect_all（reconnect?server= 的路径）能把服务器接起来。"""
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps({"mcpServers": {"s1": {"type": "streamable-http", "url": "http://x/mcp", "enabled": False}}})
    )
    reg = MCPRegistry(config_path=cfg)
    reg.load()
    await reg.connect_all()
    assert reg._live == {}  # disabled 不连接

    reg.sync_configs(_raw_config({"s1": True}))
    await reg.connect_all(only={"s1"})
    assert "s1" in reg._live


async def test_sync_configs_mutates_in_place_for_live_servers(tmp_path, fake_live):
    """开关字段必须原位改写：_LiveServer.config 与 servers[name] 是同一对象，
    已连接服务器的 _active_tools() 立即按新 disabled_tools 过滤（「下一 turn
    生效」依赖 unconfigured_wrapped_names 读到新值）。"""
    reg = _registry_loaded(tmp_path, names=("s1",))
    await reg.connect_all()
    live = reg._live["s1"]
    live.tools = [_FakeTool("t1"), _FakeTool("t2")]
    obj = reg.servers["s1"]

    reg.sync_configs(
        {"mcpServers": {"s1": {"type": "streamable-http", "url": "http://x/mcp", "disabled_tools": ["t1"]}}}
    )
    assert reg.servers["s1"] is obj  # 对象身份保留
    assert live.config is obj  # 活连接共享同一配置对象
    assert obj.disabled_tools == ["t1"]
    # 真实 _LiveServer._active_tools() 读 self.config.disabled_tools，共享对象
    # ⇒ 图视角立即排除 t1（fake 无该方法，这里断言它依赖的数据链）；
    # status 的 toolDetails 走 live.tools 原始上报，仍完整可见。
    assert [t.name for t in live.tools if t.name not in set(live.config.disabled_tools)] == ["t2"]
    assert [d["name"] for d in reg.status(include_tools=True)[0]["toolDetails"]] == ["t1", "t2"]


def test_sync_configs_keeps_connection_params_stale(tmp_path, fake_live):
    """连接参数（url）刻意不热同步：已建立连接按旧参数通信，内存与连接一致；
    改参数走 Save & Reload 全量重建。"""
    reg = _registry_loaded(tmp_path, names=("s1",))
    reg.sync_configs({"mcpServers": {"s1": {"type": "streamable-http", "url": "http://NEW/mcp"}}})
    assert reg.servers["s1"].url == "http://x/mcp"


async def test_sync_configs_add_and_remove(tmp_path, fake_live):
    reg = _registry_loaded(tmp_path, names=("s1",))
    await reg.connect_all()

    evicted = reg.sync_configs(
        {
            "mcpServers": {
                "s2": {"type": "streamable-http", "url": "http://x/mcp"},
            }
        }
    )
    await asyncio.sleep(0)
    assert evicted == ["s1"]  # 删除已连接的服务器也要驱离
    assert "s1" not in reg.servers and "s1" not in reg._live
    assert "s2" in reg.servers and "s2" not in reg._live  # 新增条目进内存，未连接
    assert [s["name"] for s in reg.status()] == ["s2"]


def test_sync_configs_tolerates_malformed_entry(tmp_path, fake_live):
    reg = _registry_loaded(tmp_path, names=("s1", "s2"))
    # s1 条目坏掉（connect_timeout 非数）→ 只跳过该条；s2 正常禁用
    reg.sync_configs(
        {
            "mcpServers": {
                "s1": {"type": "streamable-http", "url": "http://x/mcp", "connect_timeout": "abc"},
                "s2": {"type": "streamable-http", "url": "http://x/mcp", "enabled": False},
            }
        }
    )
    assert reg.servers["s1"].enabled is True  # 保留旧配置
    assert reg.servers["s2"].enabled is False


def test_sync_configs_empty_dict_clears_all(tmp_path, fake_live):
    reg = _registry_loaded(tmp_path, names=("s1",))
    reg.sync_configs({"mcpServers": {}})
    assert reg.servers == {}


async def test_put_mcp_endpoint_hot_syncs_registry(tmp_path, fake_live, monkeypatch, isolated_home):
    """端到端：PUT /api/mcp → 内存 configs 同步 → status 立即翻缺（不再被
    轮询翻回）。这是「设置页启停开关无响应」的端到端回归。"""
    from ginno_runtime import server_shared
    from ginno_runtime.api.config import put_mcp_endpoint
    from ginno_runtime import paths as paths_mod

    cfg_path = paths_mod.mcp_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"mcpServers": {"s1": {"type": "streamable-http", "url": "http://x/mcp"}}}))

    reg = MCPRegistry()  # 默认路径 = isolated_home 下的 mcp.json
    reg.load()
    await reg.connect_all()
    assert reg._live
    monkeypatch.setattr(server_shared, "_mcp", reg)

    body = {"mcpServers": {"s1": {"type": "streamable-http", "url": "http://x/mcp", "enabled": False}}}
    res = await put_mcp_endpoint(body)
    assert res == {"ok": True}
    await asyncio.sleep(0)

    st = reg.status()[0]
    assert st["enabled"] is False and st["connected"] is False
    # 文件也落盘了
    assert json.loads(cfg_path.read_text())["mcpServers"]["s1"]["enabled"] is False

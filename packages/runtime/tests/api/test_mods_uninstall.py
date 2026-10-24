"""DELETE /api/mods/{name} — uninstall wiring (design §5.1): install dir +
settings item removal, registration teardown (classic hooks, commands/tools/
agents registries), and ghost-row cleanup. Real FastAPI stack, stub channel —
no broker, no node."""

from __future__ import annotations

import pytest

import ginno_runtime.server_shared as server_shared
from ginno_runtime import jsonx as json
from ginno_runtime.agents import mod_registry
from ginno_runtime.commands import mod_commands
from ginno_runtime.mods import api as mods_api
from ginno_runtime.mods import bridge_utils
from ginno_runtime.tools import mod_tools

pytestmark = pytest.mark.api


class UninstallStubChannel:
    """The uninstall + list surface: mutable mods_state, never connected
    (no config re-push to wait on)."""

    def __init__(self):
        self.mods_state: dict[str, dict] = {}
        self.is_connected = False

    def ensure_started(self):
        pass

    def availability(self):
        return {"status": "connected", "detail": "", "connected": True, "mods": []}

    def compat_summary(self):
        return {}


@pytest.fixture
def stub_channel(monkeypatch):
    stub = UninstallStubChannel()
    monkeypatch.setattr(mods_api, "get_channel", lambda: stub)
    return stub


@pytest.fixture
def installed_mod(isolated_home):
    """A drop-in JS-shape mod in the global dir + its settings item."""
    mod_dir = isolated_home / "mods" / "alpha"
    (mod_dir / ".claude-plugin").mkdir(parents=True)
    (mod_dir / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "alpha", "version": "1.0.0"})
    )
    hooks_dir = mod_dir / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(json.dumps({"modules": ["hooks.ts"]}))
    bridge_utils.update_mod_item("alpha", {"enabled": True, "source": "global"})
    return mod_dir


def test_uninstall_removes_dir_item_and_registrations(client, isolated_home, installed_mod, stub_channel):
    # Registration slices a live mod leaves behind: one per registry, plus a
    # classic-shape hooks entry in the real dispatcher.
    mod_commands.register("greet", "alpha", "say hi")
    mod_registry.register("helper", "alpha", "an agent")
    mod_tools.register_tool("alpha", "fetch", "a tool")
    dispatcher = server_shared._hooks
    dispatcher.register_plugin(
        "alpha",
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "run.sh"}]}]}},
        str(installed_mod),
    )
    assert dispatcher.plugin_hook_count() == 1
    # broker 状态缓存先行——卸载必须连它一起清,否则列表出幽灵行。
    stub_channel.mods_state["alpha"] = {"name": "alpha", "status": "loaded"}

    r = client.delete("/api/mods/alpha")
    body = r.json()
    assert body["ok"] is True
    assert body["removed"]["scope"] == "global"
    assert body["removed"]["path"] == str(installed_mod)

    assert not installed_mod.exists()
    assert "alpha" not in bridge_utils.load_mods_settings()["items"]
    assert mod_commands.lookup("greet") is None
    assert mod_registry.get("helper") is None
    assert not mod_tools.owns_tool("mcp__alpha__fetch")
    assert dispatcher.plugin_hook_count() == 0
    # 列表不再出现该 mod(目录 + settings 条目 + broker 状态缓存全清)。
    mods = client.get("/api/mods").json()["mods"]
    assert all(m["name"] != "alpha" for m in mods)


def test_uninstall_without_dir_still_cleans_item(client, isolated_home, stub_channel):
    """Drop-in dir already gone (deleted by hand) but the settings item and
    ghost state remain: uninstall from the UI must still succeed."""
    bridge_utils.update_mod_item("alpha", {"enabled": False, "source": "global"})
    r = client.delete("/api/mods/alpha")
    assert r.json()["ok"] is True
    assert "alpha" not in bridge_utils.load_mods_settings()["items"]


def test_uninstall_unknown_mod_is_an_error(client, stub_channel):
    assert client.delete("/api/mods/ghost").json()["ok"] is False


def test_uninstall_invalid_name_is_rejected(client, stub_channel):
    assert client.delete("/api/mods/bad%20name").json()["ok"] is False

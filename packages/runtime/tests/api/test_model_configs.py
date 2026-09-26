"""API tests for the v2 multi-provider model-config backend.

Covers (design: docs/design/multi-provider-model-config.md + P1 decisions):
* lazy mapping from the frozen legacy ``providers`` dict (decision Q1)
* CRUD via ``GET/PUT /api/model_configs`` (full-array replacement)
* verify-draft saves on success (decision Q4), nothing on failure
* three-protocol ``build_model`` dispatch incl. openai-responses (decision Q6)
* ``build_model_by_name`` three-tier matching
* deletion guard with reference list
* agent model-binding hard constraint (decision Q3) + runtime fallback
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime import paths
from ginno_runtime import providers as prov_mod

pytestmark = pytest.mark.api


def _write_legacy_settings(providers: dict, default_provider: str | None = None) -> None:
    settings: dict = {"providers": providers}
    if default_provider:
        settings["default_provider"] = default_provider
    paths.settings_path().write_text(json.dumps(settings))


def _read_settings() -> dict:
    return json.loads(paths.settings_path().read_text())


_LEGACY_THREE = {
    "anthropic": {"enabled": True, "api_key": "sk-ant", "default_model": "claude-x"},
    "openai": {"enabled": False, "api_key": "sk-oai", "default_model": "gpt-4o"},
    "custom": {
        "enabled": True,
        "api_key": "k",
        "base_url": "https://relay.example.com/v1",
        "model": "deepseek-v3",
    },
}


# ------------------------------ lazy mapping ------------------------------ #


def test_lazy_mapping_three_slots(client):
    _write_legacy_settings(_LEGACY_THREE, default_provider="custom")
    r = client.get("/api/model_configs").json()
    by_id = {c["id"]: c for c in r["configs"]}
    assert set(by_id) == {"anthropic", "openai", "custom"}
    # protocol mapping: anthropic slot stays anthropic; openai AND custom
    # collapse to openai-compatible (decision Q6 kebab strings)
    assert by_id["anthropic"]["protocol"] == "anthropic"
    assert by_id["openai"]["protocol"] == "openai-compatible"
    assert by_id["custom"]["protocol"] == "openai-compatible"
    # single-value model fields fold into models[] + default_model
    assert by_id["anthropic"]["models"] == ["claude-x"]
    assert by_id["custom"]["models"] == ["deepseek-v3"]
    assert by_id["custom"]["default_model"] == "deepseek-v3"
    # legacy default_provider is honoured as default_config fallback
    assert r["default_config"] == "custom"
    # empty custom name gets the doc §2.6 default display name
    assert by_id["custom"]["name"] == "自定义端点"


def test_lazy_mapping_never_writes_disk(client):
    _write_legacy_settings(_LEGACY_THREE)
    client.get("/api/model_configs")
    disk = _read_settings()
    # decision Q1: old keys frozen, new key only appears on an explicit save
    assert isinstance(disk["providers"], dict)
    assert "model_configs" not in disk


def test_legacy_providers_get_maps_new_store(client):
    # save a v2 array, then read it back through the legacy endpoint
    configs = [
        {
            "id": "custom",
            "name": "中转",
            "protocol": "openai-compatible",
            "base_url": "https://r/v1",
            "api_key": "k",
            "models": ["m1"],
            "default_model": "m1",
            "enabled": True,
        }
    ]
    client.put("/api/model_configs", json={"configs": configs, "default_config": "custom"})
    legacy = client.get("/api/providers").json()
    assert legacy["default_provider"] == "custom"
    assert legacy["providers"]["custom"]["enabled"] is True
    # legacy shape: custom slot keeps its historical `model` field name
    assert legacy["providers"]["custom"]["model"] == "m1"


# ---------------------------------- CRUD ---------------------------------- #


def _relay(cid: str, **kw) -> dict:
    cfg = {
        "id": cid,
        "name": f"relay {cid}",
        "protocol": "openai-compatible",
        "base_url": f"https://{cid}.example.com/v1",
        "api_key": "sk-x",
        "models": ["m1", "m2"],
        "default_model": "m1",
        "enabled": True,
    }
    cfg.update(kw)
    return cfg


def test_put_and_get_model_configs_roundtrip(client):
    # full-array replacement is relative to the current view (the lazily
    # mapped legacy three), like a real client: edit + add, never blind-drop
    cur = client.get("/api/model_configs").json()["configs"]
    configs = cur + [_relay("relay-a"), _relay("relay-b", enabled=False)]
    r = client.put("/api/model_configs", json={"configs": configs, "default_config": "relay-a"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["default_config"] == "relay-a"
    # persisted under the NEW key only; legacy keys untouched
    disk = _read_settings()
    assert [c["id"] for c in disk["model_configs"]][-2:] == ["relay-a", "relay-b"]
    assert disk["default_config"] == "relay-a"
    assert "providers" not in disk or isinstance(disk.get("providers"), dict)
    got = client.get("/api/model_configs").json()
    assert {c["id"] for c in got["configs"]} >= {"relay-a", "relay-b"}


def test_put_model_configs_validation_errors(client):
    bad_model = _relay("relay-a", default_model="nope")
    r = client.put("/api/model_configs", json={"configs": [bad_model]})
    assert r.status_code == 400
    assert "models" in r.json()["error"]
    dup = [_relay("relay-a"), _relay("relay-a")]
    r = client.put("/api/model_configs", json={"configs": dup})
    assert r.status_code == 400
    assert "重复" in r.json()["error"]
    bad_proto = _relay("relay-a", protocol="bedrock")
    r = client.put("/api/model_configs", json={"configs": [bad_proto]})
    assert r.status_code == 400


# ---------------------------- verify → save (Q4) --------------------------- #


_DRAFT = {
    "name": "中转 A",
    "protocol": "openai-compatible",
    "base_url": "https://relay.example.com/v1",
    "api_key": "sk-draft",
    "models": ["qwen-plus"],
    "default_model": "qwen-plus",
}


def test_verify_draft_success_saves(client, monkeypatch):
    # seam: the network probe inside providers._verify_config
    monkeypatch.setattr(prov_mod, "verify_config_draft", lambda cfg: {"ok": True, "latency_ms": 42})
    r = client.post("/api/model_configs/verify", json=_DRAFT)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["latency_ms"] == 42
    cid = body["config"]["id"]
    assert cid  # backend assigns an id for a draft that had none
    assert body["config"]["verified_at"] > 0
    # saved to disk with the verified_at stamp
    disk = _read_settings()
    saved = {c["id"]: c for c in disk["model_configs"]}
    assert saved[cid]["verified_at"] == body["config"]["verified_at"]
    # an existing config is upserted by id, not duplicated
    r2 = client.post("/api/model_configs/verify", json={**_DRAFT, "id": cid, "name": "改名"})
    assert r2.json()["ok"] is True
    disk = _read_settings()
    matches = [c for c in disk["model_configs"] if c["id"] == cid]
    assert len(matches) == 1
    assert matches[0]["name"] == "改名"


def test_verify_draft_failure_not_saved(client, monkeypatch):
    monkeypatch.setattr(
        prov_mod,
        "verify_config_draft",
        lambda cfg: {"ok": False, "error": "连接超时", "latency_ms": 5},
    )
    r = client.post("/api/model_configs/verify", json=_DRAFT)
    # failure is a 200 + ok:false so the UI shows the yellow banner
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "连接超时" in body["error"]
    # nothing was written
    disk = _read_settings()
    assert "model_configs" not in disk


# ---------------------------- build_model dispatch ------------------------- #


def _patch_providers(monkeypatch, provs: dict):
    import ginno_runtime.models as M

    monkeypatch.setattr(M.prov_mod, "load_providers", lambda: provs)
    return M


class _FakeModel:
    def __init__(self, **kw):
        self.kw = kw


def test_build_model_dispatch_three_protocols(monkeypatch):
    M = _patch_providers(
        monkeypatch,
        {
            "ant": {
                "enabled": True,
                "protocol": "anthropic",
                "api_key": "k-ant",
                "default_model": "claude-x",
            },
            "compat": {
                "enabled": True,
                "protocol": "openai-compatible",
                "api_key": "k-oai",
                "base_url": "http://localhost:9/v1",
                "default_model": "qwen-plus",
                "enable_thinking": True,
            },
            "resp": {
                "enabled": True,
                "protocol": "openai-responses",
                "api_key": "k-resp",
                "default_model": "gpt-5",
                "org_id": "org-1",
            },
        },
    )
    # reset the cached lazily-built classes so the fakes are picked up
    monkeypatch.setattr(M, "_REASONING_CLS", None)
    monkeypatch.setattr(M, "_RESPONSES_CLS", None)
    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", _FakeModel)
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeModel)

    ant = M.build_model("ant")
    assert isinstance(ant, _FakeModel)
    assert ant.kw["model"] == "claude-x"
    assert ant.kw["api_key"] == "k-ant"

    compat = M.build_model("compat")
    assert isinstance(compat, _FakeModel)
    assert "use_responses_api" not in compat.kw  # completions path
    assert compat.kw["extra_body"] == {"enable_thinking": True}

    resp = M.build_model("resp")
    assert isinstance(resp, _FakeModel)
    # decision Q6/P2: openai-responses → ChatOpenAI(use_responses_api=True)
    assert resp.kw["use_responses_api"] is True
    assert resp.kw["base_url"] == "https://api.openai.com/v1"
    assert resp.kw["openai_organization"] == "org-1"


def test_bridge_responses_reasoning():
    import ginno_runtime.models as M

    class Msg:
        def __init__(self):
            self.additional_kwargs = {}

    class Chunk:
        def __init__(self, content):
            self.message = Msg()
            self.message.content = content

    chunk = Chunk(
        [
            {
                "type": "reasoning",
                "summary": [{"index": 0, "type": "summary_text", "text": "思考过程"}],
            },
            {"type": "text", "text": "答案"},
        ]
    )
    out = M.bridge_responses_reasoning(chunk)
    assert out.message.additional_kwargs["reasoning_content"] == "思考过程"

    # non-reasoning chunks pass through untouched
    plain = Chunk([{"type": "text", "text": "hi"}])
    out = M.bridge_responses_reasoning(plain)
    assert "reasoning_content" not in out.message.additional_kwargs
    # non-list content must not blow up
    assert M.bridge_responses_reasoning(Chunk("plain text")) is not None


# --------------------------- build_model_by_name --------------------------- #


def test_build_model_by_name_three_tier(monkeypatch):
    M = _patch_providers(
        monkeypatch,
        {
            "relay": {
                "enabled": True,
                "protocol": "openai-compatible",
                "models": ["qwen-plus", "deepseek-v3"],
                "default_model": "qwen-plus",
            },
            "anthropic": {
                "enabled": True,
                "protocol": "anthropic",
                "models": ["claude-x"],
                "default_model": "claude-x",
            },
            "disabled": {
                "enabled": False,
                "protocol": "openai-compatible",
                "models": ["zombie-model"],
                "default_model": "zombie-model",
            },
        },
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        M, "build_model", lambda pid, model_name=None, **k: calls.append((pid, model_name)) or "MODEL"
    )
    monkeypatch.setattr(M.prov_mod, "get_default_provider", lambda: "anthropic")

    # ① enabled + name ∈ models[]
    M.build_model_by_name("deepseek-v3")
    assert calls[-1] == ("relay", "deepseek-v3")
    # ① also matches the config's default model when models[] is somehow absent
    M.build_model_by_name("qwen-plus")
    assert calls[-1] == ("relay", "qwen-plus")
    # disabled configs never match by model membership
    M.build_model_by_name("zombie-model")
    assert calls[-1] == ("anthropic", "zombie-model")
    # ② exact id hit
    M.build_model_by_name("anthropic")
    assert calls[-1] == ("anthropic", None)
    # ③ default config + name as override
    M.build_model_by_name("mystery-model")
    assert calls[-1] == ("anthropic", "mystery-model")


# ------------------------------ deletion guard ----------------------------- #


def test_delete_referenced_config_blocked_with_references(client):
    cur = client.get("/api/model_configs").json()["configs"]
    client.put(
        "/api/model_configs",
        json={"configs": cur + [_relay("relay-x"), _relay("relay-y")], "default_config": "relay-y"},
    )
    # an agent points at relay-x
    r = client.post(
        "/api/agents", json={"id": "bound", "name": "B", "provider": "relay-x", "model": "m1"}
    )
    assert r.status_code == 200

    # removal via PUT: blocked, with the agent in the reference list
    r = client.put(
        "/api/model_configs",
        json={"configs": cur + [_relay("relay-y")], "default_config": "relay-y"},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["references"]["relay-x"]["agents"][0]["id"] == "bound"

    # removal via DELETE: same guard
    r = client.delete("/api/model_configs/relay-x")
    assert r.status_code == 400
    assert r.json()["references"]["relay-x"]["agents"]

    # once the reference is gone, deletion works
    assert client.delete("/api/agents/bound").json()["ok"] is True
    r = client.delete("/api/model_configs/relay-x")
    assert r.status_code == 200 and r.json()["ok"] is True
    ids = {c["id"] for c in client.get("/api/model_configs").json()["configs"]}
    assert "relay-x" not in ids


def test_delete_default_config_blocked_unless_default_moves(client):
    cur = client.get("/api/model_configs").json()["configs"]
    client.put(
        "/api/model_configs",
        json={"configs": cur + [_relay("relay-x"), _relay("relay-y")], "default_config": "relay-y"},
    )
    # default_config references relay-y → plain PUT dropping it is blocked
    r = client.put("/api/model_configs", json={"configs": cur + [_relay("relay-x")]})
    assert r.status_code == 400
    assert r.json()["references"]["relay-y"]["is_default"] is True
    # ...but allowed when the request moves the default at the same time
    r = client.put(
        "/api/model_configs",
        json={"configs": cur + [_relay("relay-x")], "default_config": "relay-x"},
    )
    assert r.status_code == 200


# ------------------------ agent binding (decision Q3) ---------------------- #


def test_agent_model_binding_hard_constraint(client):
    cur = client.get("/api/model_configs").json()["configs"]
    client.put(
        "/api/model_configs",
        json={
            "configs": cur + [_relay("relay-a", models=["m1", "m2"])],
            "default_config": "relay-a",
        },
    )
    # model outside models[] → HTTP 400 listing the allowed values
    r = client.post(
        "/api/agents",
        json={"id": "tester", "name": "T", "provider": "relay-a", "model": "m3"},
    )
    assert r.status_code == 400
    assert "m1" in r.json()["detail"] and "m2" in r.json()["detail"]
    # member model → ok
    r = client.post(
        "/api/agents",
        json={"id": "tester", "name": "T", "provider": "relay-a", "model": "m2"},
    )
    assert r.status_code == 200
    # update onto a non-member is rejected too
    r = client.put("/api/agents/tester", json={"model": "m3"})
    assert r.status_code == 400
    # unknown provider id stays unrestricted (legacy/deleted — edit page flags it)
    r = client.put("/api/agents/tester", json={"provider": "ghost", "model": "anything"})
    assert r.status_code == 200


def test_agent_model_binding_runtime_fallback(monkeypatch):
    """build failure at runtime degrades to the default config, no crash."""
    import ginno_runtime.api.sessions as S

    provs = {
        "dead": {"id": "dead", "enabled": False, "protocol": "openai-compatible"},
        "alive": {
            "id": "alive",
            "enabled": True,
            "protocol": "openai-compatible",
            "default_model": "m-alive",
        },
    }
    monkeypatch.setattr(S.prov_mod, "load_providers", lambda: provs)
    monkeypatch.setattr(S.prov_mod, "get_default_provider", lambda: "alive")
    calls: list[tuple] = []

    def fake_build(pid, model_name=None, enable_search=None):
        if pid == "dead":
            raise ValueError("provider dead is disabled")
        calls.append((pid, model_name))
        return "MODEL"

    monkeypatch.setattr(S, "build_model", fake_build)

    assert S._build_model_with_fallback("dead", "m1") == "MODEL"
    assert calls[-1] == ("alive", "m-alive")


def test_agent_model_binding_runtime_fallback_no_loop(monkeypatch):
    """when the default pair itself fails to build, the error re-raises."""
    import ginno_runtime.api.sessions as S

    provs = {
        "alive": {
            "id": "alive",
            "enabled": True,
            "protocol": "openai-compatible",
            "default_model": "m-alive",
        },
    }
    monkeypatch.setattr(S.prov_mod, "load_providers", lambda: provs)
    monkeypatch.setattr(S.prov_mod, "get_default_provider", lambda: "alive")

    def broken_build(pid, model_name=None, enable_search=None):
        raise ValueError("everything broken")

    monkeypatch.setattr(S, "build_model", broken_build)
    # the fallback target IS the failing pair → re-raise instead of looping
    with pytest.raises(ValueError):
        S._build_model_with_fallback("alive", "m-alive")

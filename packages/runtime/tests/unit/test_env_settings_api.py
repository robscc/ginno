"""env 专用 GET/PATCH 每键 CAS API + settings 完整性 API 的行为契约。

settings-integrity（2026-11-22）验收面：
- GET /api/settings/env：普通键回明文；敏感键（键名模式 ∪ 带 userinfo 的
  URL = 认证连接串）只回元数据 + version token（随机 opaque，非 hash）；
- PATCH 全事务 CAS：版本冲突 409 且全部不落盘；同键重复 400；不同键合并；
- version=null 创建语义；remove 语义；
- settings GET/PUT 掩码往返兼容（含值级 DSN 掩码）；
- 损坏：GET 带状态、GET /api/settings 带 parseError、PUT/PATCH 拒 409 且
  原字节逐字节保留；备份端点非破坏；
- 损坏启动安全模式：bypass fail-closed、hooks 不执行、权限默认 ask。
"""

from __future__ import annotations

import json as stdjson

import pytest
from fastapi.testclient import TestClient

from ginno_runtime import paths

pytestmark = pytest.mark.api

AUTH_DSN = "postgres://scott:tiger@db.internal:5432/appdb"
PLAIN_URL = "http://127.0.0.1:7890"


def _settings_path():
    return paths.settings_path()


def _raw():
    return _settings_path().read_bytes()


def _write_raw(text: str) -> None:
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _put_env(client: TestClient, updates: list[dict], expect: int = 200):
    r = client.patch("/api/settings/env", json={"updates": updates})
    assert r.status_code == expect, r.text
    return r


def _get_env(client: TestClient) -> dict:
    r = client.get("/api/settings/env")
    assert r.status_code == 200, r.text
    return r.json()


# ---- GET 视图：秘密不泄漏 ----------------------------------------------------


def test_get_plain_value_visible_secret_metadata_only(client):
    _put_env(client, [
        {"key": "HTTP_PROXY", "version": None, "value": PLAIN_URL},
        {"key": "MY_API_TOKEN", "version": None, "value": "tok-abcdef123456"},
        {"key": "DATABASE_URL", "version": None, "value": AUTH_DSN},
    ])
    state = _get_env(client)
    entries = state["entries"]

    assert entries["HTTP_PROXY"]["sensitive"] is False
    assert entries["HTTP_PROXY"]["value"] == PLAIN_URL
    assert entries["HTTP_PROXY"]["version"]

    # 键名命中：只有元数据
    tok = entries["MY_API_TOKEN"]
    assert tok["sensitive"] is True
    assert "value" not in tok
    assert tok["set"] is True
    assert tok["kind"] == "secret-name"

    # 认证连接串（键名不含 SECRET/TOKEN）：值形态命中
    dsn = entries["DATABASE_URL"]
    assert dsn["sensitive"] is True
    assert "value" not in dsn
    assert dsn["kind"] == "url-with-auth"
    assert dsn["url"]["scheme"] == "postgres"
    assert dsn["url"]["host"] == "db.internal"
    assert dsn["url"]["has_credentials"] is True
    # 凭据不出现（不打印值——响应体全文不含秘密）
    body = stdjson.dumps(state)
    assert "tiger" not in body
    assert "tok-abcdef123456" not in body


def test_get_url_without_credentials_is_plain(client):
    _put_env(client, [{"key": "DATABASE_URL", "version": None,
                       "value": "postgres://db.internal:5432/appdb"}])
    e = _get_env(client)["entries"]["DATABASE_URL"]
    assert e["sensitive"] is False
    assert e["value"] == "postgres://db.internal:5432/appdb"


def test_version_token_is_opaque_not_value_hash(client):
    """token 与值内容无关：同值两次写入产生不同 token（每次写重新随机）。"""
    _put_env(client, [{"key": "K", "version": None, "value": "same"}])
    v1 = _get_env(client)["entries"]["K"]["version"]
    _put_env(client, [{"key": "K", "version": v1, "value": "same"}])
    v2 = _get_env(client)["entries"]["K"]["version"]
    assert v1 != v2


# ---- CAS 语义 ---------------------------------------------------------------


def test_cas_stale_version_409_nothing_applied(client):
    _put_env(client, [
        {"key": "A", "version": None, "value": "1"},
        {"key": "B", "version": None, "value": "1"},
    ])
    va = _get_env(client)["entries"]["A"]["version"]
    # 先改 A（token 轮换）
    _put_env(client, [{"key": "A", "version": va, "value": "2"}])
    stale = _get_env(client)["entries"]["A"]["version"]
    # 用旧 token 改 A、用新 token 改 B → 409 全拒绝
    r = _put_env(client, [
        {"key": "A", "version": va, "value": "3"},
        {"key": "B", "version": stale, "value": "2"},
    ], expect=409)
    body = r.json()
    assert body["error"] == "version_conflict"
    assert body["conflicts"][0]["key"] == "A"
    # B 未被应用（全事务）
    assert _get_env(client)["entries"]["B"]["value"] == "1"
    assert _get_env(client)["entries"]["A"]["value"] == "2"


def test_different_keys_merge(client):
    """A 键无冲突即接受——与其它键的并发写合并，不要求整块版本。"""
    _put_env(client, [
        {"key": "A", "version": None, "value": "1"},
        {"key": "B", "version": None, "value": "1"},
    ])
    va = _get_env(client)["entries"]["A"]["version"]
    # 两个键各自的当前 token 一起提交 → 都成功
    vb = _get_env(client)["entries"]["B"]["version"]
    _put_env(client, [
        {"key": "A", "version": va, "value": "2"},
        {"key": "B", "version": vb, "remove": True},
    ])
    entries = _get_env(client)["entries"]
    assert entries["A"]["value"] == "2"
    assert "B" not in entries


def test_duplicate_key_in_one_request_rejected(client):
    r = _put_env(client, [
        {"key": "X", "version": None, "value": "1"},
        {"key": "X", "version": None, "value": "2"},
    ], expect=400)
    assert "duplicate" in r.json()["detail"]
    assert "X" not in _get_env(client)["entries"]


def test_version_null_requires_key_absent(client):
    _put_env(client, [{"key": "EXISTS", "version": None, "value": "1"}])
    r = _put_env(client, [{"key": "EXISTS", "version": None, "value": "2"}], expect=409)
    # 冲突响应带 current_version 供重试
    assert r.json()["conflicts"][0]["current_version"]
    assert _get_env(client)["entries"]["EXISTS"]["value"] == "1"


def test_remove_with_matching_version(client):
    _put_env(client, [{"key": "GONE", "version": None, "value": "x"}])
    v = _get_env(client)["entries"]["GONE"]["version"]
    _put_env(client, [{"key": "GONE", "version": v, "remove": True}])
    assert "GONE" not in _get_env(client)["entries"]
    # 磁盘上的 env 块也删除了该键
    doc = stdjson.loads(_raw())
    assert "GONE" not in (doc.get("env") or {})


def test_bad_payload_shapes(client):
    # 非 list
    r = client.patch("/api/settings/env", json={"updates": "nope"})
    assert r.status_code == 400
    # 值类型非法
    r = _put_env(client, [{"key": "N", "version": None, "value": {"nested": 1}}],
                 expect=400)
    # remove 带 value
    r = _put_env(client, [{"key": "N", "version": None, "remove": True, "value": "x"}],
                 expect=400)
    # 坏键名
    r = _put_env(client, [{"key": "A=B", "version": None, "value": "x"}], expect=400)
    # 空 updates
    r = _put_env(client, [], expect=400)


def test_env_injection_still_consumes_written_values(client):
    """写进去的值必须能被 env_inject 消费（类型保持 str/num/bool）。"""
    from ginno_runtime.tools import env_inject

    _put_env(client, [
        {"key": "STR", "version": None, "value": "s"},
        {"key": "NUM", "version": None, "value": 42},
        {"key": "FLAG", "version": None, "value": True},
    ])
    extra = env_inject.load_extra_env()
    assert extra["STR"] == "s"
    assert extra["NUM"] == "42"
    assert extra["FLAG"] == "true"


# ---- 整文档 PUT 的 CAS 交互 --------------------------------------------------


def test_full_document_put_preserves_untouched_tokens(client):
    """PUT /api/settings 改 language（不动 env）→ env token 不失效。"""
    _put_env(client, [{"key": "A", "version": None, "value": "1"}])
    v = _get_env(client)["entries"]["A"]["version"]
    doc = client.get("/api/settings").json()
    doc["language"] = "zh-CN"
    assert client.put("/api/settings", json=doc).status_code == 200
    # token 保留 → 旧编辑器仍可 CAS
    assert _get_env(client)["entries"]["A"]["version"] == v
    _put_env(client, [{"key": "A", "version": v, "value": "2"}])
    assert _get_env(client)["entries"]["A"]["value"] == "2"


def test_full_document_put_removes_dropped_key_token(client):
    _put_env(client, [{"key": "A", "version": None, "value": "1"}])
    doc = client.get("/api/settings").json()
    doc["env"] = {"B": "2"}  # A 被整文档删除
    assert client.put("/api/settings", json=doc).status_code == 200
    entries = _get_env(client)["entries"]
    assert "A" not in entries
    assert entries["B"]["value"] == "2"
    stored = stdjson.loads(_raw()).get("env_versions", {})
    assert "A" not in stored and "B" in stored


# ---- settings GET/PUT 掩码往返（兼容）----------------------------------------


def test_settings_get_masks_auth_dsn_put_round_trips(client):
    _put_env(client, [
        {"key": "DATABASE_URL", "version": None, "value": AUTH_DSN},
        {"key": "MY_API_TOKEN", "version": None, "value": "tok-abcdef123456"},
    ])
    doc = client.get("/api/settings").json()
    # 掩码呈现：认证连接串与键名敏感值都不明文
    assert "tiger" not in stdjson.dumps(doc)
    assert "tok-abcdef123456" not in stdjson.dumps(doc)
    assert doc["env"]["DATABASE_URL"].startswith("••••")
    assert doc["env"]["MY_API_TOKEN"].startswith("••••")
    # 往返：掩码原样 PUT → 存储还原为真实值（不落掩码）
    doc["language"] = "en"
    assert client.put("/api/settings", json=doc).status_code == 200
    stored = stdjson.loads(_raw())
    assert stored["env"]["DATABASE_URL"] == AUTH_DSN
    assert stored["env"]["MY_API_TOKEN"] == "tok-abcdef123456"


def test_settings_get_plain_env_value_visible(client):
    _put_env(client, [{"key": "HTTP_PROXY", "version": None, "value": PLAIN_URL}])
    doc = client.get("/api/settings").json()
    assert doc["env"]["HTTP_PROXY"] == PLAIN_URL


# ---- 损坏：拒写 / 保留原字节 / 安全模式 --------------------------------------


CORRUPT = '{"providers": {"custom": {"api_key": "sk-live-1234"'


def test_corrupt_get_settings_reports_parse_error(client):
    _write_raw(CORRUPT)
    doc = client.get("/api/settings").json()
    assert "parseError" in doc
    # 与「不存在」区分：不存在时是 {}
    assert doc != {}


def test_corrupt_get_env_reports_status(client):
    _write_raw(CORRUPT)
    state = _get_env(client)
    assert state["status"] == "corrupt"
    assert state["parseError"]
    assert state["entries"] == {}


def test_corrupt_health_endpoint(client):
    _write_raw(CORRUPT)
    h = client.get("/api/settings/health").json()
    assert h["status"] == "corrupt"
    assert h["error"]


def test_corrupt_put_settings_refused_bytes_preserved(client):
    _write_raw(CORRUPT)
    before = _raw()
    r = client.put("/api/settings", json={"language": "en"})
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "settings_corrupt"
    assert body["recovery"]["backup"] == "POST /api/settings/backup"
    assert _raw() == before


def test_corrupt_patch_env_refused_bytes_preserved(client):
    _write_raw(CORRUPT)
    before = _raw()
    r = _put_env(client, [{"key": "A", "version": None, "value": "1"}], expect=409)
    assert r.json()["error"] == "settings_corrupt"
    assert _raw() == before


def test_corrupt_backup_non_destructive_then_explicit_fix(client):
    _write_raw(CORRUPT)
    r = client.post("/api/settings/backup")
    assert r.status_code == 200
    backup_path = r.json()["path"]
    assert open(backup_path, "rb").read() == _raw()
    # 普通 PUT 仍拒（防盲目 round-trip）
    assert client.put("/api/settings", json={"language": "zh-CN"}).status_code == 409
    # 显式修复通道：用户确认后 PUT /api/settings/repair → 自动备份 + 落盘
    r = client.put("/api/settings/repair", json={"language": "zh-CN"})
    assert r.status_code == 200
    assert r.json()["restart_required"] is True
    assert r.json()["backup"]  # pre-repair 备份已生成
    assert open(r.json()["backup"], "rb").read() == CORRUPT.encode()
    assert stdjson.loads(_raw()) == {"language": "zh-CN"}
    assert client.get("/api/settings/health").json()["status"] == "ok"


def test_corrupt_bypass_permissions_fail_closed(client):
    from ginno_runtime.permission.policy import is_bypass_permissions

    _write_raw(CORRUPT)
    assert is_bypass_permissions() is False
    # 对照：missing/正常文档的默认语义不变
    _write_raw("{}")
    assert is_bypass_permissions() is True


def test_corrupt_hooks_dispatcher_empty(client):
    from ginno_runtime.hooks.dispatcher import HookDispatcher

    _write_raw(CORRUPT)
    d = HookDispatcher.from_settings()
    assert d.settings == {}
    hooks = d._hooks_for("PreToolUse", "bash")
    assert hooks == []


def test_corrupt_permission_policy_defaults_to_ask(client):
    from ginno_runtime.permission.policy import PermissionPolicy

    _write_raw(CORRUPT)
    policy = PermissionPolicy.from_settings()
    assert policy.decide("bash", "rm -rf /") == "ask"
    assert policy.decide("write_file", "/etc/passwd") == "ask"


def test_corrupt_permission_migration_refuses_to_write(client):
    from ginno_runtime.permission.policy import ensure_web_permissions

    _write_raw(CORRUPT)
    before = _raw()
    ensure_web_permissions()  # 不 raise、不写
    assert _raw() == before


def test_corrupt_env_injection_disabled(client):
    from ginno_runtime.tools import env_inject

    _put_env(client, [{"key": "HTTP_PROXY", "version": None, "value": PLAIN_URL}])
    assert env_inject.load_extra_env() == {"HTTP_PROXY": PLAIN_URL}
    _write_raw(CORRUPT)
    assert env_inject.load_extra_env() == {}


# ---- 写失败路径 -------------------------------------------------------------


def test_patch_env_write_failure_returns_500_and_keeps_file(client, monkeypatch):
    import os

    _put_env(client, [{"key": "A", "version": None, "value": "1"}])
    before = _raw()

    def _boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", _boom)
    v = _get_env(client)["entries"]["A"]["version"]
    r = client.patch("/api/settings/env",
                     json={"updates": [{"key": "A", "version": v, "value": "2"}]})
    assert r.status_code == 500
    assert _raw() == before


def test_put_settings_write_failure_500(client, monkeypatch):
    import os

    client.put("/api/settings", json={"language": "en"})
    before = _raw()

    def _boom(src, dst):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(os, "replace", _boom)
    r = client.put("/api/settings", json={"language": "zh-CN"})
    assert r.status_code == 500
    assert _raw() == before


# ---- 并发（API 层压力）------------------------------------------------------


def test_concurrent_patch_different_keys_all_land(client):
    import threading

    for k in ("K0", "K1", "K2", "K3"):
        _put_env(client, [{"key": k, "version": None, "value": "0"}])
    versions = {k: _get_env(client)["entries"][k]["version"] for k in ("K0", "K1", "K2", "K3")}
    results: dict[str, int] = {}
    lock = threading.Lock()

    def _bump(k: str):
        r = client.patch(
            "/api/settings/env",
            json={"updates": [{"key": k, "version": versions[k], "value": "1"}]},
        )
        with lock:
            results[k] = r.status_code

    threads = [threading.Thread(target=_bump, args=(k,)) for k in versions]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == [200, 200, 200, 200]
    entries = _get_env(client)["entries"]
    assert all(entries[k]["value"] == "1" for k in versions)

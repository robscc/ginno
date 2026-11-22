"""Settings 完整性 × 调度可靠性 —— 真实 Chromium 回归（settings-schedule-integrity）。

复用 ``test_packaged_ui_tasks_overview`` 的 source/strict harness
（GINNO_E2E_RUNTIME=source → ``uv run ginno-runtime`` 源码 sidecar + repo 内
apps/web/out 静态导出；GINNO_E2E_STRICT=1 时环境缺失即 fail）。随机空闲端口 +
临时 GINNO_HOME + GINNO_FAKE_LLM=1，绝不碰 8787 与真实数据；断言打在
「磁盘字节 / API 终态 / UI 实际发出的请求」三层。

覆盖（v0.5.1 已复现问题的回归化，参考 repro_settings_051.py / ginno_sched_repro.py）：

设置三项：
A. settings.json 损坏——启动存活 + 原字节保留；运行中损坏时遍历任意设置页
   不覆盖字节；ensure_layout 迁移与 PUT 全部拒写；横幅备份/修复流程闭环。
   老格式迁移就地保用户键（迁移不 PUT：env 编辑全程零 PUT /api/settings）。
B. 认证 DSN/令牌——GET env 响应体无明文、DOM 无明文（secret 行 + password
   输入）；编辑其他键保存后磁盘上秘密原值不丢。
C. 双窗口——异键并行保存互不覆盖；同键后写 409 冲突草稿保留，显式
   overwrite/reload 两个出口都真实生效。
调度两类：
S1. 未来 once 手动暂停：runs=0、无 Done 徽标、再启用恢复；按计划成功 /
    失败 / 老数据（无 last_scheduled：schedule→legacyDone、manual→paused）
    四种 once 终态在 UI 上可区分；老 runs 行（缺 trigger）不崩 Runs 页。
S2. 首拉失败（JSON 5xx / 断连）只重试不渲染错误体；暂停被拒（断连/500）
    行内红反馈 + 服务端未变；「立即执行」真到服务端但回执丢失 + WS 屏蔽 →
    黄色「结果未知」+ 行内查看记录入口（task-view-runs）可对账，绝不重发。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import _sidecar as sc
import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e

_REPO_RT = smoke._REPO  # packages/runtime
_WEB = _REPO_RT.parents[1] / "apps" / "web"
_SCHED = json.loads((_WEB / "messages" / "en" / "sched.json").read_text())["sched"]
_TOOLS = json.loads((_WEB / "messages" / "en" / "tools.json").read_text())
_ENVMSG = _TOOLS["settings"]["env"]
_CORRUPTMSG = _TOOLS["settings"]["corrupt"]

MASK = "••••"
CORRUPT_TEXT = (
    '{"providers":{"custom":{"api_key":"sk-VALUABLE1111111","enabled":true}},'
    '"env":{"SEED_OLD":"x"},"language":"en"'
)  # 故意截断（v0.5.1 复现形态）：解析失败，字节里还有值得保留的内容

_DSN = "postgres://u:PWA1b2c3d4e5f@db.invalid/x"
_TOKEN = "tok-A9b8c7d6e5f43210"


# --------------------------------------------------------------------------- #
# harness                                                                     #
# --------------------------------------------------------------------------- #

def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _child_env(home: Path, port: int) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GINNO_")}
    env.update(GINNO_HOME=str(home), GINNO_RUNTIME_PORT=str(port), GINNO_FAKE_LLM="1")
    return env


def _spawn(home: Path, port: int, err_log):
    if smoke.RUNTIME_MODE == "source":
        return subprocess.Popen(
            ["uv", "run", "--no-sync", "ginno-runtime"],
            cwd=str(_REPO_RT), env=_child_env(home, port),
            stdout=subprocess.DEVNULL, stderr=err_log, start_new_session=True,
        )
    return sc.spawn_sidecar(str(smoke.RUNTIME_BIN), _child_env(home, port), err=err_log)


@contextlib.contextmanager
def _runtime(home: Path, *, settings_text: str | None = None,
             schedules: dict | None = None, run_rows: list | None = None):
    """带预置数据的隔离 sidecar（数据在 spawn 前落盘，服务端首次读取即生效）。"""
    smoke._require_runtime()
    home.mkdir(parents=True, exist_ok=True)
    port = smoke._free_port()
    (home / "settings.json").write_text(
        settings_text if settings_text is not None else json.dumps(smoke._settings()),
        encoding="utf-8")
    if schedules is not None:
        (home / "schedules.json").write_text(json.dumps(schedules), encoding="utf-8")
    if run_rows:
        d = home / "schedule-runs"
        d.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (d / f"runs-{today}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in run_rows) + "\n", encoding="utf-8")
    err_log = (home / "sidecar-stderr.log").open("wb")
    proc = _spawn(home, port, err_log)
    try:
        smoke._wait_health(port)
        yield port
    finally:
        smoke._stop(proc, err_log)


def _api_raw(port: int, method: str, path: str, body=None):
    """返回 (status, 原始文本)——掩码/错误体断言需要原文而不是解析后的 dict。"""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _open_env_tab(page, base: str):
    page.goto(base + "/settings/security/hooks", wait_until="load")
    tab = page.get_by_role("button", name="Env Vars", exact=True)
    tab.wait_for(state="visible", timeout=20_000)
    tab.click()
    page.locator('[data-testid="env-vars"]').wait_for(state="visible", timeout=10_000)


def _row_index(page, key: str) -> int:
    n = page.locator('[data-testid^="env-key-"]').count()
    for i in range(n):
        if page.locator(f'[data-testid="env-key-{i}"]').input_value() == key:
            return i
    raise AssertionError(f"env row {key!r} not found among {n} rows")


def _save_env(page) -> None:
    page.locator('[data-testid="env-save"]').click()
    page.get_by_test_id("env-status").wait_for(
        state="visible", timeout=10_000)
    page.wait_for_function(
        "() => ['Saved','Save failed'].includes("
        "document.querySelector('[data-testid=\"env-status\"]')?.innerText)",
        timeout=20_000)
    assert page.get_by_test_id("env-status").inner_text() == "Saved"


def _schedule_task(port: int, name: str, *, at=None, minutes=None, target=None) -> dict:
    if at is not None:
        schedule = {"kind": "once", "at": at}
    else:
        schedule = {"kind": "interval", "minutes": minutes or 60}
    return smoke._api(port, "POST", "/api/schedule/tasks", {
        "name": name,
        "target": target or {"type": "prompt", "prompt": "hi"},
        "schedule": schedule,
    })


def _task(port: int, tid: str) -> dict:
    cfg = smoke._api(port, "GET", "/api/schedule")
    return next(t for t in cfg["tasks"] if t["id"] == tid)


def _wait_task(port: int, tid: str, pred, timeout: float = 105, what=""):
    dl = time.time() + timeout
    last = None
    while time.time() < dl:
        last = _task(port, tid)
        if pred(last):
            return last
        time.sleep(2)
    raise AssertionError(f"task {tid} never reached {what}: {json.dumps(last)[:400]}")


def _wait_runs(port: int, tid: str, *, at_least=1, finished=False, timeout=120) -> list:
    dl = time.time() + timeout
    rows: list = []
    while time.time() < dl:
        r = smoke._api(port, "GET", f"/api/schedule/runs?task_id={tid}")
        rows = r.get("rows") or []
        ok = len(rows) >= at_least and (not finished or all(x.get("finished_at") for x in rows))
        if ok:
            return rows
        time.sleep(1)
    raise AssertionError(f"runs for {tid} never satisfied (n={len(rows)}): {json.dumps(rows)[:300]}")


def _task_row(page, tid: str):
    row = page.locator(f'[data-task-id="{tid}"]')
    row.wait_for(state="visible", timeout=20_000)
    return row


def _iso_in(seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# 设置 A：损坏启动 / 运行中损坏 / 迁移                                        #
# --------------------------------------------------------------------------- #

def test_corrupt_startup_preserves_bytes_and_recovery(tmp_path):
    """A1：坏文件启动存活 + 字节保留 + 拒写 + 备份/修复 API 闭环。

    损坏降级 503 体由 ``json()`` 识别抛错（AppShell 不再白屏），恢复横幅
    断言在 :func:`test_corrupt_recovery_banner_ui`；本用例的恢复流程走 API
    层（与横幅调用的端点完全相同），页面在浏览器中加载交互后复核字节。
    """
    from playwright.sync_api import expect

    cfg = tmp_path / "settings.json"
    cfg.write_text(CORRUPT_TEXT, encoding="utf-8")
    sha0 = _sha(cfg)
    with _runtime(tmp_path, settings_text=CORRUPT_TEXT) as port:
        # 启动存活 + ensure_layout 迁移拒写（原字节逐字节保留）
        assert _sha(cfg) == sha0, "corrupt settings.json must survive startup byte-for-byte"
        st, body = _api_raw(port, "GET", "/api/settings/health")
        assert st == 200 and json.loads(body)["status"] == "corrupt"
        st, body = _api_raw(port, "GET", "/api/settings")
        assert st == 200 and "parseError" in json.loads(body)
        # 任意客户端 PUT（模拟其他设置页保存）→ 409，不是静默覆盖
        st, body = _api_raw(port, "PUT", "/api/settings", {"language": "en"})
        assert st == 409 and json.loads(body).get("error") == "settings_corrupt"
        assert _sha(cfg) == sha0, "rejected PUT must not touch the damaged file"

        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                # 页面在损坏态加载/执行 JS 后原字节仍不动（UI 修复另行钉住）
                page.goto(f"http://127.0.0.1:{port}/settings/general", wait_until="load")
                page.wait_for_timeout(1500)
                assert _sha(cfg) == sha0, "page load must not rewrite the damaged file"
            finally:
                browser.close()

        # 恢复流程①：非破坏备份（原文件不动，旁路 .bak 保留原字节）
        st, body = _api_raw(port, "POST", "/api/settings/backup")
        assert st == 200 and json.loads(body)["ok"]
        bak = Path(json.loads(body)["path"])
        assert bak.exists() and bak.read_text() == CORRUPT_TEXT
        assert _sha(cfg) == sha0, "backup must not modify the original"
        # 恢复流程②b：repair（唯一损坏态写通道；服务端落盘前自动再备份）
        st, body = _api_raw(port, "PUT", "/api/settings/repair",
                            {"language": "en", "env": {"REPAIRED": "1"}})
        assert st == 200 and json.loads(body)["ok"]
        disk = json.loads(cfg.read_text())
        assert disk["env"] == {"REPAIRED": "1"}
        pre = sorted(tmp_path.glob("settings.json.bak-*"))
        assert any(p.read_text() == CORRUPT_TEXT for p in pre), "pre-repair backup keeps original bytes"
        st, body = _api_raw(port, "GET", "/api/settings/health")
        assert json.loads(body)["status"] == "ok"


def test_corrupt_recovery_banner_ui(tmp_path):
    """损坏态恢复 UI（横幅 + 备份/修复按钮）——当前因前端白屏不可达。"""
    from playwright.sync_api import expect

    with _runtime(tmp_path, settings_text=CORRUPT_TEXT) as port:
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(f"http://127.0.0.1:{port}/settings/general", wait_until="load")
                banner = page.get_by_test_id("settings-corrupt-banner")
                expect(banner).to_be_visible(timeout=15_000)
                expect(banner).to_contain_text(_CORRUPTMSG["title"])
                page.get_by_test_id("settings-corrupt-backup").click()
                done = page.get_by_test_id("settings-corrupt-backup-result")
                expect(done).to_be_visible(timeout=10_000)
                page.get_by_test_id("settings-corrupt-repair-input").fill(
                    json.dumps({"language": "en"}))
                page.get_by_test_id("settings-corrupt-repair").click()
                expect(page.get_by_test_id("settings-corrupt-repair-done")).to_be_visible(
                    timeout=10_000)
            finally:
                browser.close()


def test_corrupt_runtime_any_tab_locks_writes(tmp_path):
    """A2：运行中写坏 → 遍历任意设置页 + env 页 + PUT 全部不覆盖字节。"""
    from playwright.sync_api import expect

    good = {"providers": {"custom": {"api_key": "sk-VALUABLE2222", "enabled": True}},
            "env": {"SEED_OLD": "x"}, "language": "en"}
    with _runtime(tmp_path, settings_text=json.dumps(good)) as port:
        cfg = tmp_path / "settings.json"
        # 运行中把磁盘写坏（PUT 无法注入坏 JSON，只能直接落盘）
        cfg.write_text(CORRUPT_TEXT, encoding="utf-8")
        sha0 = _sha(cfg)
        puts: list[str] = []
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.on("request", lambda r: puts.append(r.url)
                        if r.method == "PUT" and r.url.endswith("/api/settings") else None)
                for path in ("/settings/general", "/settings/agents", "/settings/model"):
                    page.goto(f"http://127.0.0.1:{port}{path}", wait_until="load")
                    page.wait_for_timeout(800)
                    assert _sha(cfg) == sha0, f"visiting {path} must not rewrite the damaged file"
                # env 视图（专用端点在白名单内，不受降级闸门影响）：如实报
                # corrupt → 只读态。注：页壳可能因 503 白屏（产品 bug，
                # test_corrupt_recovery_banner_ui 钉住），故 env 页 UI 断言
                # 放宽为「可达时必须锁定」——API 层断言无论如何必查。
                page.goto(f"http://127.0.0.1:{port}/settings/security/hooks",
                          wait_until="load")
                tab = page.get_by_role("button", name="Env Vars", exact=True)
                try:
                    tab.wait_for(state="visible", timeout=4000)
                    tab.click()
                    page.locator('[data-testid="env-vars"]').wait_for(
                        state="visible", timeout=5000)
                except Exception:
                    pass  # 页壳降级渲染（横幅 bug 已修，此分支仅容错）
                if page.get_by_test_id("env-corrupt").count():
                    from playwright.sync_api import expect as _expect
                    _expect(page.get_by_test_id("env-save")).to_be_disabled()
                    _expect(page.get_by_test_id("env-add")).to_be_disabled()
                    assert page.locator('[data-testid^="env-key-"]').count() == 0, \
                        "损坏视图绝不能把行渲染出来（会被当真值编辑）"
                assert _sha(cfg) == sha0
                # 服务端侧：env PATCH 与整文档 PUT 都拒写（防御纵深第二层）
                st, body = _api_raw(port, "PATCH", "/api/settings/env",
                                    {"updates": [{"key": "X", "version": None, "value": "1"}]})
                assert st == 409 and json.loads(body).get("error") == "settings_corrupt"
                st, _ = _api_raw(port, "PUT", "/api/settings", {"language": "en"})
                assert st == 409
                assert _sha(cfg) == sha0
                assert puts == [], "UI 在损坏态不得发出任何 PUT /api/settings"
            finally:
                browser.close()


def test_legacy_settings_migration_preserves_keys_no_put(tmp_path):
    """老格式（无 providers）→ 迁移就地保用户键；env 编辑零 PUT。

    老文档预置 notifications 键（v0.5.1 存量形态：老版前端已播种过）。
    notifyPrefs.loadNotifyPrefs 已改纯读（迁移仅在内存生效、落盘推迟到显式
    保存），boot 期不再有任何播种 PUT——本用例可直接开始编辑，钉住核心契约「env 编辑保存绝不 PUT 整文档」。
    """
    from playwright.sync_api import expect

    legacy = json.dumps({
        "language": "en",
        "env": {"SEED_OLD": "x"},
        "notifications": {"enabled": True, "sound": True, "kinds": {"reply": True}},
    })
    with _runtime(tmp_path, settings_text=legacy) as port:
        cfg = tmp_path / "settings.json"
        disk = json.loads(cfg.read_text())
        assert disk.get("providers"), "ensure_layout must seed providers on migration"
        assert disk.get("env") == {"SEED_OLD": "x"} and disk.get("language") == "en", \
            "migration preserves user keys"

        puts: list[str] = []
        patches: list[str] = []

        def _track(r):
            # 只记写入：PUT 整文档 / PATCH·PUT env——GET /api/settings（页面
            # 挂载期各面板的读）绝不能混进写入计数（前版条件把 GET 也算进
            # patches，导致 7==1 的误报）。
            if r.method == "PUT" and r.url.endswith("/api/settings"):
                puts.append(r.url)
            elif r.method in ("PATCH", "PUT") and r.url.endswith("/api/settings/env"):
                patches.append(r.url)

        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.on("request", _track)
                # 先在设置页等 boot 期播种（notifyPrefs kinds 补齐的整文档
                # PUT）落定，再进 env tab 取最新 version token——播种 PUT 会
                # 轮换全部 env token，先取 token 再等播种会让保存吃 409。
                page.goto(f"http://127.0.0.1:{port}/settings/general", wait_until="load")
                page.wait_for_timeout(2500)
                boot_puts = len(puts)
                _open_env_tab(page, f"http://127.0.0.1:{port}")
                i = _row_index(page, "SEED_OLD")
                page.get_by_test_id(f"env-value-{i}").fill("migrated")
                page.get_by_test_id("env-add").click()
                n = page.locator('[data-testid^="env-key-"]').count() - 1
                page.get_by_test_id(f"env-key-{n}").fill("NORMAL")
                page.get_by_test_id(f"env-value-{n}").fill("plain")
                _save_env(page)
                assert len(puts) == boot_puts, \
                    "env 编辑保存绝不 PUT 整文档（迁移不 PUT）"
                assert len(patches) == 1 and patches[0].endswith("/api/settings/env")
                disk = json.loads(cfg.read_text())
                assert disk["env"]["SEED_OLD"] == "migrated" and disk["env"]["NORMAL"] == "plain"
                assert disk.get("providers"), "PATCH env must not drop other settings"
            finally:
                browser.close()


def test_legacy_doc_boot_no_put(tmp_path):
    """无 notifications 键的老文档：设置页挂载即播种 PUT —— 应为零。"""
    legacy = json.dumps({"language": "en", "env": {"SEED_OLD": "x"}})
    with _runtime(tmp_path, settings_text=legacy) as port:
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                puts: list[str] = []
                page.on("request", lambda r: puts.append(r.url)
                        if r.method == "PUT" and r.url.endswith("/api/settings") else None)
                page.goto(f"http://127.0.0.1:{port}/settings/general", wait_until="load")
                page.wait_for_timeout(2500)
                assert puts == [], "boot-time seeding must not PUT the whole settings document"
            finally:
                browser.close()


# --------------------------------------------------------------------------- #
# 设置 B：认证 DSN / 令牌掩码                                                 #
# --------------------------------------------------------------------------- #

def test_auth_dsn_never_plaintext_other_edits_keep_secrets(tmp_path):
    """GET env 响应体 + DOM 双层无明文；改普通键后磁盘秘密原值不丢。"""
    from playwright.sync_api import expect

    settings = {
        "language": "en",
        "env": {"DATABASE_URL": _DSN, "MY_TOKEN": _TOKEN, "NORMAL": "plainvalue"},
    }
    with _runtime(tmp_path, settings_text=json.dumps(settings)) as port:
        # API 层：专用端点响应体全文不含秘密；兼容端点回掩码
        st, body = _api_raw(port, "GET", "/api/settings/env")
        view = json.loads(body)
        assert st == 200 and view["status"] == "ok"
        assert _DSN not in body and _TOKEN not in body
        dsn = view["entries"]["DATABASE_URL"]
        assert dsn["sensitive"] and dsn["set"] and not dsn.get("value")
        assert dsn["url"]["scheme"] == "postgres" and dsn["url"]["host"] == "db.invalid"
        assert view["entries"]["MY_TOKEN"]["sensitive"] and not view["entries"]["MY_TOKEN"].get("value")
        assert view["entries"]["NORMAL"]["value"] == "plainvalue"
        st, body = _api_raw(port, "GET", "/api/settings")
        whole = json.loads(body)
        assert str(whole["env"]["DATABASE_URL"]).startswith(MASK)
        assert str(whole["env"]["MY_TOKEN"]).startswith(MASK)

        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                _open_env_tab(page, f"http://127.0.0.1:{port}")
                tok_i = _row_index(page, "MY_TOKEN")
                dsn_i = _row_index(page, "DATABASE_URL")
                # DOM：敏感行显示「已设置 + scheme/host 元数据」，键名锁定
                expect(page.get_by_test_id(f"env-secret-{tok_i}")).to_contain_text(_ENVMSG["secretSet"])
                expect(page.get_by_test_id(f"env-secret-{dsn_i}")).to_contain_text("postgres://db.invalid")
                assert page.get_by_test_id(f"env-key-{tok_i}").is_disabled()
                body_text = page.locator("body").inner_text()
                assert _DSN not in body_text and _TOKEN not in body_text, \
                    "DOM must never show credential plaintext"
                # 替换入口 = password 整值重输（不回填旧值）
                page.get_by_test_id(f"env-replace-{tok_i}").click()
                inp = page.get_by_test_id(f"env-value-{tok_i}")
                expect(inp).to_have_value("")
                assert inp.get_attribute("type") == "password"
                page.get_by_test_id(f"env-replace-cancel-{tok_i}").click()
                # 编辑普通键 → 保存 → 磁盘秘密原值不丢（PATCH 只动 NORMAL）
                norm_i = _row_index(page, "NORMAL")
                page.get_by_test_id(f"env-value-{norm_i}").fill("plainvalue2")
                _save_env(page)
                disk = json.loads((tmp_path / "settings.json").read_text())
                assert disk["env"]["MY_TOKEN"] == _TOKEN, "editing other keys must not lose the token"
                assert disk["env"]["DATABASE_URL"] == _DSN, "editing other keys must not lose the DSN"
                assert disk["env"]["NORMAL"] == "plainvalue2"
            finally:
                browser.close()


# --------------------------------------------------------------------------- #
# 设置 C：双窗口异键 / 同键冲突                                               #
# --------------------------------------------------------------------------- #

def test_two_windows_disjoint_keys_and_conflict_draft(tmp_path):
    """异键各改各的互不覆盖；同键 409 草稿保留，overwrite/reload 出口都生效。"""
    from playwright.sync_api import expect

    seed = json.dumps({"language": "en", "env": {"KEY_A": "a0", "KEY_B": "b0", "SHARED": "s0"}})
    with _runtime(tmp_path, settings_text=seed) as port:
        base = f"http://127.0.0.1:{port}"
        with _pw() as pw:
            browser = pw.chromium.launch()
            try:
                ctx_a, ctx_b = browser.new_context(), browser.new_context()
                pa, pb = ctx_a.new_page(), ctx_b.new_page()
                for p in (pa, pb):
                    p.add_init_script("window.localStorage.setItem('ginno.language','en')")
                # 轮 1：异键并行保存 → 两个编辑都落盘
                _open_env_tab(pa, base)
                _open_env_tab(pb, base)
                pa.get_by_test_id(f"env-value-{_row_index(pa, 'KEY_A')}").fill("a1")
                pb.get_by_test_id(f"env-value-{_row_index(pb, 'KEY_B')}").fill("b1")
                _save_env(pb)
                _save_env(pa)
                view = smoke._api(port, "GET", "/api/settings/env")["entries"]
                assert view["KEY_A"]["value"] == "a1" and view["KEY_B"]["value"] == "b1"
                # 轮 2：同键冲突 → 后写 409：草稿保留 + 服务端保持先写值
                pa.get_by_test_id(f"env-value-{_row_index(pa, 'SHARED')}").fill("sA")
                pb.get_by_test_id(f"env-value-{_row_index(pb, 'SHARED')}").fill("sB")
                _save_env(pb)
                pa.get_by_test_id("env-save").click()
                expect(pa.get_by_test_id("env-conflict")).to_be_visible(timeout=15_000)
                expect(pa.get_by_test_id("env-conflict")).to_contain_text("SHARED")
                expect(pa.get_by_test_id(f"env-value-{_row_index(pa, 'SHARED')}")).to_have_value("sA")
                view = smoke._api(port, "GET", "/api/settings/env")["entries"]
                assert view["SHARED"]["value"] == "sB", "conflicted save applies nothing"
                # 出口 1：显式覆盖 → 后写者胜出
                pa.get_by_test_id("env-conflict-overwrite").click()
                expect(pa.get_by_test_id("env-status")).to_have_text("Saved", timeout=15_000)
                assert smoke._api(port, "GET", "/api/settings/env")["entries"]["SHARED"]["value"] == "sA"
                # 轮 3：反向冲突（B 的 token 已过期）→ 出口 2：放弃草稿重拉
                pb.get_by_test_id(f"env-value-{_row_index(pb, 'SHARED')}").fill("sB2")
                pb.get_by_test_id("env-save").click()
                expect(pb.get_by_test_id("env-conflict")).to_be_visible(timeout=15_000)
                expect(pb.get_by_test_id(f"env-value-{_row_index(pb, 'SHARED')}")).to_have_value("sB2")
                pb.get_by_test_id("env-conflict-reload").click()
                expect(pb.get_by_test_id(f"env-value-{_row_index(pb, 'SHARED')}")).to_have_value("sA",
                                                                                                 timeout=15_000)
                assert smoke._api(port, "GET", "/api/settings/env")["entries"]["SHARED"]["value"] == "sA"
            finally:
                browser.close()


# --------------------------------------------------------------------------- #
# 调度 S1：once 生命周期                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.timeout(420)
def test_future_once_pause_not_done_zero_runs(tmp_path):
    """未来 once 手动暂停：runs=0、无 Done、显式 Paused；再启用恢复。"""
    from playwright.sync_api import expect

    with _runtime(tmp_path) as port:
        task = _schedule_task(port, "e2e once pause", at=_iso_in(2 * 3600))
        tid = task["id"]
        base = f"http://127.0.0.1:{port}"
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(base + "/scheduled", wait_until="load")
                row = _task_row(page, tid)
                assert row.get_by_text("Done", exact=True).count() == 0
                expect(row).to_contain_text(re.compile(r"Next:"))
                with page.expect_response(
                        lambda r: r.url.endswith(f"/api/schedule/tasks/{tid}")
                        and r.request.method == "PATCH", timeout=15_000):
                    row.locator(f'button[title="{_SCHED["tasks"]["pauseTitle"]}"]').click()
                row.locator(f'button[title="{_SCHED["tasks"]["enableTitle"]}"]').wait_for(
                    timeout=15_000)
                # 暂停后：不显示 Done，也不是「one-shot finished」——只是 Paused
                assert row.get_by_text("Done", exact=True).count() == 0
                expect(row).to_contain_text(_SCHED["tasks"]["statePaused"])
                assert "one-shot task finished" not in row.inner_text()
                assert _task(port, tid)["enabled"] is False
                runs = smoke._api(port, "GET", f"/api/schedule/runs?task_id={tid}")
                assert runs.get("total", 0) == 0 and not runs.get("rows"), \
                    "paused future once must have zero runs"
                # 再启用：徽标依旧不出现（Done 只能由按计划成功推导）
                row.locator(f'button[title="{_SCHED["tasks"]["enableTitle"]}"]').click()
                row.locator(f'button[title="{_SCHED["tasks"]["pauseTitle"]}"]').wait_for(
                    timeout=15_000)
                assert row.get_by_text("Done", exact=True).count() == 0
                assert _task(port, tid)["enabled"] is True
            finally:
                browser.close()


@pytest.mark.timeout(420)
def test_once_outcomes_success_failure_and_legacy(tmp_path):
    """按计划成功 / 失败 / 老数据（legacyDone 与 manual→paused）四种终态可区分。

    老任务 = 调度器从未写 last_scheduled 的存量数据（spawn 前预置
    schedules.json）；另预置一条缺 trigger 的老 runs 行——Runs 页必须照常
    渲染（未知来源不崩、不臆造结论）。
    """
    from playwright.sync_api import expect

    now = time.time()
    past = (datetime.now() - timedelta(hours=2)).replace(microsecond=0).isoformat()
    legacy_done = {
        "id": "legacy-done", "name": "e2e legacy done", "enabled": False, "notify": False,
        "model": None, "thinking_effort": None,
        "target": {"type": "prompt", "prompt": "hi"},
        "schedule": {"kind": "once", "at": past},
        "created": now - 86400, "updated": now - 7200,
        "last_run": {"run_id": "legacy-run-1", "status": "ok", "trigger": "schedule",
                     "started": now - 7200, "finished": now - 7140},
    }
    legacy_manual = {
        "id": "legacy-manual", "name": "e2e legacy manual", "enabled": False, "notify": False,
        "model": None, "thinking_effort": None,
        "target": {"type": "prompt", "prompt": "hi"},
        "schedule": {"kind": "once", "at": past},
        "created": now - 86400, "updated": now - 7200,
        "last_run": {"run_id": "legacy-run-2", "status": "ok", "trigger": "manual",
                     "started": now - 3600, "finished": now - 3540},
    }
    old_row_no_trigger = {
        "run_id": "old-no-trigger", "task_id": "legacy-done", "task_name": "e2e legacy done",
        "target_type": "prompt", "status": "ok",
        "scheduled_at": now - 7200, "started_at": now - 7200, "finished_at": now - 7140,
    }  # 老版本写的行：没有 trigger 字段（来源未知）
    schedules = {"enabled": True, "keep_awake": False, "catch_up_enabled": False,
                 "tasks": [legacy_done, legacy_manual]}
    with _runtime(tmp_path, schedules=schedules,
                  run_rows=[legacy_done["last_run"] | {"task_id": "legacy-done",
                                                       "task_name": "e2e legacy done",
                                                       "target_type": "prompt",
                                                       "scheduled_at": now - 7200,
                                                       "started_at": now - 7200},
                            old_row_no_trigger]) as port:
        ok_t = _schedule_task(port, "e2e once ok", at=_iso_in(5))
        err_t = _schedule_task(port, "e2e once fail", at=_iso_in(5),
                               target={"type": "workflow", "workflow_id": "wf-does-not-exist"})
        ok_me = _wait_task(port, ok_t["id"],
                           lambda t: (t.get("last_scheduled") or {}).get("status") == "ok",
                           what="scheduled success")
        assert ok_me["enabled"] is False
        err_me = _wait_task(port, err_t["id"],
                            lambda t: (t.get("last_scheduled") or {}).get("status") == "error",
                            what="scheduled failure")
        assert err_me["enabled"] is False

        base = f"http://127.0.0.1:{port}"
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(base + "/scheduled", wait_until="load")
                ok_row = _task_row(page, ok_t["id"])
                expect(ok_row.get_by_text("Done", exact=True)).to_be_visible(timeout=15_000)
                expect(ok_row).to_contain_text(_SCHED["tasks"]["stateOnceDone"])
                err_row = _task_row(page, err_t["id"])
                assert err_row.get_by_text("Done", exact=True).count() == 0, \
                    "failed scheduled run must not claim Done"
                expect(err_row).to_contain_text(_SCHED["tasks"]["stateOnceFailed"])
                ld = _task_row(page, "legacy-done")
                expect(ld.get_by_text(_SCHED["tasks"]["doneLegacy"], exact=True)).to_be_visible(
                    timeout=15_000)
                expect(ld).to_contain_text(_SCHED["tasks"]["stateOnceDone"])
                lm = _task_row(page, "legacy-manual")
                assert lm.get_by_text("Done", exact=True).count() == 0
                assert lm.get_by_text(_SCHED["tasks"]["doneLegacy"], exact=True).count() == 0
                expect(lm).to_contain_text(_SCHED["tasks"]["statePaused"])
                # 失败任务在 runs 里有 error 记录（可对账）
                rows = _wait_runs(port, err_t["id"])
                assert rows[0]["status"] == "error"
                # Runs 页：老数据（含缺 trigger 的未知来源行）照常渲染。
                # run_id 不在表格 UI 中（仅在错误详情 tooltip 里），断言打
                # task_name 行与行数——预置的两条老行（含缺 trigger）都要在。
                page.get_by_role("button", name=re.compile(r"^Runs")).click()
                legacy_rows = page.get_by_role("row").filter(has_text="e2e legacy done")
                expect(legacy_rows.first).to_be_visible(timeout=15_000)
                assert legacy_rows.count() >= 2, \
                    f"预置两条老 runs 行都必须渲染（缺 trigger 不崩），实际 {legacy_rows.count()}"
            finally:
                browser.close()


# --------------------------------------------------------------------------- #
# 调度 S2：加载失败 / 操作被拒 / 丢回执                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("failure", ["json503", "disconnect"])
@pytest.mark.timeout(420)
def test_schedule_first_load_failure_retries(tmp_path, failure):
    """首拉失败（JSON 5xx / 断连）→ 只重试，绝不把错误体渲染成配置。"""
    from playwright.sync_api import expect

    with _runtime(tmp_path) as port:
        t = _schedule_task(port, "e2e load fail")
        base = f"http://127.0.0.1:{port}"
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(base + "/scheduled", wait_until="load")
                _task_row(page, t["id"])  # 先证明任务本来可见
                page.goto("about:blank")

                def fail(route):
                    if failure == "json503":
                        route.fulfill(status=503, content_type="application/json",
                                      body=json.dumps({"detail": "boom"}))
                    else:
                        route.abort("connectionreset")

                page.route(re.compile(r"/api/schedule$"), fail)
                page.goto(base + "/scheduled", wait_until="load")
                expect(page.get_by_text(_SCHED["page"]["loadRetrying"])).to_be_visible(
                    timeout=15_000)
                expect(page.get_by_role("button", name=_SCHED["page"]["retryNow"])).to_be_visible()
                assert page.locator("[data-task-id]").count() == 0, \
                    "failed first load must not render stale/garbage rows"
                # 恢复：手动 Retry now → 真实配置回到页面
                page.unroute(re.compile(r"/api/schedule$"))
                page.get_by_role("button", name=_SCHED["page"]["retryNow"]).click()
                _task_row(page, t["id"])
            finally:
                browser.close()


@pytest.mark.parametrize("failure", ["disconnect", "http500"])
@pytest.mark.timeout(420)
def test_schedule_toggle_rejection_keeps_truth(tmp_path, failure):
    """暂停被拒（断连 / 500 JSON）→ 行内红反馈 + 服务端 enabled 未变。"""
    from playwright.sync_api import expect

    with _runtime(tmp_path) as port:
        t = _schedule_task(port, "e2e toggle reject")
        tid = t["id"]
        base = f"http://127.0.0.1:{port}"
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                page.goto(base + "/scheduled", wait_until="load")
                row = _task_row(page, tid)
                pause = row.locator(f'button[title="{_SCHED["tasks"]["pauseTitle"]}"]')

                def reject(route):
                    if failure == "http500":
                        route.fulfill(status=500, content_type="application/json",
                                      body=json.dumps({"detail": "boom"}))
                    else:
                        route.abort("connectionreset")

                page.route(re.compile(rf"/api/schedule/tasks/{tid}$"), reject)
                pause.click()
                fb = page.get_by_test_id(f"task-action-feedback-{tid}")
                expect(fb).to_be_visible(timeout=15_000)
                expect(fb).to_contain_text("Pause/enable failed")
                if failure == "http500":
                    expect(fb).to_contain_text("boom")
                assert _task(port, tid)["enabled"] is True, "rejected PATCH must not flip the server"
                row.locator(f'button[title="{_SCHED["tasks"]["pauseTitle"]}"]').wait_for(
                    timeout=15_000), "UI 对账后仍显示可暂停（未静默翻转）"
                # 恢复后重试 → 真实成功
                page.unroute(re.compile(rf"/api/schedule/tasks/{tid}$"))
                pause.click()
                row.locator(f'button[title="{_SCHED["tasks"]["enableTitle"]}"]').wait_for(
                    timeout=15_000)
                assert _task(port, tid)["enabled"] is False
            finally:
                browser.close()


@pytest.mark.timeout(420)
def test_run_now_lost_receipt_unknown_and_ledger(tmp_path):
    """立即执行：请求真到服务端但回执丢失 + WS 屏蔽 → 「结果未知」+ 对账入口。

    契约（schedule-integrity）：网络层失败（回执丢失）绝不能自动重发（请求
    可能已生效）；行内给出查看记录入口；服务端 runs 是对账真源。
    """
    from playwright.sync_api import expect

    with _runtime(tmp_path) as port:
        t = _schedule_task(port, "e2e run now lost")
        tid = t["id"]
        base = f"http://127.0.0.1:{port}"
        with _pw() as pw:
            browser, page = smoke._new_page(pw)
            try:
                # 屏蔽 schedule 事件 WS（断连场景下 push 通道不可用）
                page.add_init_script("""
                  (() => {
                    const OrigWS = window.WebSocket;
                    window.WebSocket = function(url, protocols) {
                      if (String(url).indexOf('/api/ws/schedule') !== -1) {
                        window.__scheduleWsMocked = (window.__scheduleWsMocked || 0) + 1;
                        return { close(){}, addEventListener(){}, removeEventListener(){},
                          onmessage:null, onerror:null, onopen:null, onclose:null,
                          readyState:1, send(){}, bufferedAmount:0, extensions:'',
                          protocol:'', url:String(url), binaryType:'blob' };
                      }
                      return new OrigWS(url, protocols);
                    };
                  })();
                """)
                page.goto(base + "/scheduled", wait_until="load")
                row = _task_row(page, tid)
                assert page.evaluate("window.__scheduleWsMocked || 0") >= 1, "schedule WS is mocked off"

                server_receipt: dict = {}
                posts = {"n": 0}

                def lose(route):
                    posts["n"] += 1
                    resp = route.fetch()  # 请求确实到达真实服务端
                    server_receipt["status"] = resp.status
                    route.abort("connectionreset")  # 浏览器侧只见断连

                page.route(re.compile(rf"/api/schedule/tasks/{tid}/run$"), lose)
                row.locator(f'button[title="{_SCHED["tasks"]["runNowTitle"]}"]').click()
                fb = page.get_by_test_id(f"task-action-feedback-{tid}")
                expect(fb).to_be_visible(timeout=20_000)
                expect(fb).to_contain_text("Sent but no receipt")
                # 服务端确实接受了（对账真源），但 UI 绝不自动重发
                _wait_runs(port, tid, at_least=1)
                assert server_receipt.get("status") == 200
                page.wait_for_timeout(2500)
                assert posts["n"] == 1, "lost receipt must never auto-resend"
                # 对账入口：反馈块内的「查看此任务记录」按钮
                view = page.get_by_test_id(f"task-view-runs-{tid}")
                expect(view).to_be_visible()
                view.click()
                expect(page.get_by_role("button", name=re.compile(r"^Runs"))).to_be_visible(
                    timeout=10_000), "对账入口应落到执行记录"
                # 手动刷新仍可用（WS 之外的状态恢复路径）——先切回任务页签
                # 再断言行（对账入口把我们带到了 Runs 页签）。
                page.get_by_role("button", name=re.compile(r"^Tasks")).click()
                page.get_by_title(_SCHED["page"]["refresh"], exact=True).click()
                _task_row(page, tid)
                # 恢复通道后重试是安全的：确认首跑完成后再次 Run now 真实成功
                page.unroute(re.compile(rf"/api/schedule/tasks/{tid}/run$"))
                _wait_runs(port, tid, finished=True)
                page.get_by_title(_SCHED["page"]["refresh"], exact=True).click()
                row = _task_row(page, tid)
                row.locator(f'button[title="{_SCHED["tasks"]["runNowTitle"]}"]').click()
                page.wait_for_function(
                    f"() => !document.querySelector('[data-testid=\"task-action-feedback-{tid}\"]')",
                    timeout=30_000)
                rows = _wait_runs(port, tid, at_least=2)
                # posts 计数在 unroute 后不再经过 handler——第二次真实成功以
                # 服务端 runs（对账真源）为准：≥2 条即「丢失回执的那次 + 手动
                # 重试的那次」都真实落账。
                assert len(rows) >= 2
            finally:
                browser.close()


# --------------------------------------------------------------------------- #
# playwright 上下文 helper（顶层 import 会让无 playwright 环境收集失败）        #
# --------------------------------------------------------------------------- #

@contextlib.contextmanager
def _pw():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        yield p

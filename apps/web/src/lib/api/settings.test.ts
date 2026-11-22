/**
 * api/settings.ts（settings 完整性 & env CAS 客户端）单元测试。
 *
 * 打桩策略：桩 globalThis.fetch 走真实实现，不 mock ./core；i18n provider
 * 桩成固定 zh-CN。重点分支：
 *  - patchEnvSettings 五种结局（200 视图 / 409 version_conflict 带 conflicts /
 *    409 settings_corrupt / 400 invalid_request / 500 write_failed）与网络失败；
 *  - getEnvSettings 的 corrupt 视图透传 parseError、坏形状降级；
 *  - health/backup/repair 的 ok/失败/网络三分支；
 *  - 请求形状：PATCH body 包 {updates}、locale 头、方法。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../i18n/provider", () => ({ currentLocale: () => "zh-CN" }));

import {
  backupSettings,
  getEnvSettings,
  getSettingsHealth,
  patchEnvSettings,
  repairSettings,
} from "./settings";

const API = "http://127.0.0.1:8787/api";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function reply(body: unknown, status = 200) {
  fetchMock.mockResolvedValue({ ok: status >= 200 && status < 300, status, json: async () => body });
}

function lastCall() {
  const [url, init] = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
  return { url, init };
}

describe("getEnvSettings", () => {
  it("正常视图原样返回", async () => {
    reply({ status: "ok", entries: { A: { sensitive: false, kind: "plain", value: "1", version: "v" } } });
    const v = await getEnvSettings();
    expect(v).toEqual({ status: "ok", entries: { A: { sensitive: false, kind: "plain", value: "1", version: "v" } } });
    expect(lastCall().url).toBe(`${API}/settings/env`);
    expect(lastCall().init.method).toBe("GET");
  });
  it("corrupt 视图透传 parseError、entries 强制为空", async () => {
    reply({ status: "corrupt", parseError: "JSONDecodeError: x", entries: { A: { sensitive: false, kind: "plain", version: "v" } } });
    expect(await getEnvSettings()).toEqual({ status: "corrupt", entries: {}, parseError: "JSONDecodeError: x" });
  });
  it("坏形状（无 entries）降级为空视图而非抛错", async () => {
    reply({});
    expect(await getEnvSettings()).toEqual({ status: "ok", entries: {} });
  });
  it("网络失败 → null（调用方走 loadFailed，绝不把错误体当视图）", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    expect(await getEnvSettings()).toBeNull();
  });
  it("503 JSON 错误体不会被判成视图（非 2xx → null，走 loadFailed）", async () => {
    reply({ detail: "boom" }, 503);
    expect(await getEnvSettings()).toBeNull();
  });
});

describe("patchEnvSettings", () => {
  const updates = [{ key: "A", version: "v1", value: "2" }] as const;

  it("200 → 新视图（受影响键 version 已轮换）", async () => {
    reply({ status: "ok", entries: { A: { sensitive: false, kind: "plain", value: "2", version: "v2" } } });
    const r = await patchEnvSettings([...updates]);
    expect(r).toEqual({ ok: true, view: { status: "ok", entries: { A: { sensitive: false, kind: "plain", value: "2", version: "v2" } } } });
    const { url, init } = lastCall();
    expect(url).toBe(`${API}/settings/env`);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ updates });
    expect(init.headers["X-Ginno-Language"]).toBe("zh-CN");
  });

  it("409 version_conflict → conflicts 清单（current_version 供覆盖重试）", async () => {
    reply({ ok: false, error: "version_conflict", detail: "…", conflicts: [{ key: "A", current_version: "v9" }] }, 409);
    const r = await patchEnvSettings([...updates]);
    expect(r).toEqual({ ok: false, error: "version_conflict", conflicts: [{ key: "A", current_version: "v9" }], detail: "…" });
  });

  it("409 settings_corrupt（损坏拒写，原文件保留）", async () => {
    reply({ ok: false, error: "settings_corrupt", detail: "JSONDecodeError: x" }, 409);
    expect(await patchEnvSettings([...updates])).toEqual({ ok: false, error: "settings_corrupt", detail: "JSONDecodeError: x" });
  });

  it("400 invalid_request（如同请求同键）", async () => {
    reply({ ok: false, error: "invalid_request", detail: "duplicate key in one request (all-or-nothing): A" }, 400);
    expect(await patchEnvSettings([...updates])).toEqual({
      ok: false,
      error: "invalid_request",
      detail: "duplicate key in one request (all-or-nothing): A",
    });
  });

  it("500 write_failed（原文件未动）", async () => {
    reply({ ok: false, error: "write_failed", detail: "atomic write failed: disk full" }, 500);
    expect(await patchEnvSettings([...updates])).toEqual({ ok: false, error: "write_failed", detail: "atomic write failed: disk full" });
  });

  it("网络失败 → network（不抛错）", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    expect(await patchEnvSettings([...updates])).toEqual({ ok: false, error: "network" });
  });

  it("非 JSON 错误体 → detail 回退 HTTP 状态码", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 500, json: async () => { throw new Error("not json"); } });
    expect(await patchEnvSettings([...updates])).toEqual({ ok: false, error: "write_failed", detail: "HTTP 500" });
  });
});

describe("getSettingsHealth", () => {
  it("三态各自透传", async () => {
    reply({ status: "ok", error: null, path: "~/.ginno/settings.json" });
    expect(await getSettingsHealth()).toEqual({ status: "ok", error: null, path: "~/.ginno/settings.json" });
    reply({ status: "corrupt", error: "JSONDecodeError: bad", path: "p" });
    expect(await getSettingsHealth()).toEqual({ status: "corrupt", error: "JSONDecodeError: bad", path: "p" });
  });
  it("未知/坏形状 → null（横幅静默，不误报损坏）", async () => {
    reply({ status: "weird" });
    expect(await getSettingsHealth()).toBeNull();
  });
  it("网络失败 → null", async () => {
    fetchMock.mockRejectedValue(new Error("offline"));
    expect(await getSettingsHealth()).toBeNull();
  });
});

describe("backupSettings", () => {
  it("成功回 path", async () => {
    reply({ ok: true, path: "~/.ginno/settings.json.bak-20261122T0000Z" });
    expect(await backupSettings()).toEqual({ ok: true, path: "~/.ginno/settings.json.bak-20261122T0000Z" });
    expect(lastCall().init.method).toBe("POST");
  });
  it("失败回 error；网络失败不抛", async () => {
    reply({ ok: false, error: "settings.json does not exist" }, 500);
    expect(await backupSettings()).toEqual({ ok: false, error: "settings.json does not exist" });
    fetchMock.mockRejectedValue(new Error("offline"));
    expect(await backupSettings()).toEqual({ ok: false, error: "runtime unreachable" });
  });
});

describe("repairSettings", () => {
  it("成功回 backup + restart_required；PUT 方法", async () => {
    reply({ ok: true, backup: "~/.ginno/settings.json.bak-x-pre-repair", restart_required: true });
    expect(await repairSettings({ a: 1 })).toEqual({ ok: true, backup: "~/.ginno/settings.json.bak-x-pre-repair", restart_required: true });
    expect(lastCall().init.method).toBe("PUT");
    expect(lastCall().url).toBe(`${API}/settings/repair`);
    expect(JSON.parse(lastCall().init.body)).toEqual({ a: 1 });
  });
  it("失败回 error+detail；网络失败不抛", async () => {
    reply({ ok: false, error: "settings_corrupt", detail: "still corrupt" }, 409);
    expect(await repairSettings({})).toEqual({ ok: false, error: "settings_corrupt", detail: "still corrupt" });
    fetchMock.mockRejectedValue(new Error("offline"));
    expect(await repairSettings({})).toEqual({ ok: false, error: "runtime unreachable" });
  });
});

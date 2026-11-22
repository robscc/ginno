// lib/desktop 桌面壳桥接层补测（覆盖率第二批）。
//
// 与 desktopNotifyGate.test.ts 的分工：那边已覆盖 notifyNative 的窗口闸门与
// 偏好闸门（审计 P0-3/P1-1），本文件不重复那些用例，只补它够不到的分支：
//   - isDesktop 的 WKWebView UA 回落判定（release 包经 splash location.replace
//     进来时没有 __TAURI_INTERNALS__，靠 UA 识别）
//   - isNotifyOwnerWindow 的 location 访问抛错兜底
//   - notifyNative 的 emit 抛错（桥不可用）与 test 类提示音分支
//   - setKeepAwake / getAutostartEnabled / setAutostart 全段：桌面/非桌面双路径
//     + invoke / isEnabled / enable / disable 抛错
//
// 环境 node 无 DOM：window/navigator 全用 vi.stubGlobal 伪造；Tauri API 全桩，
// 只桩依赖、不 mock 被测模块自身。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { emit } = vi.hoisted(() => ({ emit: vi.fn(async () => {}) }));
vi.mock("@tauri-apps/api/event", () => ({ emit }));

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

// 2026-11-22 巡检 P1 后 autostart 走自有 command（SMAppService 真源 +
// legacy plist 迁移），不再用 plugin-autostart；断言按命令名过滤 invoke
// 调用（invoke 是整文件共享 mock，其他功能也在用）。
const autostartCalls = (cmd: string) =>
  invoke.mock.calls.filter((c: unknown[]) => c[0] === cmd);
const autostartNext: Record<string, unknown[]> = {};
const stubAutostart = (cmd: string, value: unknown) => {
  (autostartNext[cmd] ??= []).push(value);
};
beforeEach(() => {
  for (const k of Object.keys(autostartNext)) delete autostartNext[k];
});
const invokeAutostart = async (cmd: string) => {
  const q = autostartNext[cmd];
  const v = q?.shift();
  if (v instanceof Error) throw v;
  if (q && q.length === 0 && autostartNext[cmd]) delete autostartNext[cmd];
  return v;
};

// notifyPrefs 的偏好缓存只能经 loadNotifyPrefs 写入，而它读 ./runtime 的
// getSettings——桩掉它就能精确控制偏好（与 desktopNotifyGate.test.ts 同范式）。
const { getSettings, putSettings } = vi.hoisted(() => ({
  getSettings: vi.fn(async () => ({}) as Record<string, unknown>),
  putSettings: vi.fn(async () => undefined),
}));
vi.mock("./runtime", () => ({ getSettings, putSettings }));

import {
  getAutostartEnabled,
  isDesktop,
  isNotifyOwnerWindow,
  notifyNative,
  setAutostart,
  setKeepAwake,
  type NativeNotification,
} from "@/lib/desktop";
import { loadNotifyPrefs } from "@/lib/notifyPrefs";

const PAYLOAD: NativeNotification = {
  kind: "session",
  notifyKind: "reply",
  id: "s1",
  title: "测试会话",
  body: "回复完成",
};

/** Tauri 壳内窗口：isDesktop 认 __TAURI_INTERNALS__。 */
function stubDesktopWindow(pathname = "/") {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {}, location: { pathname } });
}

/** 普通浏览器窗口：无桥标志，UA 决定 isDesktop 的回落判定。 */
function stubBrowserWindow(userAgent: string) {
  vi.stubGlobal("window", { location: { pathname: "/" } });
  vi.stubGlobal("navigator", { userAgent });
}

/** 写入偏好缓存。⚠️ Node 25 暴露了 localStorage 全局但 getItem 不是函数，
 *  loadNotifyPrefs 第一行就会抛 TypeError 被自己的 catch 吞掉、偏好停在默认值，
 *  所以必须桩掉（浏览器里正常，纯为可测性）。 */
async function setPrefs(raw: Record<string, unknown>) {
  vi.stubGlobal("localStorage", {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  });
  getSettings.mockResolvedValue({ notifications: raw });
  await loadNotifyPrefs();
}

/** 最近一次 emit 的载荷。 */
function lastEmitPayload() {
  const calls = emit.mock.calls as unknown as Array<[string, Record<string, unknown>]>;
  return calls.length ? calls[calls.length - 1][1] : null;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("isDesktop：壳检测双路径", () => {
  it("无 window（SSR/node）→ false", () => {
    vi.stubGlobal("window", undefined);
    expect(isDesktop()).toBe(false);
  });

  it("有 __TAURI_INTERNALS__ → true（即便 UA 是 Chrome，桥标志优先）", () => {
    vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
    vi.stubGlobal("navigator", {
      userAgent: "Mozilla/5.0 Chrome/120 Safari/537.36",
    });
    expect(isDesktop()).toBe(true);
  });

  it("release 回落：WKWebView UA（AppleWebKit 且无 Safari/Chrome token）→ true", () => {
    stubBrowserWindow(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)",
    );
    expect(isDesktop()).toBe(true);
  });

  it("Safari UA（含 Safari token）→ false", () => {
    stubBrowserWindow(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    );
    expect(isDesktop()).toBe(false);
  });

  it("Chrome UA（含 Chrome token）→ false", () => {
    stubBrowserWindow(
      "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    );
    expect(isDesktop()).toBe(false);
  });

  it("非 WebKit UA（Firefox）→ false", () => {
    stubBrowserWindow(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:130.0) Gecko/20100101 Firefox/130.0",
    );
    expect(isDesktop()).toBe(false);
  });

  it("空 UA → false", () => {
    stubBrowserWindow("");
    expect(isDesktop()).toBe(false);
  });
});

describe("isNotifyOwnerWindow：边界补充（主路径见 desktopNotifyGate.test.ts）", () => {
  it("location 访问抛错（跨域受限）→ 兜底认领所有权 true", () => {
    vi.stubGlobal("window", {
      get location() {
        throw new Error("cross-origin access blocked");
      },
    });
    expect(isNotifyOwnerWindow()).toBe(true);
  });

  it("判定是精确匹配 /pin：/pinboard 等前缀路由仍是所有者", () => {
    vi.stubGlobal("window", { location: { pathname: "/pinboard" } });
    expect(isNotifyOwnerWindow()).toBe(true);
  });
});

describe("notifyNative：桥错误与 test 提示音（闸门用例见 desktopNotifyGate.test.ts）", () => {
  beforeEach(async () => {
    stubDesktopWindow("/");
    await setPrefs({ enabled: true, sound: true, sound_name: "Glass" });
  });

  it("emit 抛错（splash 跳转后桥不可用）→ false，不向调用方抛异常", async () => {
    emit.mockRejectedValueOnce(new Error("bridge unavailable"));
    await expect(notifyNative(PAYLOAD)).resolves.toBe(false);
    expect(emit).toHaveBeenCalledTimes(1);
    expect(emit).toHaveBeenCalledWith(
      "ginno:notify",
      expect.objectContaining({ id: "s1" }),
    );
  });

  it("test 类不推导提示音：即使 sound 开着也静音，正文不裁", async () => {
    await expect(
      notifyNative({ ...PAYLOAD, notifyKind: "test" }),
    ).resolves.toBe(true);
    expect(lastEmitPayload()?.sound).toBeUndefined();
    expect(lastEmitPayload()?.body).toBe("回复完成");
  });

  it("test 类显式给的 sound 仍优先", async () => {
    await notifyNative({ ...PAYLOAD, notifyKind: "test", sound: "Ping" });
    expect(lastEmitPayload()?.sound).toBe("Ping");
  });

  it("非桌面 → false 且不 emit（HTML5 兜底由调用方负责）", async () => {
    stubBrowserWindow("Mozilla/5.0 Chrome/120 Safari/537.36");
    await expect(notifyNative(PAYLOAD)).resolves.toBe(false);
    expect(emit).not.toHaveBeenCalled();
  });
});

describe("setKeepAwake", () => {
  beforeEach(() => {
    invoke.mockResolvedValue(undefined);
  });

  it("非桌面 → false，且一次 invoke 都不发", async () => {
    stubBrowserWindow("Mozilla/5.0 Chrome/120 Safari/537.36");
    await expect(setKeepAwake(true)).resolves.toBe(false);
    expect(invoke).not.toHaveBeenCalled();
  });

  it("桌面：enabled=true 透传给壳命令 set_keep_awake", async () => {
    stubDesktopWindow();
    await expect(setKeepAwake(true)).resolves.toBe(true);
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith("set_keep_awake", { enabled: true });
  });

  it("桌面：enabled=false 同样透传（释放 caffeinate 子进程）", async () => {
    stubDesktopWindow();
    await expect(setKeepAwake(false)).resolves.toBe(true);
    expect(invoke).toHaveBeenCalledWith("set_keep_awake", { enabled: false });
  });

  it("invoke 抛错 → false（调用方优雅降级，不抛异常）", async () => {
    stubDesktopWindow();
    invoke.mockRejectedValueOnce(new Error("command not found"));
    await expect(setKeepAwake(true)).resolves.toBe(false);
    expect(invoke).toHaveBeenCalledTimes(1);
  });
});

describe("getAutostartEnabled", () => {
  it("非桌面 → null（设置页开关据此隐藏），不调 command", async () => {
    stubBrowserWindow("Mozilla/5.0 Chrome/120 Safari/537.36");
    await expect(getAutostartEnabled()).resolves.toBeNull();
    expect(autostartCalls("ginno_autostart_is_enabled").length).toBe(0);
  });

  it("桌面：SMAppService 已注册 → true", async () => {
    stubDesktopWindow();
    stubAutostart("ginno_autostart_is_enabled", true);
    invoke.mockImplementation(((cmd: string) => invokeAutostart(cmd)) as never);
    await expect(getAutostartEnabled()).resolves.toBe(true);
    expect(autostartCalls("ginno_autostart_is_enabled").length).toBe(1);
  });

  it("桌面：未注册 → false", async () => {
    stubDesktopWindow();
    stubAutostart("ginno_autostart_is_enabled", false);
    invoke.mockImplementation(((cmd: string) => invokeAutostart(cmd)) as never);
    await expect(getAutostartEnabled()).resolves.toBe(false);
  });

  it("command 抛错 → null（与浏览器环境不可区分，开关隐藏）", async () => {
    stubDesktopWindow();
    stubAutostart("ginno_autostart_is_enabled", new Error("command missing"));
    invoke.mockImplementation(((cmd: string) => invokeAutostart(cmd)) as never);
    await expect(getAutostartEnabled()).resolves.toBeNull();
  });

  it("无本地缓存：每次调用都重读系统注册态（用户在系统设置里关掉能如实反映）", async () => {
    stubDesktopWindow();
    stubAutostart("ginno_autostart_is_enabled", true);
    stubAutostart("ginno_autostart_is_enabled", false);
    invoke.mockImplementation(((cmd: string) => invokeAutostart(cmd)) as never);
    await expect(getAutostartEnabled()).resolves.toBe(true);
    await expect(getAutostartEnabled()).resolves.toBe(false);
    expect(autostartCalls("ginno_autostart_is_enabled").length).toBe(2);
  });
});

describe("setAutostart", () => {
  it("非桌面 → {ok:false, error:'not desktop'}，不调 command", async () => {
    stubBrowserWindow("Mozilla/5.0 Chrome/120 Safari/537.36");
    await expect(setAutostart(true)).resolves.toEqual({
      ok: false,
      error: "not desktop",
    });
    expect(autostartCalls("ginno_autostart_set").length).toBe(0);
  });

  it("桌面：enabled=true 发 ginno_autostart_set {enabled:true}", async () => {
    stubDesktopWindow();
    await expect(setAutostart(true)).resolves.toEqual({ ok: true });
    expect(autostartCalls("ginno_autostart_set").length).toBe(1);
    expect(invoke).toHaveBeenCalledWith("ginno_autostart_set", {
      enabled: true,
    });
  });

  it("桌面：enabled=false 同样发 set（Rust 侧双途径清理）", async () => {
    stubDesktopWindow();
    await expect(setAutostart(false)).resolves.toEqual({ ok: true });
    expect(invoke).toHaveBeenCalledWith("ginno_autostart_set", {
      enabled: false,
    });
  });

  it("command 抛错 → ok:false 且 error 是 String(e)（策略拦截等场景）", async () => {
    stubDesktopWindow();
    invoke.mockRejectedValueOnce(new Error("SMAppService policy blocked"));
    await expect(setAutostart(true)).resolves.toEqual({
      ok: false,
      error: "Error: SMAppService policy blocked",
    });
  });

  it("command 抛非 Error 值 → String(e) 原样透传", async () => {
    stubDesktopWindow();
    invoke.mockRejectedValueOnce("approval required");
    await expect(setAutostart(false)).resolves.toEqual({
      ok: false,
      error: "approval required",
    });
  });
});

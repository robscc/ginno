// lib/desktop 的通知闸门（docs/notifications-audit-2026-11-14.md P0-3 + P1-1）。
//
// notifyNative 是**所有 OS 通知的唯一出口**，因此偏好闸门装在这里而不是散在各
// 调用点。修复前 7 个发通知的站点各自拼闸门，覆盖极不均匀：handle.ts 的
// lastwords 与 NoticeHost 的失焦兜底完全不过总开关/分类/勿扰/隐私，
// store/notifications.ts 的三个函数只过总开关——于是「关掉桌面提醒」关不干净、
// 「开了勿扰」照样弹、「仅标题」隐私对一半站点无效。
//
// 另一件事：悬浮窗 /pin 为拿 hooks 也挂了 AppShell，而 AppShell 所有 hook 都跑
// 在 `if (pathname === "/pin") return` 早退**之前**；pin 窗口建出来即 hidden 且
// hide 保活，其 visibilityState !== "visible" 恒为真 → 那三处「用户没在看才弹」
// 的前提永远成立。闸门放在唯一出口，一处覆盖全部站点（含将来新增的）。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { emit } = vi.hoisted(() => ({ emit: vi.fn(async () => {}) }));
vi.mock("@tauri-apps/api/event", () => ({ emit }));

// notifyPrefs 的偏好缓存只能经 loadNotifyPrefs 写入，而它读 ./runtime 的
// getSettings——桩掉它就能精确控制偏好，不必碰真实 settings.json。
const { getSettings, putSettings } = vi.hoisted(() => ({
  getSettings: vi.fn(async () => ({}) as Record<string, unknown>),
  putSettings: vi.fn(async () => undefined),
}));
vi.mock("./runtime", () => ({ getSettings, putSettings }));

import { isNotifyOwnerWindow, notifyNative, type NativeNotification } from "@/lib/desktop";
import { loadNotifyPrefs } from "@/lib/notifyPrefs";

const PAYLOAD: NativeNotification = {
  kind: "session",
  notifyKind: "reply",
  id: "s1",
  title: "⏸ 测试会话",
  body: "回复完成",
};

/** 伪装成 Tauri 壳内的窗口（isDesktop 认 __TAURI_INTERNALS__）。 */
function stubWindow(pathname = "/") {
  vi.stubGlobal("window", { __TAURI_INTERNALS__: {}, location: { pathname } });
}

/** 最近一次 emit 的载荷。 */
function lastPayload() {
  const calls = emit.mock.calls as unknown as Array<[string, Record<string, unknown>]>;
  return calls.length ? calls[calls.length - 1][1] : null;
}

/** 写入偏好缓存。传 settings.json 里 notifications 键的原始形状。
 *
 *  ⚠️ Node 25 暴露了 `localStorage` 全局对象、但 `getItem` 不是函数（未开
 *  --experimental-webstorage），而 loadNotifyPrefs 的第一行就是
 *  `localStorage.getItem(LEGACY_KEY)` → 抛 TypeError 被它自己的 catch 吞掉，
 *  偏好永远停在默认值、getSettings 一次都不会被调。浏览器里两者都正常，这里
 *  纯粹为了可测性打桩（sessionStorage 在 Node 25 反而是可用的）。 */
async function setPrefs(raw: Record<string, unknown>) {
  vi.stubGlobal("localStorage", {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  });
  getSettings.mockResolvedValue({ notifications: raw });
  await loadNotifyPrefs();
}

describe("isNotifyOwnerWindow", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("主窗（/）是通知所有者", () => {
    stubWindow("/");
    expect(isNotifyOwnerWindow()).toBe(true);
  });

  it("其它业务路由同样是所有者", () => {
    for (const p of ["/settings", "/workflows", "/kb"]) {
      stubWindow(p);
      expect(isNotifyOwnerWindow()).toBe(true);
    }
  });

  it("悬浮窗 /pin 不是所有者", () => {
    stubWindow("/pin");
    expect(isNotifyOwnerWindow()).toBe(false);
  });

  it("无 window（node/SSR）不认领所有权", () => {
    vi.stubGlobal("window", undefined);
    expect(isNotifyOwnerWindow()).toBe(false);
  });
});

describe("notifyNative 的窗口闸门", () => {
  beforeEach(async () => {
    emit.mockClear();
    await setPrefs({ enabled: true });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("主窗：事件照常投递给壳", async () => {
    stubWindow("/");
    await expect(notifyNative(PAYLOAD)).resolves.toBe(true);
    expect(emit).toHaveBeenCalledWith(
      "ginno:notify",
      expect.objectContaining({ kind: "session", id: "s1" }),
    );
  });

  it("悬浮窗：一条都不投递（第二通知源被掐掉）", async () => {
    stubWindow("/pin");
    await expect(notifyNative(PAYLOAD)).resolves.toBe(false);
    expect(emit).not.toHaveBeenCalled();
  });

  it("非桌面环境：保持原语义返回 false", async () => {
    vi.stubGlobal("window", { location: { pathname: "/" } });
    vi.stubGlobal("navigator", { userAgent: "Mozilla/5.0 Chrome/120 Safari/537.36" });
    await expect(notifyNative(PAYLOAD)).resolves.toBe(false);
    expect(emit).not.toHaveBeenCalled();
  });
});

describe("notifyNative 的偏好闸门（P1-1：出口统一施加）", () => {
  beforeEach(() => {
    emit.mockClear();
    stubWindow("/");
  });
  afterEach(() => vi.unstubAllGlobals());

  it("总开关关掉 → 任何分类都不投递", async () => {
    await setPrefs({ enabled: false });
    for (const k of ["reply", "askUser", "failure", "workflow", "schedule", "bgTask"] as const) {
      await expect(notifyNative({ ...PAYLOAD, notifyKind: k })).resolves.toBe(false);
    }
    expect(emit).not.toHaveBeenCalled();
  });

  it("分类开关关掉 → 只拦该分类，其余照发", async () => {
    // kinds_schema:2 = 已迁移过，这里的 false 是用户的真选择，migrateKinds
    // 不会再把它拉回 true（v1 的死开关默认值才会被迁移覆盖）
    await setPrefs({ enabled: true, kinds_schema: 2, kinds: { workflow: false } });
    await expect(notifyNative({ ...PAYLOAD, notifyKind: "workflow" })).resolves.toBe(false);
    await expect(notifyNative({ ...PAYLOAD, notifyKind: "schedule" })).resolves.toBe(true);
    expect(emit).toHaveBeenCalledTimes(1);
  });

  it("勿扰窗内 → 不投递", async () => {
    await setPrefs({ enabled: true, dnd: { enabled: true, from: "00:00", to: "23:59" } });
    await expect(notifyNative(PAYLOAD)).resolves.toBe(false);
    expect(emit).not.toHaveBeenCalled();
  });

  it("隐私 titleOnly → 正文被抹空，标题保留", async () => {
    await setPrefs({ enabled: true, privacy: "titleOnly" });
    await notifyNative(PAYLOAD);
    expect(lastPayload()).toMatchObject({ title: "⏸ 测试会话", body: "" });
  });

  it("隐私 full → 正文原样", async () => {
    await setPrefs({ enabled: true, privacy: "full" });
    await notifyNative(PAYLOAD);
    expect(lastPayload()).toMatchObject({ body: "回复完成" });
  });

  it("提示音由分类推导：failure → Basso，其余 → 用户选的音色", async () => {
    await setPrefs({ enabled: true, sound: true, sound_name: "Glass" });
    await notifyNative({ ...PAYLOAD, notifyKind: "failure" });
    expect(lastPayload()).toMatchObject({ sound: "Basso" });
    await notifyNative({ ...PAYLOAD, notifyKind: "reply" });
    expect(lastPayload()).toMatchObject({ sound: "Glass" });
  });

  it("提示音关掉 → 静音（sound 省略）", async () => {
    await setPrefs({ enabled: true, sound: false });
    await notifyNative(PAYLOAD);
    expect(lastPayload()?.sound).toBeUndefined();
  });

  it("调用点显式给的 sound 优先于推导", async () => {
    await setPrefs({ enabled: true, sound: true, sound_name: "Glass" });
    await notifyNative({ ...PAYLOAD, sound: "Ping" });
    expect(lastPayload()).toMatchObject({ sound: "Ping" });
  });

  // 设置页测试按钮是用户的显式动作，必须在任何偏好下都给出可见反馈，否则
  // 「按了没反应」会被误读成通知系统坏了。
  it("test 类绕过总开关 / 分类 / 勿扰 / 隐私", async () => {
    await setPrefs({
      enabled: false,
      privacy: "titleOnly",
      dnd: { enabled: true, from: "00:00", to: "23:59" },
    });
    await expect(notifyNative({ ...PAYLOAD, notifyKind: "test" })).resolves.toBe(true);
    expect(lastPayload()).toMatchObject({ body: "回复完成" }); // 正文未被抹空
  });

  it("test 类仍受窗口闸门约束（pin 窗不发）", async () => {
    await setPrefs({ enabled: true });
    stubWindow("/pin");
    await expect(notifyNative({ ...PAYLOAD, notifyKind: "test" })).resolves.toBe(false);
    expect(emit).not.toHaveBeenCalled();
  });

  it("载荷带上 notify_kind 供壳侧日志定位（旧壳 serde 忽略未知字段）", async () => {
    await setPrefs({ enabled: true });
    await notifyNative({ ...PAYLOAD, notifyKind: "schedule" });
    expect(lastPayload()).toMatchObject({ notify_kind: "schedule", kind: "session" });
  });
});

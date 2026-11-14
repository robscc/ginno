// lib/desktop 的通知所有者闸门（docs/notifications-audit-2026-11-14.md P0-3）。
//
// 悬浮窗 `/pin` 为了拿 hooks 也挂了 AppShell，而 AppShell 的所有 hook 都跑在
// `if (pathname === "/pin") return` 早退**之前**（:56 useNoticeNativeFallback、
// :522 schedule WS、store.tsx:309 useWorkflows 轮询）。pin 窗口建出来就是 hidden
// 且 hide 保活不销毁，它的 `visibilityState !== "visible"` 恒为真 —— 那三处
// 「用户没在看才弹」的前提在 pin 窗口永远成立，于是：用户正盯着主窗也照样弹，
// 主窗同时隐藏时同一事件还弹两条（模块级状态按窗口独立，无跨窗去重）。
//
// 闸门放在 notifyNative 这个**唯一出口**，一处覆盖全部站点（含将来新增的）。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { emit } = vi.hoisted(() => ({ emit: vi.fn(async () => {}) }));
vi.mock("@tauri-apps/api/event", () => ({ emit }));

import { isNotifyOwnerWindow, notifyNative, type NativeNotification } from "@/lib/desktop";

const PAYLOAD: NativeNotification = {
  kind: "session",
  id: "s1",
  title: "⏸ 测试会话",
  body: "等你回答——任务已挂起",
};

/** 伪装成 Tauri 壳内的窗口（isDesktop 认 __TAURI_INTERNALS__）。 */
function stubWindow(pathname: string) {
  vi.stubGlobal("window", {
    __TAURI_INTERNALS__: {},
    location: { pathname },
  });
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
  beforeEach(() => emit.mockClear());
  afterEach(() => vi.unstubAllGlobals());

  it("主窗：事件照常投递给壳", async () => {
    stubWindow("/");
    await expect(notifyNative(PAYLOAD)).resolves.toBe(true);
    expect(emit).toHaveBeenCalledWith("ginno:notify", PAYLOAD);
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

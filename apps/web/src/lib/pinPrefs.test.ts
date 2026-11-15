// 悬浮窗 pin 偏好（lib/pinPrefs.ts）单测。
//
// 模块级缓存 + read-modify-write 范式：桩掉 ./runtime 的 getSettings/putSettings
// 与 ./desktop 的 isDesktop、@tauri-apps/api/core 的 invoke，即可在 node 环境
// 精确控制「settings.json 文档 / 是否桌面壳 / 壳命令返回值」三个外部变量。
// 缓存复位：每个用例前用空文档 loadPinPrefs() 把模块缓存拉回 DEFAULT_FLOATING。
//
// 注：pinPrefs 不直接碰 localStorage（读写都走 runtime API），无需 webstorage 桩。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { invoke } = vi.hoisted(() => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke }));

const { isDesktop } = vi.hoisted(() => ({ isDesktop: vi.fn(() => false) }));
vi.mock("./desktop", () => ({ isDesktop }));

const { getSettings, putSettings } = vi.hoisted(() => ({
  getSettings: vi.fn(async () => ({}) as Record<string, unknown>),
  putSettings: vi.fn(async () => undefined),
}));
vi.mock("./runtime", () => ({ getSettings, putSettings }));

import {
  DEFAULT_FLOATING,
  eventToAccelerator,
  formatAccelerator,
  loadPinPrefs,
  pinPrefs,
  pushPinPrefsToShell,
  queryHotkeyActive,
  savePinPrefs,
} from "@/lib/pinPrefs";

/** 完整合法 wire 形状（settings.json 的 floating 键，snake_case）。 */
const FULL_WIRE = {
  enabled: false,
  hotkey: "  CommandOrControl+Alt+Space  ",
  default_mode: "follow",
  inactive_opacity: 0.5,
  visible_on_all_spaces: false,
  fullscreen_policy: "overlay",
  pill_click_through: true,
  show_on_launch: true,
};

/** 与 FULL_WIRE 对应的 camelCase 期望（hotkey 已 trim）。 */
const FULL_PARSED = {
  enabled: false,
  hotkey: "CommandOrControl+Alt+Space",
  defaultMode: "follow",
  inactiveOpacity: 0.5,
  visibleOnAllSpaces: false,
  fullscreenPolicy: "overlay",
  pillClickThrough: true,
  showOnLaunch: true,
};

/** 默认值的 snake_case wire 形状（pushPinPrefsToShell 的载荷期望）。 */
const DEFAULT_WIRE = {
  enabled: true,
  hotkey: "CommandOrControl+Shift+Space",
  default_mode: "quick",
  inactive_opacity: 0.7,
  visible_on_all_spaces: true,
  fullscreen_policy: "avoid",
  pill_click_through: false,
  show_on_launch: false,
};

beforeEach(async () => {
  invoke.mockReset();
  isDesktop.mockReset().mockReturnValue(false);
  getSettings.mockReset().mockResolvedValue({});
  putSettings.mockReset().mockResolvedValue(undefined);
  await loadPinPrefs(); // 空文档 → fromWire(undefined) → 缓存复位为默认值
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loadPinPrefs / fromWire", () => {
  it("初始缓存即默认值，pinPrefs() 同步可读", () => {
    expect(pinPrefs()).toEqual(DEFAULT_FLOATING);
  });

  it("完整合法 wire：逐字段映射且 hotkey 去首尾空白", async () => {
    getSettings.mockResolvedValue({ floating: FULL_WIRE });
    const p = await loadPinPrefs();
    expect(p).toEqual(FULL_PARSED);
    expect(pinPrefs()).toEqual(FULL_PARSED); // 同步缓存与返回值一致
  });

  it("非法字段逐个回落默认值（类型错/枚举错/空白 hotkey）", async () => {
    getSettings.mockResolvedValue({
      floating: {
        enabled: "yes",
        hotkey: "   ",
        default_mode: "bogus",
        inactive_opacity: 0.05,
        visible_on_all_spaces: 1,
        fullscreen_policy: "float",
        pill_click_through: "true",
        show_on_launch: null,
      },
    });
    expect(await loadPinPrefs()).toEqual(DEFAULT_FLOATING);
  });

  it("inactive_opacity 越界/非数字一律回落 0.7", async () => {
    for (const v of [0.05, 1.5, "0.5", NaN]) {
      getSettings.mockResolvedValue({ floating: { inactive_opacity: v } });
      expect((await loadPinPrefs()).inactiveOpacity).toBe(0.7);
    }
  });

  it("inactive_opacity 边界 0.1 与 1 均接受", async () => {
    for (const v of [0.1, 1]) {
      getSettings.mockResolvedValue({ floating: { inactive_opacity: v } });
      expect((await loadPinPrefs()).inactiveOpacity).toBe(v);
    }
  });

  it("floating 缺失/null/非对象 → 全默认", async () => {
    for (const floating of [undefined, null, "str", 42]) {
      getSettings.mockResolvedValue({ floating });
      expect(await loadPinPrefs()).toEqual(DEFAULT_FLOATING);
    }
  });

  it("sidecar 挂了（getSettings 抛错）→ 不抛异常，保持默认", async () => {
    getSettings.mockRejectedValue(new Error("sidecar down"));
    await expect(loadPinPrefs()).resolves.toEqual(DEFAULT_FLOATING);
    expect(pinPrefs()).toEqual(DEFAULT_FLOATING);
  });
});

describe("savePinPrefs", () => {
  it("部分更新合并进当前缓存，整文档写回且保留无关键", async () => {
    getSettings.mockResolvedValue({ notifications: { enabled: true } });
    const res = await savePinPrefs({ inactiveOpacity: 0.4, defaultMode: "follow" });
    expect(res.hotkeyOk).toBe(true); // 非桌面环境恒 true
    expect(res.prefs).toEqual({ ...DEFAULT_FLOATING, inactiveOpacity: 0.4, defaultMode: "follow" });
    expect(pinPrefs()).toEqual(res.prefs);
    expect(putSettings).toHaveBeenCalledTimes(1);
    expect(putSettings).toHaveBeenCalledWith({
      notifications: { enabled: true }, // 无关设置不被覆盖
      floating: { ...DEFAULT_WIRE, default_mode: "follow", inactive_opacity: 0.4 },
    });
  });

  it("写失败 → 异常上抛且缓存不更新（失败保存不撒谎）", async () => {
    putSettings.mockRejectedValue(new Error("disk full"));
    await expect(savePinPrefs({ enabled: false })).rejects.toThrow("disk full");
    expect(pinPrefs()).toEqual(DEFAULT_FLOATING);
  });

  it("桌面壳注册热键失败 → hotkeyOk=false 但值仍持久化", async () => {
    isDesktop.mockReturnValue(true);
    invoke.mockResolvedValue(false);
    const res = await savePinPrefs({ hotkey: "Alt+F9" });
    expect(res.hotkeyOk).toBe(false);
    expect(res.prefs.hotkey).toBe("Alt+F9");
    expect(putSettings).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith("pin_apply_prefs", {
      prefs: { ...DEFAULT_WIRE, hotkey: "Alt+F9" },
    });
  });

  it("连续保存：第二次合并基于第一次的结果", async () => {
    await savePinPrefs({ inactiveOpacity: 0.3 });
    const res = await savePinPrefs({ showOnLaunch: true });
    expect(res.prefs).toEqual({ ...DEFAULT_FLOATING, inactiveOpacity: 0.3, showOnLaunch: true });
  });
});

describe("pushPinPrefsToShell / queryHotkeyActive", () => {
  it("非桌面：两者都直接 true，不调 invoke", async () => {
    await expect(pushPinPrefsToShell()).resolves.toBe(true);
    await expect(queryHotkeyActive()).resolves.toBe(true);
    expect(invoke).not.toHaveBeenCalled();
  });

  it("桌面：push 透传壳的注册结果（true/false）", async () => {
    isDesktop.mockReturnValue(true);
    invoke.mockResolvedValueOnce(true);
    await expect(pushPinPrefsToShell()).resolves.toBe(true);
    expect(invoke).toHaveBeenCalledWith("pin_apply_prefs", { prefs: DEFAULT_WIRE });
    invoke.mockResolvedValueOnce(false);
    await expect(pushPinPrefsToShell()).resolves.toBe(false);
  });

  it("桌面：壳命令不存在（invoke 抛错）→ push 兜底 true", async () => {
    isDesktop.mockReturnValue(true);
    invoke.mockRejectedValue(new Error("unknown command"));
    await expect(pushPinPrefsToShell()).resolves.toBe(true);
  });

  it("桌面：queryHotkeyActive 透传 pin_hotkey_status 结果", async () => {
    isDesktop.mockReturnValue(true);
    invoke.mockResolvedValueOnce(false);
    await expect(queryHotkeyActive()).resolves.toBe(false);
    expect(invoke).toHaveBeenCalledWith("pin_hotkey_status");
  });

  it("桌面：queryHotkeyActive 抛错 → 兜底 true", async () => {
    isDesktop.mockReturnValue(true);
    invoke.mockRejectedValue(new Error("boom"));
    await expect(queryHotkeyActive()).resolves.toBe(true);
  });
});

// ── 热键录制器 ──────────────────────────────────────────────────────────

/** node 无 KeyboardEvent：被测函数只读 code + 四个修饰键布尔，平对象即可。 */
function key(
  code: string,
  mods: Partial<{ metaKey: boolean; ctrlKey: boolean; altKey: boolean; shiftKey: boolean }> = {},
): KeyboardEvent {
  return { code, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...mods } as unknown as KeyboardEvent;
}

describe("eventToAccelerator", () => {
  it("字母键：物理码 KeyG → G，meta 映射 CommandOrControl", () => {
    expect(eventToAccelerator(key("KeyG", { metaKey: true, shiftKey: true }))).toBe(
      "CommandOrControl+Shift+G",
    );
  });

  it("数字键：Digit1 → 1", () => {
    expect(eventToAccelerator(key("Digit1", { ctrlKey: true }))).toBe("Control+1");
  });

  it("F1..F24 可 standalone；F25 不在映射内 → null", () => {
    expect(eventToAccelerator(key("F5"))).toBe("F5");
    expect(eventToAccelerator(key("F24"))).toBe("F24");
    expect(eventToAccelerator(key("F1", { altKey: true }))).toBe("Alt+F1");
    expect(eventToAccelerator(key("F25"))).toBeNull();
  });

  it("Space 与标点键逐一映射", () => {
    const cases: Array<[string, string]> = [
      ["Space", "Space"],
      ["Minus", "-"],
      ["Equal", "="],
      ["BracketLeft", "["],
      ["BracketRight", "]"],
      ["Backslash", "\\"],
      ["Semicolon", ";"],
      ["Quote", "'"],
      ["Comma", ","],
      ["Period", "."],
      ["Slash", "/"],
      ["Backquote", "`"],
    ];
    for (const [code, sym] of cases) {
      expect(eventToAccelerator(key(code, { ctrlKey: true }))).toBe(`Control+${sym}`);
    }
  });

  it("方向键映射 Up/Down/Left/Right", () => {
    expect(eventToAccelerator(key("ArrowUp", { altKey: true }))).toBe("Alt+Up");
    expect(eventToAccelerator(key("ArrowDown", { altKey: true }))).toBe("Alt+Down");
    expect(eventToAccelerator(key("ArrowLeft", { altKey: true }))).toBe("Alt+Left");
    expect(eventToAccelerator(key("ArrowRight", { altKey: true }))).toBe("Alt+Right");
  });

  it("裸修饰键与未映射键 → null", () => {
    for (const code of ["ShiftLeft", "ControlRight", "AltLeft", "MetaRight", "Tab", "Enter", "CapsLock"]) {
      expect(eventToAccelerator(key(code, { shiftKey: true }))).toBeNull();
    }
  });

  it("无修饰的字母/数字/空格 → null（避免劫持全局输入）", () => {
    expect(eventToAccelerator(key("KeyA"))).toBeNull();
    expect(eventToAccelerator(key("Digit7"))).toBeNull();
    expect(eventToAccelerator(key("Space"))).toBeNull();
  });

  it("四修饰键全按：确定性顺序 CommandOrControl+Control+Alt+Shift", () => {
    expect(
      eventToAccelerator(key("KeyA", { metaKey: true, ctrlKey: true, altKey: true, shiftKey: true })),
    ).toBe("CommandOrControl+Control+Alt+Shift+A");
  });

  it("部分修饰键：顺序仍规范（Control 在 Alt 前）", () => {
    expect(eventToAccelerator(key("KeyK", { altKey: true, ctrlKey: true }))).toBe("Control+Alt+K");
    expect(eventToAccelerator(key("KeyK", { shiftKey: true, metaKey: true }))).toBe(
      "CommandOrControl+Shift+K",
    );
  });
});

describe("formatAccelerator", () => {
  it("默认热键 → Apple 修饰键顺序 ⇧⌘Space", () => {
    expect(formatAccelerator("CommandOrControl+Shift+Space")).toBe("⇧⌘Space");
  });

  it("输入顺序打乱也按 ⌃⌥⇧⌘ 规范输出", () => {
    expect(formatAccelerator("Shift+Alt+Control+CommandOrControl+A")).toBe("⌃⌥⇧⌘A");
  });

  it("空串/纯加号 → 原样返回", () => {
    expect(formatAccelerator("")).toBe("");
    expect(formatAccelerator("++")).toBe("++");
  });

  it("功能键与单字符：F5 原样，单字母大写", () => {
    expect(formatAccelerator("F5")).toBe("F5");
    expect(formatAccelerator("CommandOrControl+F5")).toBe("⌘F5");
    expect(formatAccelerator("Control+a")).toBe("⌃A");
  });

  it("方向键渲染为箭头符号", () => {
    expect(formatAccelerator("Alt+Up")).toBe("⌥↑");
    expect(formatAccelerator("Alt+Down")).toBe("⌥↓");
    expect(formatAccelerator("Alt+Left")).toBe("⌥←");
    expect(formatAccelerator("Alt+Right")).toBe("⌥→");
  });

  it("特殊键符号：Enter/Tab/Escape/Delete", () => {
    expect(formatAccelerator("CommandOrControl+Enter")).toBe("⌘⏎");
    expect(formatAccelerator("CommandOrControl+Tab")).toBe("⌘⇥");
    expect(formatAccelerator("CommandOrControl+Escape")).toBe("⌘⎋");
    expect(formatAccelerator("CommandOrControl+Delete")).toBe("⌘⌫");
  });

  it("未识别的多字符 token 原样透传", () => {
    expect(formatAccelerator("CommandOrControl+Foo")).toBe("⌘Foo");
  });
});

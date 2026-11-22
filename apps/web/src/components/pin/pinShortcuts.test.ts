// pinShortcuts — Pin 窗快捷键判定单测（Q8 IME Esc 收窗修复）。
//
// 钉住的行为：中文输入法合成期间（候选窗口激活）Esc 是「取消候选」，
// 不得把 pin 窗收成 pill；⌘↵ 同理不触发跳主窗。非合成 Esc 照常收窗。
// keyCode 229 分支：合成中部分引擎的 keydown 不置 isComposing、只给 229
// ——KeyboardEvent 构造器无法伪造 keyCode，所以这条路径只能在这里以
// plain-object 形状覆盖（e2e 里页面注入的合成事件 keyCode 恒 0）。

import { describe, expect, it } from "vitest";
import { isImeComposing, pinShortcutAction } from "./pinShortcuts";

const key = (over: Partial<Parameters<typeof pinShortcutAction>[0]>) =>
  ({ key: "", isComposing: false, keyCode: 0, metaKey: false, ctrlKey: false, ...over });

describe("isImeComposing", () => {
  it("isComposing=true → 合成中", () => {
    expect(isImeComposing(key({ key: "Escape", isComposing: true }))).toBe(true);
  });
  it("keyCode 229（旧引擎合成标记）→ 合成中，即使 isComposing 缺失/false", () => {
    expect(isImeComposing(key({ key: "Escape", keyCode: 229 }))).toBe(true);
    expect(isImeComposing(key({ key: "Escape", isComposing: false, keyCode: 229 }))).toBe(true);
  });
  it("普通 keydown（isComposing=false/undefined，keyCode 0）→ 非合成", () => {
    expect(isImeComposing(key({ key: "Escape" }))).toBe(false);
    expect(isImeComposing({ key: "Escape" })).toBe(false);
  });
});

describe("pinShortcutAction · IME 合成守卫（Q8）", () => {
  it("合成中 Esc（isComposing=true）→ null，不收窗", () => {
    expect(pinShortcutAction(key({ key: "Escape", isComposing: true }))).toBeNull();
  });
  it("合成中 Esc（keyCode=229 兜底路径）→ null，不收窗", () => {
    expect(pinShortcutAction(key({ key: "Escape", keyCode: 229 }))).toBeNull();
  });
  it("非合成 Esc → collapse（原有行为不变）", () => {
    expect(pinShortcutAction(key({ key: "Escape" }))).toBe("collapse");
  });
  it("合成中 ⌘Enter → null，不跳主窗", () => {
    expect(pinShortcutAction(key({ key: "Enter", isComposing: true, metaKey: true }))).toBeNull();
  });
  it("非合成 ⌘Enter / Ctrl+Enter → open-main（原有行为不变）", () => {
    expect(pinShortcutAction(key({ key: "Enter", metaKey: true }))).toBe("open-main");
    expect(pinShortcutAction(key({ key: "Enter", ctrlKey: true }))).toBe("open-main");
  });
  it("裸 Enter（IME 上屏键，无修饰）→ null，与原逻辑一致", () => {
    expect(pinShortcutAction(key({ key: "Enter" }))).toBeNull();
  });
  it("其它键 → null", () => {
    expect(pinShortcutAction(key({ key: "a" }))).toBeNull();
    expect(pinShortcutAction(key({ key: "Escape", metaKey: true }))).toBe("collapse");
  });
});

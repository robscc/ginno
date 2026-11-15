// mermaidPrefs 单测：localStorage 读写 + 默认值兜底 + 订阅/退订广播。
// node 环境无 localStorage（Node 25 虽暴露全局但 getItem 不是函数），
// 用 vi.stubGlobal 造内存桩；模块级 subs Set 用 vi.resetModules + 动态 import
// 每用例隔离，避免订阅残留互相污染。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Mod = typeof import("@/lib/mermaidPrefs");
const KEY = "ginno-mermaid";

let mod: Mod;
let store: Map<string, string>;
let getItem: ReturnType<typeof vi.fn>;
let setItem: ReturnType<typeof vi.fn>;

beforeEach(async () => {
  vi.resetModules();
  store = new Map();
  getItem = vi.fn((k: string) => store.get(k) ?? null);
  setItem = vi.fn((k: string, v: string) => {
    store.set(k, v);
  });
  vi.stubGlobal("localStorage", { getItem, setItem, removeItem: vi.fn() });
  mod = await import("@/lib/mermaidPrefs");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("readMermaidPref", () => {
  it("默认开：MERMAID_DEFAULT 为 true，未写入时读回 true", () => {
    expect(mod.MERMAID_DEFAULT).toBe(true);
    expect(mod.readMermaidPref()).toBe(true);
    expect(getItem).toHaveBeenCalledWith(KEY);
  });

  it('"0" → false，"1" → true', () => {
    store.set(KEY, "0");
    expect(mod.readMermaidPref()).toBe(false);
    store.set(KEY, "1");
    expect(mod.readMermaidPref()).toBe(true);
  });

  it("只有字面量 \"0\" 算关：其余任何非 null 值都读作开", () => {
    for (const raw of ["false", "off", "garbage", "00", " 0"]) {
      store.set(KEY, raw);
      expect(mod.readMermaidPref(), raw).toBe(true);
    }
  });

  it("quirk：空串读作开（raw !== \"0\" 对 \"\" 成立），固化当前行为", () => {
    store.set(KEY, "");
    expect(mod.readMermaidPref()).toBe(true);
  });

  it("localStorage 抛错（隐私模式）→ 退回默认 true，不向外抛", () => {
    getItem.mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(mod.readMermaidPref()).toBe(true);
  });
});

describe("writeMermaidPref", () => {
  it("true 写 \"1\"、false 写 \"0\"，键为 ginno-mermaid", () => {
    mod.writeMermaidPref(true);
    expect(setItem).toHaveBeenLastCalledWith(KEY, "1");
    mod.writeMermaidPref(false);
    expect(setItem).toHaveBeenLastCalledWith(KEY, "0");
    expect(setItem).toHaveBeenCalledTimes(2);
  });

  it("写后读回环一致", () => {
    mod.writeMermaidPref(false);
    expect(mod.readMermaidPref()).toBe(false);
    mod.writeMermaidPref(true);
    expect(mod.readMermaidPref()).toBe(true);
  });

  it("setItem 抛错被吞掉：不向外抛，且订阅方照常收到通知", () => {
    setItem.mockImplementation(() => {
      throw new Error("QuotaExceeded");
    });
    const fn = vi.fn();
    mod.subscribeMermaidPref(fn);
    expect(() => mod.writeMermaidPref(false)).not.toThrow();
    expect(fn).toHaveBeenCalledWith(false);
  });
});

describe("subscribeMermaidPref 广播", () => {
  it("写入立即同步回调，true/false 都送达", () => {
    const fn = vi.fn();
    mod.subscribeMermaidPref(fn);
    mod.writeMermaidPref(false);
    mod.writeMermaidPref(true);
    expect(fn.mock.calls).toEqual([[false], [true]]);
  });

  it("退订后不再收到通知；重复退订安全", () => {
    const fn = vi.fn();
    const unsub = mod.subscribeMermaidPref(fn);
    mod.writeMermaidPref(false);
    unsub();
    unsub(); // Set.delete 幂等
    mod.writeMermaidPref(true);
    expect(fn).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledWith(false);
  });

  it("多个订阅方都收到；其中一个抛错不拖垮其他", () => {
    const order: string[] = [];
    const bad = vi.fn(() => {
      order.push("bad");
      throw new Error("subscriber blew up");
    });
    const good = vi.fn(() => order.push("good"));
    mod.subscribeMermaidPref(bad);
    mod.subscribeMermaidPref(good);
    expect(() => mod.writeMermaidPref(false)).not.toThrow();
    expect(bad).toHaveBeenCalledWith(false);
    expect(good).toHaveBeenCalledWith(false);
    expect(order).toEqual(["bad", "good"]); // 抛错者在前，后者仍执行
  });

  it("快照语义：广播中退订他人，本轮仍送达（Array.from 快照）", () => {
    const b = vi.fn();
    let unsubB: () => void = () => {};
    const a = vi.fn(() => unsubB());
    mod.subscribeMermaidPref(a);
    unsubB = mod.subscribeMermaidPref(b);
    mod.writeMermaidPref(false);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1); // 快照已含 b，本轮照发
    mod.writeMermaidPref(true);
    expect(b).toHaveBeenCalledTimes(1); // 下一轮才真正缺席
  });

  it("快照语义：广播中新订阅者本轮不收，下一轮才收", () => {
    const late = vi.fn();
    const a = vi.fn(() => {
      mod.subscribeMermaidPref(late);
    });
    mod.subscribeMermaidPref(a);
    mod.writeMermaidPref(false);
    expect(late).not.toHaveBeenCalled();
    mod.writeMermaidPref(true);
    expect(late).toHaveBeenCalledWith(true);
  });

  it("无订阅方时写入不报错", () => {
    expect(() => mod.writeMermaidPref(true)).not.toThrow();
    expect(setItem).toHaveBeenCalledWith(KEY, "1");
  });

  it("模块隔离自检：本用例的订阅不会泄漏到下一用例（resetModules 生效）", () => {
    const fn = vi.fn();
    mod.subscribeMermaidPref(fn);
    mod.writeMermaidPref(false);
    expect(fn).toHaveBeenCalledTimes(1);
    // 下一用例 beforeEach 会 resetModules + 重新 import，fn 自然失效
  });
});

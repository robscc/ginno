import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  FONT_DEFAULT,
  FONT_FAMILY_ORDER,
  FONT_FAMILY_STACKS,
  FONT_SIZE_ORDER,
  FONT_SIZE_PX,
  applyFontPrefs,
  parseFontPrefs,
  readFontPrefs,
  writeFontPrefs,
} from "./fontPrefs";

describe("parseFontPrefs (pure validation)", () => {
  it("null / undefined / 非对象 → 默认", () => {
    expect(parseFontPrefs(null)).toEqual(FONT_DEFAULT);
    expect(parseFontPrefs(undefined)).toEqual(FONT_DEFAULT);
    expect(parseFontPrefs("serif")).toEqual(FONT_DEFAULT);
    expect(parseFontPrefs(42)).toEqual(FONT_DEFAULT);
  });

  it("合法值保留", () => {
    expect(parseFontPrefs({ family: "serif", size: "large" })).toEqual({
      family: "serif",
      size: "large",
    });
  });

  it("未知 family / size 回退默认（不崩）", () => {
    expect(parseFontPrefs({ family: "comic-sans", size: "huge" })).toEqual(FONT_DEFAULT);
  });

  it("部分字段：缺省补默认", () => {
    expect(parseFontPrefs({ size: "small" })).toEqual({ family: "inter", size: "small" });
    expect(parseFontPrefs({ family: "mono" })).toEqual({ family: "mono", size: "medium" });
  });

  it("类型错误字段回退", () => {
    expect(parseFontPrefs({ family: 123, size: true })).toEqual(FONT_DEFAULT);
  });
});

describe("constants integrity", () => {
  it("三档字号递增，medium=16（浏览器默认，不改既有观感）", () => {
    expect(FONT_SIZE_PX.small).toBeLessThan(FONT_SIZE_PX.medium);
    expect(FONT_SIZE_PX.medium).toBeLessThan(FONT_SIZE_PX.large);
    expect(FONT_SIZE_PX.medium).toBe(16);
    expect(FONT_SIZE_ORDER).toEqual(["small", "medium", "large"]);
  });

  it("四个字体族 stack 非空且与 order 一致", () => {
    expect(FONT_FAMILY_ORDER).toEqual(["inter", "system", "serif", "mono"]);
    for (const f of FONT_FAMILY_ORDER) {
      expect(FONT_FAMILY_STACKS[f].length).toBeGreaterThan(0);
    }
  });
});

describe("read / write / apply (mocked DOM + localStorage)", () => {
  let store: Record<string, string>;
  let rootStyle: { fontSize: string; setProperty: (k: string, v: string) => void } & Record<
    string,
    unknown
  >;

  beforeEach(() => {
    store = {};
    rootStyle = {
      fontSize: "",
      setProperty(k: string, v: string) {
        this[k] = v;
      },
    };
    (globalThis as Record<string, unknown>).localStorage = {
      getItem: (k: string) => (k in store ? store[k] : null),
      setItem: (k: string, v: string) => {
        store[k] = v;
      },
      removeItem: (k: string) => {
        delete store[k];
      },
    };
    (globalThis as Record<string, unknown>).document = { documentElement: { style: rootStyle } };
  });

  afterEach(() => {
    delete (globalThis as Record<string, unknown>).localStorage;
    delete (globalThis as Record<string, unknown>).document;
  });

  it("空存储 → 默认", () => {
    expect(readFontPrefs()).toEqual(FONT_DEFAULT);
  });

  it("write 持久化 + 立即 apply（字号 + 字体族变量）", () => {
    const merged = writeFontPrefs({ size: "large", family: "serif" });
    expect(merged).toEqual({ family: "serif", size: "large" });
    expect(JSON.parse(store["ginno-font-prefs"])).toEqual({ family: "serif", size: "large" });
    expect(rootStyle.fontSize).toBe("18px");
    expect(rootStyle["--app-font-family"]).toBe(FONT_FAMILY_STACKS.serif);
  });

  it("read roundtrip", () => {
    writeFontPrefs({ family: "mono", size: "small" });
    expect(readFontPrefs()).toEqual({ family: "mono", size: "small" });
    expect(rootStyle.fontSize).toBe("14px");
  });

  it("损坏 JSON → 默认（不崩）", () => {
    store["ginno-font-prefs"] = "{not json";
    expect(readFontPrefs()).toEqual(FONT_DEFAULT);
  });

  it("write 合并保留未改字段", () => {
    writeFontPrefs({ family: "serif" });
    const merged = writeFontPrefs({ size: "large" });
    expect(merged).toEqual({ family: "serif", size: "large" });
  });

  it("applyFontPrefs 无 document 时 no-op（SSR）", () => {
    delete (globalThis as Record<string, unknown>).document;
    expect(() => applyFontPrefs({ family: "inter", size: "medium" })).not.toThrow();
  });
});
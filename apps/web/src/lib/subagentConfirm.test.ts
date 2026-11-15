// subagentConfirm 单测：「已确认」本地态的读写、去重移尾、500 上限裁剪、
// 脏数据降级。node 环境用内存桩替代 localStorage；模块本身无缓存态
// （每次调用都读 storage），静态 import 即可，桩每用例重建。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isSubagentConfirmed, markSubagentConfirmed } from "@/lib/subagentConfirm";

const KEY = "ginno-subagent-confirmed";

let store: Map<string, string>;
let getItem: ReturnType<typeof vi.fn>;
let setItem: ReturnType<typeof vi.fn>;

/** 直接写入存储原始值（绕过被测写入路径，用于构造脏数据/预置态）。 */
function seed(raw: string) {
  store.set(KEY, raw);
}
function stored(): unknown {
  return JSON.parse(store.get(KEY) ?? "null");
}

beforeEach(() => {
  store = new Map();
  getItem = vi.fn((k: string) => store.get(k) ?? null);
  setItem = vi.fn((k: string, v: string) => {
    store.set(k, v);
  });
  vi.stubGlobal("localStorage", { getItem, setItem, removeItem: vi.fn() });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("isSubagentConfirmed 读取", () => {
  it("空存储：任何 id 都未确认", () => {
    expect(isSubagentConfirmed("s1")).toBe(false);
    expect(getItem).toHaveBeenCalledWith(KEY);
  });

  it("已写入的 id 返回 true，其余 false", () => {
    seed(JSON.stringify(["s1", "s2"]));
    expect(isSubagentConfirmed("s1")).toBe(true);
    expect(isSubagentConfirmed("s2")).toBe(true);
    expect(isSubagentConfirmed("s3")).toBe(false);
    expect(isSubagentConfirmed("")).toBe(false);
  });

  it("损坏 JSON → 降级为空，不抛错", () => {
    seed("{oops not json");
    expect(isSubagentConfirmed("s1")).toBe(false);
  });

  it("非数组 JSON（对象/字符串/数字/null）→ 一律视为空", () => {
    for (const raw of ['{"a":1}', '"s1"', "42", "null", "true"]) {
      seed(raw);
      expect(isSubagentConfirmed("s1"), raw).toBe(false);
    }
  });

  it("数组里的非字符串项被过滤：只认 string", () => {
    seed(JSON.stringify([1, "s1", null, { id: "s2" }, "s2", true]));
    expect(isSubagentConfirmed("s1")).toBe(true);
    expect(isSubagentConfirmed("s2")).toBe(true);
    expect(isSubagentConfirmed("1")).toBe(false);
    expect(isSubagentConfirmed("null")).toBe(false);
  });

  it("空串原始值按 falsy 处理 → 空列表（不做 JSON.parse）", () => {
    seed("");
    expect(isSubagentConfirmed("s1")).toBe(false);
  });

  it("getItem 抛错（隐私模式）→ false，不向外抛", () => {
    getItem.mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(isSubagentConfirmed("s1")).toBe(false);
  });
});

describe("markSubagentConfirmed 写入", () => {
  it("首次确认：写入单元素数组并持久化", () => {
    markSubagentConfirmed("s1");
    expect(setItem).toHaveBeenCalledWith(KEY, JSON.stringify(["s1"]));
    expect(stored()).toEqual(["s1"]);
    expect(isSubagentConfirmed("s1")).toBe(true);
  });

  it("重复确认同一 id：去重且移到末尾（最近确认排最后）", () => {
    seed(JSON.stringify(["a", "b", "c"]));
    markSubagentConfirmed("a");
    expect(stored()).toEqual(["b", "c", "a"]);
    markSubagentConfirmed("a");
    expect(stored()).toEqual(["b", "c", "a"]); // 幂等，不膨胀
  });

  it("多个不同 id 按写入序追加", () => {
    markSubagentConfirmed("x");
    markSubagentConfirmed("y");
    markSubagentConfirmed("z");
    expect(stored()).toEqual(["x", "y", "z"]);
  });

  it("写入时顺带清洗历史脏数据（非字符串项被滤掉）", () => {
    seed(JSON.stringify([1, "a", null, "b"]));
    markSubagentConfirmed("c");
    expect(stored()).toEqual(["a", "b", "c"]);
  });

  it("损坏 JSON 上写入 → 从空列表重建，不抛错", () => {
    seed("{broken");
    markSubagentConfirmed("s1");
    expect(stored()).toEqual(["s1"]);
  });

  it("setItem 抛错被吞：点击不报错，只是不记忆", () => {
    setItem.mockImplementation(() => {
      throw new Error("QuotaExceeded");
    });
    expect(() => markSubagentConfirmed("s1")).not.toThrow();
    expect(isSubagentConfirmed("s1")).toBe(false);
  });

  it("getItem 抛错但 setItem 可用 → 仍写入新列表", () => {
    getItem.mockImplementation(() => {
      throw new Error("SecurityError");
    });
    markSubagentConfirmed("s1");
    expect(stored()).toEqual(["s1"]);
  });
});

describe("MAX_ENTRIES = 500 上限", () => {
  const ids = (n: number, offset = 0) => Array.from({ length: n }, (_, i) => `s${i + offset}`);

  it("499 条 + 新 id → 恰好 500，不裁剪", () => {
    seed(JSON.stringify(ids(499)));
    markSubagentConfirmed("new");
    const list = stored() as string[];
    expect(list).toHaveLength(500);
    expect(list[0]).toBe("s0");
    expect(list[499]).toBe("new");
  });

  it("500 条 + 新 id → 裁掉最旧（写入序首位），新 id 在末尾", () => {
    seed(JSON.stringify(ids(500)));
    markSubagentConfirmed("new");
    const list = stored() as string[];
    expect(list).toHaveLength(500);
    expect(list[0]).toBe("s1"); // s0 被裁
    expect(list[499]).toBe("new");
    expect(list).not.toContain("s0");
  });

  it("500 条 + 已存在 id → 不膨胀，只移到末尾", () => {
    seed(JSON.stringify(ids(500)));
    markSubagentConfirmed("s0");
    const list = stored() as string[];
    expect(list).toHaveLength(500);
    expect(list[0]).toBe("s1");
    expect(list[499]).toBe("s0");
  });

  it("超限脏存储（600 条）写入 → 收敛到 500", () => {
    seed(JSON.stringify(ids(600)));
    markSubagentConfirmed("new");
    const list = stored() as string[];
    expect(list).toHaveLength(500);
    expect(list[0]).toBe("s101"); // 600+1 条取末 500 → 从 s101 起
    expect(list[499]).toBe("new");
  });
});

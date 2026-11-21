import { describe, expect, it } from "vitest";
import {
  canonRows,
  envFromRows,
  normValue,
  rowsChanged,
  rowsFromEnv,
  sameRows,
} from "./envVarsRows";

describe("normValue", () => {
  it("字符串原样透传（不二次 JSON 化）", () => {
    expect(normValue("http://127.0.0.1:7890")).toBe("http://127.0.0.1:7890");
    expect(normValue("null")).toBe("null");
  });
  it("非字符串 JSON 序列化", () => {
    expect(normValue(7890)).toBe("7890");
    expect(normValue(true)).toBe("true");
    expect(normValue(null)).toBe("null");
    expect(normValue({ a: 1 })).toBe('{"a":1}');
  });
});

describe("rowsFromEnv", () => {
  it("对象 → 行数组，保序", () => {
    expect(rowsFromEnv({ HTTPS_PROXY: "http://x", PATH: "/a" })).toEqual([
      { key: "HTTPS_PROXY", value: "http://x" },
      { key: "PATH", value: "/a" },
    ]);
  });
  it("非对象脏输入一律空表（null/数组/标量/undefined）", () => {
    for (const bad of [null, undefined, [], 42, "x", true]) {
      expect(rowsFromEnv(bad)).toEqual([]);
    }
  });
  it("非字符串值序列化进行内", () => {
    expect(rowsFromEnv({ N: 1, B: false })).toEqual([
      { key: "N", value: "1" },
      { key: "B", value: "false" },
    ]);
  });
});

describe("canonRows", () => {
  it("key 去首尾空白", () => {
    expect(canonRows([{ key: "  A  ", value: "1" }])).toEqual([
      { key: "A", value: "1" },
    ]);
  });
  it("丢弃空白 key 的行（含纯空格）", () => {
    expect(
      canonRows([
        { key: "", value: "x" },
        { key: "   ", value: "y" },
        { key: "A", value: "1" },
      ]),
    ).toEqual([{ key: "A", value: "1" }]);
  });
});

describe("sameRows / rowsChanged", () => {
  const base = [
    { key: "A", value: "1" },
    { key: "B", value: "2" },
  ];
  it("全等为真", () => {
    expect(sameRows(base, [...base])).toBe(true);
    expect(rowsChanged(base, base)).toBe(false);
  });
  it("值/键/长度/顺序差异均判脏", () => {
    expect(rowsChanged([{ key: "A", value: "9" }, base[1]], base)).toBe(true);
    expect(rowsChanged([{ key: "C", value: "1" }, base[1]], base)).toBe(true);
    expect(rowsChanged(base, [base[0]])).toBe(true);
    expect(rowsChanged([base[1], base[0]], base)).toBe(true);
  });
  it("只加一个空白行不算修改（无修改不可保存的关键判据）", () => {
    expect(rowsChanged([...base, { key: "", value: "" }], base)).toBe(false);
  });
  it("草稿 key 带首尾空白但内容一致 → 规范后不脏（trim 后写盘才是真修改）", () => {
    expect(rowsChanged([{ key: " A ", value: "1" }, base[1]], base)).toBe(
      false,
    );
  });
});

describe("envFromRows", () => {
  it("trim key、丢空 key 行、同名后者覆盖", () => {
    expect(
      envFromRows(
        [
          { key: " A ", value: "1" },
          { key: "", value: "drop" },
          { key: "A", value: "2" },
        ],
        {},
      ),
    ).toEqual({ A: "2" });
  });
  it("未触碰的 number/boolean 按原类型写回（保护已有 env）", () => {
    const rawPrev = { N: 7890, B: false, S: "keep" };
    expect(
      envFromRows(
        [
          { key: "N", value: "7890" },
          { key: "B", value: "false" },
          { key: "S", value: "keep" },
        ],
        rawPrev,
      ),
    ).toEqual({ N: 7890, B: false, S: "keep" });
  });
  it("动过的行按字符串写入", () => {
    expect(envFromRows([{ key: "N", value: "7891" }], { N: 7890 })).toEqual({
      N: "7891",
    });
  });
  it("新增 key 按字符串写入；rawPrev 缺该 key 时即使值相同也写字符串", () => {
    expect(envFromRows([{ key: "NEW", value: "7890" }], {})).toEqual({
      NEW: "7890",
    });
  });
  it("空 key 行不落盘", () => {
    expect(envFromRows([{ key: "", value: "" }], {})).toEqual({});
  });
});

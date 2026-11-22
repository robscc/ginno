/* 方案 D2：NodeInspector 结构化字段的 draft↔DSL 双向序列化（node 环境纯逻辑）。
 *
 * 契约（「绝不静默丢字段」）：
 * 1. 可表达域内 rows↔DSL 往返保真（含非字符串标量原类型保留、`{then}` 无 when 的 key 省略）；
 * 2. 不可表达 DSL（嵌套 args、带 transform 的 case）→ lossless:false；
 * 3. applyDraftFields 对 locked 字段原样保留（嵌套 args 编辑别的字段后仍原样）；
 * 4. `{{}}` 模板粗检只做语法级（不重复 dry-run 的语义校验）。 */
import { describe, expect, it } from "vitest";
import {
  applyDraftFields,
  argsToRows,
  casesToRows,
  condRowsToDraft,
  contextKeys,
  nodeIds,
  parseCondRows,
  parseRows,
  rowsToArgs,
  rowsToCases,
  rowsToDraft,
  templateIssues,
} from "./fieldCodecs";

const pretty = (v: unknown) => JSON.stringify(v, null, 2);

describe("args kv-map 往返", () => {
  it("扁平标量 args：rows→draft→rows 与 DSL→rows→draft→JSON 全程保真（快照）", () => {
    const dsl = {
      model_name: "{{model_name}}",
      aliyun_records: "{{context.aliyun_records}}",
      n: 3,
      flag: true,
      none: null,
      plain: "literal string",
      "": undefined as never, // 被 argsToRows 跳过的非法 key 形态不构造，占位删除
    };
    delete (dsl as Record<string, unknown>)[""];
    const { rows, lossless } = argsToRows(dsl);
    expect(lossless).toBe(true);
    // rows → DSL → 与原 DSL deepEqual（未编辑行保留 number/bool/null 原类型）
    expect(rowsToArgs(rows)).toEqual(dsl);
    // rows → draft(JSON) → parse 回 rows：字符串编辑链路保真
    const draft = rowsToDraft(rows);
    expect(parseRows(draft)).toEqual(rows);
    // draft 与 initialText 的 pretty 口径一致（避免组件假 dirty）
    expect(draft).toBe(pretty(dsl));
    // 序列化快照（对象口径；key 保持 DSL 插入序）
    expect(JSON.parse(draft)).toMatchInlineSnapshot(`
      {
        "aliyun_records": "{{context.aliyun_records}}",
        "flag": true,
        "model_name": "{{model_name}}",
        "n": 3,
        "none": null,
        "plain": "literal string",
      }
    `);
  });

  it("嵌套 args（对象/数组值）不可表达 → lossless:false + parseRows 锁定", () => {
    for (const bad of [{ a: { b: 1 } }, { a: [1, 2] }, { a: undefined }, [1, 2], "str", 5]) {
      expect(argsToRows(bad).lossless).toBe(false);
    }
    // 带 transform 等额外字段的 DSL parse 成 rows → null（渲染层据此锁定）
    expect(parseRows(pretty({ a: { b: 1 } }))).toBeNull();
    expect(parseRows("not json")).toBeNull();
  });

  it("未设置 / 空对象 / 半成品行在 draft 中往返保留（否则「+ 添加一行」立即被序列化吞掉）", () => {
    expect(argsToRows(undefined)).toEqual({ rows: [], lossless: true });
    expect(argsToRows({})).toEqual({ rows: [], lossless: true });
    expect(parseRows("")).toEqual([]);
    // 空 key 行：draft 保留（行编辑器需要往返），写回 DSL 时清理（见 applyDraftFields）
    const draft = rowsToDraft([{ key: "", value: "" }]);
    expect(parseRows(draft)).toEqual([{ key: "", value: "" }]);
  });

  it("改过的行退化为 string，未动的行保留原类型", () => {
    const { rows } = argsToRows({ n: 3, keep: true });
    const edited = rows.map((r) => (r.key === "n" ? { ...r, value: "42" } : r));
    expect(rowsToArgs(edited)).toEqual({ n: "42", keep: true });
  });
});

describe("cases cond-list 往返", () => {
  it("纯 {when,then} cases：rows↔DSL 往返保真，无 when 的 case 写回省略 key（快照）", () => {
    const cases = [{ when: "prs", then: "review" }, { then: "fallback" }, { when: "len(prs) > 3", then: "gate" }];
    const { rows, lossless } = casesToRows(cases);
    expect(lossless).toBe(true);
    expect(rowsToCases(rows)).toEqual(cases); // {then} 无 when 的 key 省略 → 与原字节级等价
    expect(casesToRows(rowsToCases(rows)).rows).toEqual(rows); // 再往返稳定
    const draft = condRowsToDraft(rows);
    expect(parseCondRows(draft)).toEqual(rows);
    expect(draft).toBe(pretty(cases));
    expect(JSON.parse(draft)).toMatchInlineSnapshot(`
      [
        {
          "then": "review",
          "when": "prs",
        },
        {
          "then": "fallback",
        },
        {
          "then": "gate",
          "when": "len(prs) > 3",
        },
      ]
    `);
  });

  it("带 transform / 额外 key 的 case 不可表达 → 锁定", () => {
    expect(casesToRows([{ when: "a", then: "b", transform: { fn: "f" } }]).lossless).toBe(false);
    expect(casesToRows([{ when: 1, then: "b" }]).lossless).toBe(false);
    expect(casesToRows({ when: "a" }).lossless).toBe(false);
    expect(casesToRows(undefined)).toEqual({ rows: [], lossless: true });
    expect(parseCondRows(pretty([{ when: "a", then: "b", transform: {} }]))).toBeNull();
    expect(parseCondRows("")).toEqual([]);
  });

  it("未选 then 的半成品行在 draft 往返保留，写回 DSL 时清理", () => {
    const draft = condRowsToDraft([{ when: "x", then: "" }]);
    expect(parseCondRows(draft)).toEqual([{ when: "x", then: "" }]);
    // 写回清理：then 为空的行绝不落库
    const next = applyDraftFields(
      { id: "b", type: "branch" },
      [{ key: "cases", kind: "cond-list" }],
      { cases: draft },
      new Set(),
    );
    expect(next.cases).toEqual([]);
    // kv-map 同理：空 key 行写回时剔除，正常行保留（含空串值——合法值不丢）
    const kv = applyDraftFields(
      { id: "p", type: "python" },
      [{ key: "args", kind: "kv-map" }],
      { args: rowsToDraft([{ key: "", value: "junk" }, { key: "a", value: "" }]) },
      new Set(),
    );
    expect(kv.args).toEqual({ a: "" });
  });
});

describe("applyDraftFields —— locked 字段绝不静默丢", () => {
  const fields = [
    { key: "title", kind: "text" } as const,
    { key: "entry", kind: "select" } as const,
    { key: "args", kind: "kv-map" } as const,
    { key: "timeout", kind: "number" } as const,
    { key: "cases", kind: "cond-list" } as const,
  ];

  it("locked 的嵌套 args / 带 transform cases 编辑其它字段后原样保留", () => {
    const node = {
      id: "p1",
      type: "python",
      title: "old",
      args: { nested: { deep: [1, 2] }, keep: "{{x}}" },
      cases: [{ when: "a", then: "b", transform: { fn: "f" } }],
    };
    const next = applyDraftFields(node, fields, { title: "new", args: "{}", cases: "[]" }, new Set(["args", "cases"]));
    expect(next.title).toBe("new"); // 未锁字段正常写
    expect(next.args).toEqual({ nested: { deep: [1, 2] }, keep: "{{x}}" }); // 原样，不被 "{}" 覆盖
    expect(next.cases).toEqual([{ when: "a", then: "b", transform: { fn: "f" } }]);
  });

  it("未锁字段按 kind 分派写回；空 draft 删 key", () => {
    const next = applyDraftFields(
      { id: "p", type: "python", title: "x", args: { a: 1 }, timeout: 5 },
      fields,
      { title: "", args: pretty({ a: "1" }), timeout: "30" },
      new Set(),
    );
    expect(next).toEqual({ id: "p", type: "python", args: { a: "1" }, timeout: 30 });
  });

  it("防御式：不合法 JSON / NaN 保留原值而不是 undefined", () => {
    const next = applyDraftFields(
      { id: "p", type: "python", args: { a: 1 }, timeout: 9 },
      fields,
      { args: "{oops", timeout: "NaN" },
      new Set(),
    );
    expect(next.args).toEqual({ a: 1 });
    expect(next.timeout).toBe(9);
  });
});

describe("templateIssues —— {{}} regex 粗检", () => {
  it("合法模板零告警", () => {
    expect(templateIssues("")).toEqual([]);
    expect(templateIssues("plain text")).toEqual([]);
    expect(templateIssues("{{model_name}}")).toEqual([]);
    expect(templateIssues("pre {{ context.x }} post {{y}} tail")).toEqual([]);
  });
  it("悬开 / 空 / 多余闭合各自成码", () => {
    expect(templateIssues("{{context.x")).toEqual([{ code: "unclosed" }]);
    expect(templateIssues("{{}}")).toEqual([{ code: "empty" }]);
    expect(templateIssues("{{ }}")).toEqual([{ code: "empty" }]);
    expect(templateIssues("a }} b")).toEqual([{ code: "stray" }]);
    expect(templateIssues("{{a}}{{")).toEqual([{ code: "unclosed" }]);
  });
});

describe("下拉数据源", () => {
  it("contextKeys = schema.properties ∪ initial，排序去重", () => {
    const dsl = {
      context: { schema: { type: "object", properties: { zeta: { type: "string" }, alpha: { type: "array" } } }, initial: { mid: [], alpha: 1 } },
      nodes: [],
    };
    expect(contextKeys(dsl)).toEqual(["alpha", "mid", "zeta"]);
    expect(contextKeys({})).toEqual([]);
    expect(contextKeys(undefined)).toEqual([]);
  });
  it("nodeIds 提取画布节点 id（忽略非法项）", () => {
    const dsl = { nodes: [{ id: "s1", type: "step" }, { id: "b1", type: "branch" }, { type: "loop" }, null] };
    expect(nodeIds(dsl)).toEqual(["s1", "b1"]);
    expect(nodeIds({})).toEqual([]);
  });
});

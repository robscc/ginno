/* 方案 D2：NodeInspector 结构化表单扩展的渲染接线与契约（node 环境、无 DOM——
 * 照 WorkflowPane.test.tsx 的源码断言风格；draft↔DSL 序列化往返在
 * fieldCodecs.test.ts 纯逻辑覆盖，浏览器级行为由 packaged UI e2e 覆盖）。
 *
 * 契约：
 * 1. python/branch/loop 节点按 DSL 真实面（dsl.py / python_node.py）生成
 *    select/kv-map/cond-list/node 字段；
 * 2. 锁定字段走 applyDraftFields(…, lockedKeys)——不写不删，绝不静默丢；
 * 3. JSON 高级模式一键切换（setJsonMode）+ entry 离线降级手输；
 * 4. 非 structured 字段 label 与输入控件保持直接兄弟（e2e 选择器契约）；
 * 5. 新增 i18n 键 en/zh 齐全、占位符一致。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { fieldsFor } from "./NodeInspector";

const SRC = readFileSync(fileURLToPath(new URL("./NodeInspector.tsx", import.meta.url)), "utf8");

const en = JSON.parse(readFileSync(fileURLToPath(new URL("../../../../messages/en/wf.json", import.meta.url)), "utf8"));
const zh = JSON.parse(
  readFileSync(fileURLToPath(new URL("../../../../messages/zh-CN/wf.json", import.meta.url)), "utf8"),
);

const spec = (node: Record<string, unknown>, key: string) =>
  fieldsFor({ id: "n", type: "x", ...node } as never).find((f) => f.key === key);

describe("fieldsFor —— DSL 真实面（方案 D2）", () => {
  it("python：entry(select) + args(kv-map) + writes(json) + timeout(number)", () => {
    const f = fieldsFor({ id: "p1", type: "python" });
    expect(f.find((x) => x.key === "entry")?.kind).toBe("select");
    expect(f.find((x) => x.key === "args")?.kind).toBe("kv-map");
    expect(f.find((x) => x.key === "writes")?.kind).toBe("json");
    expect(f.find((x) => x.key === "timeout")?.kind).toBe("number");
  });
  it("branch：cases(cond-list) + default(node)；loop：max_iters(number) + body(node)", () => {
    expect(spec({ type: "branch" }, "cases")?.kind).toBe("cond-list");
    expect(spec({ type: "branch" }, "default")?.kind).toBe("node");
    expect(spec({ type: "loop" }, "body")?.kind).toBe("node");
    expect(spec({ type: "loop" }, "max_iters")?.kind).toBe("number");
  });
  it("step 的既有字段面不受影响（goal/agent/writes/…）", () => {
    const keys = fieldsFor({ id: "s1", type: "step" }).map((f) => f.key);
    expect(keys).toEqual(["title", "goal", "agent", "writes", "extract_model", "timeout_s", "on_error", "retry"]);
  });
});

describe("渲染接线（源码断言）", () => {
  it("kv-map/cond-list 渲染行编辑器：draft 经 parseRows/parseCondRows 解析、rowsToDraft/condRowsToDraft 写回", () => {
    expect(SRC).toContain("<KvRowsEditor");
    expect(SRC).toContain("<CondRowsEditor");
    expect(SRC).toMatch(/rows=\{parseRows\(val\) \?\? \[\]\}/);
    expect(SRC).toMatch(/rows=\{parseCondRows\(val\) \?\? \[\]\}/);
    expect(SRC).toMatch(/\[f.key\]: rowsToDraft\(rows\)/);
    expect(SRC).toMatch(/\[f.key\]: condRowsToDraft\(rows\)/);
  });
  it("锁定字段：lockedKeys 判定 + applyDraftFields 原样保留 + lockedHint 提示切 JSON", () => {
    expect(SRC).toMatch(/lockedKeys\.has\(f\.key\)/); // 渲染层锁定分支
    expect(SRC).toMatch(/applyDraftFields\(/); // buildDraft 走纯逻辑核心（locked 跳过）
    expect(SRC).toContain('t("lockedHint", { field: f.label })');
  });
  it("JSON 高级模式一键切换：per-field jsonMode + Braces toggle 双向", () => {
    expect(SRC).toMatch(/setJsonMode\(\(m\) => \(\{ \.\.\.m, \[f\.key\]: !m\[f\.key\] \}\)\)/);
    expect(SRC).toMatch(/setJsonMode\(\(m\) => \(\{ \.\.\.m, \[f\.key\]: true \}\)\)/); // locked → 一键进 JSON
  });
  it("entry 白名单走只读端点；离线（pyEntries=null）降级手输 + entriesFallback 提示", () => {
    expect(SRC).toContain("api.listPythonEntries()");
    expect(SRC).toMatch(/f\.key === "entry" && f\.kind === "select" && !entryOpts/);
    expect(SRC).toContain('t("entriesFallback")');
  });
  it("then/default/body 节点下拉共用 datalist（WF_NODE_DATALIST）", () => {
    expect(SRC).toMatch(/list=\{WF_NODE_DATALIST\}/);
    expect(SRC).toMatch(/<datalist id=\{WF_NODE_DATALIST\}>/);
  });
  it("行编辑器的 value/when 走 templateIssues 粗检红边", () => {
    expect((SRC.match(/templateIssues\(/g) ?? []).length).toBeGreaterThanOrEqual(4);
  });
  it("非 structured 字段保持 label 直接相邻（`label:text-is(\"goal\") + textarea` e2e 契约不破坏）", () => {
    expect(SRC).toMatch(/<label className="mb-1 block font-mono text-\[10px\] uppercase tracking-wide text-faint">/);
  });
});

describe("i18n catalog —— 新增键 en/zh 齐全、占位符一致", () => {
  const NEW_KEYS = [
    "hintEntry",
    "hintArgs",
    "hintBody",
    "phArgKey",
    "phArgValue",
    "phCondWhen",
    "phCondThen",
    "phNode",
    "addRow",
    "removeRow",
    "insertCtx",
    "lockedHint",
    "useJson",
    "useForm",
    "entryNone",
    "entriesFallback",
    "tplIssueTitle",
  ];
  const placeholders = (s: string) => [...s.matchAll(/\{[a-z]+\}/g)].map((m) => m[0]).sort().join(",");

  for (const k of NEW_KEYS) {
    it(`wf.node.${k} 双语齐全且占位符一致`, () => {
      expect(en.wf.node[k], `en 缺 ${k}`).toBeTruthy();
      expect(zh.wf.node[k], `zh 缺 ${k}`).toBeTruthy();
      expect(placeholders(en.wf.node[k])).toBe(placeholders(zh.wf.node[k]));
    });
  }
  it("lockedHint 带 {field} 占位（渲染处传 field: f.label）", () => {
    expect(en.wf.node.lockedHint).toContain("{field}");
    expect(zh.wf.node.lockedHint).toContain("{field}");
  });
});

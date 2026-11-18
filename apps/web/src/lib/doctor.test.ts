/**
 * Config Doctor（配置健康检查）纯逻辑单测（node 环境，照 src/lib/*.test.ts 风格）。
 *
 * 分工说明：仓库 vitest 是 environment:"node" 无 DOM（见 vitest.config.ts 注释），
 * 渲染层（按钮触发 / 分组卡片 / 徽标着色）按仓库分工由 packaged UI e2e 覆盖；
 * 这里钉住可单测的三层——
 *   ① groupDoctorChecks：契约响应（error/warn/ok 混合 + 空 items + 缺面）→
 *      分组/计数/折叠数据，即 UI 渲染的全部输入；
 *   ② DOCTOR_FIX_HINT：后端 15 个 check id 全量映射 + 未映射回退；
 *   ③ i18n 双语完整性：zh-CN / en 两份 settings.json 的 doctor 子树 key
 *      与映射对齐（IntlError 教训：嵌套 key，禁带点扁平 key）；
 *   ④ 读组件源码钉住 UI 契约（照 modelChipRemote.test.tsx 范式）：
 *      按钮触发 getDoctor、ok 折叠行、降级文案、组件确实挂载进诊断页。
 *
 * 网络层（getDoctor 的 404/shape/network 降级）在 api/sessions.test.ts。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  DOCTOR_FIX_HINT,
  DOCTOR_SURFACES,
  doctorFixHintKey,
  groupDoctorChecks,
  isDoctorReport,
  type DoctorCheck,
} from "./doctor";
import zhSettings from "../../messages/zh-CN/settings.json";
import enSettings from "../../messages/en/settings.json";

const COMPONENT = readFileSync(
  fileURLToPath(new URL("../components/settings/DiagnosticsSettings.tsx", import.meta.url)),
  "utf8",
);

/** 目标：goal 契约给出的全部 15 个 check id（后端五面）。 */
const CONTRACT_IDS = [
  "mcp.json-parse",
  "mcp.command-missing",
  "mcp.empty-config",
  "skills.frontmatter",
  "skills.name-collision",
  "skills.broken-ref",
  "hooks.matcher-invalid",
  "hooks.script-missing",
  "hooks.policy-shadowed",
  "settings.unknown-keys",
  "settings.permission-ignored",
  "runtime.homebrew-path",
  "runtime.disk-space",
  "runtime.writable",
  "runtime.dev-override",
] as const;

describe("groupDoctorChecks：契约 → 分组数据", () => {
  it("五面混合（error/warn/ok）：按固定顺序分组、计数正确、failing error 优先", () => {
    const checks: DoctorCheck[] = [
      { surface: "runtime", id: "runtime.disk-space", level: "warn", items: [{ name: "/", detail: "剩 1.2GB" }] },
      { surface: "mcp", id: "mcp.command-missing", level: "error", items: [{ name: "trending", detail: "npx 不在 PATH" }] },
      { surface: "mcp", id: "mcp.empty-config", level: "ok", items: [] },
      { surface: "skills", id: "skills.frontmatter", level: "warn", items: [{ name: "foo/SKILL.md", detail: "缺 description" }] },
      { surface: "runtime", id: "runtime.homebrew-path", level: "error", items: [{ name: "ffmpeg", detail: "not found" }] },
      { surface: "hooks", id: "hooks.script-missing", level: "ok", items: [] },
      { surface: "settings", id: "settings.unknown-keys", level: "ok", items: [] },
    ];
    const groups = groupDoctorChecks(checks);
    // 固定顺序 = DOCTOR_SURFACES 顺序，而非返回顺序（runtime 在最前传入）
    expect(groups.map((g) => g.surface)).toEqual(["mcp", "skills", "hooks", "settings", "runtime"]);

    const mcp = groups[0];
    expect(mcp.error).toBe(1);
    expect(mcp.warn).toBe(0);
    expect(mcp.okCount).toBe(1); // ok 折叠为「✓ 1 项通过」的数据源
    expect(mcp.failing.map((c) => c.id)).toEqual(["mcp.command-missing"]);

    const runtime = groups[4];
    expect(runtime.error).toBe(1);
    expect(runtime.warn).toBe(1);
    expect(runtime.okCount).toBe(0);
    // error 优先于 warn（传入顺序 warn 在前）
    expect(runtime.failing.map((c) => c.id)).toEqual(["runtime.homebrew-path", "runtime.disk-space"]);
  });

  it("非 ok 但 items 为空：保留 check 不炸，渲染层走「无明细」分支", () => {
    const groups = groupDoctorChecks([
      { surface: "mcp", id: "mcp.json-parse", level: "error", items: [] },
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].failing).toHaveLength(1);
    expect(groups[0].failing[0].items).toEqual([]);
  });

  it("缺面（后端只返回 3 面）：只渲染出现的面；空数组/未知 surface 容错", () => {
    const groups = groupDoctorChecks([
      { surface: "mcp", id: "mcp.empty-config", level: "ok", items: [] },
      { surface: "runtime", id: "runtime.writable", level: "ok", items: [] },
      { surface: "extensions", id: "x.new-surface", level: "warn", items: [] }, // 未来新增面：丢弃
    ]);
    expect(groups.map((g) => g.surface)).toEqual(["mcp", "runtime"]);
    expect(groupDoctorChecks([])).toEqual([]);
    expect(groupDoctorChecks(null)).toEqual([]);
    expect(groupDoctorChecks(undefined)).toEqual([]);
  });

  it("全 ok：每组 failing 空、okCount 正——UI 全绿折叠态的数据基础", () => {
    const groups = groupDoctorChecks(
      DOCTOR_SURFACES.map((surface, i) => ({ surface, id: CONTRACT_IDS[i], level: "ok" as const, items: [] })),
    );
    expect(groups).toHaveLength(5);
    for (const g of groups) {
      expect(g.error).toBe(0);
      expect(g.warn).toBe(0);
      expect(g.failing).toEqual([]);
      expect(g.okCount).toBe(1);
    }
  });
});

describe("DOCTOR_FIX_HINT：15 个契约 id 全覆盖 + 回退", () => {
  it("goal 契约清单逐个有映射（防后端加 id 前端漏文案）", () => {
    for (const id of CONTRACT_IDS) {
      expect(doctorFixHintKey(id), `missing mapping for ${id}`).toBeTypeOf("string");
    }
    expect(Object.keys(DOCTOR_FIX_HINT)).toHaveLength(CONTRACT_IDS.length);
  });

  it("未映射 id 回退 null（组件显示 id 本身）", () => {
    expect(doctorFixHintKey("mcp.brand-new-check")).toBeNull();
    expect(doctorFixHintKey("")).toBeNull();
  });
});

describe("isDoctorReport：契约形状守卫", () => {
  const valid = {
    ok: true,
    ran_at: "2026-11-19T10:00:00Z",
    summary: { error: 0, warn: 0, total: 15 },
    checks: [],
  };
  it("合法契约体通过", () => {
    expect(isDoctorReport(valid)).toBe(true);
  });
  it("FastAPI 404 body / ok:false / 缺字段均拒绝", () => {
    expect(isDoctorReport({ detail: "Not Found" })).toBe(false);
    expect(isDoctorReport({ ...valid, ok: false })).toBe(false);
    expect(isDoctorReport({ ...valid, summary: undefined })).toBe(false);
    expect(isDoctorReport({ ...valid, checks: "nope" })).toBe(false);
    expect(isDoctorReport(null)).toBe(false);
  });
});

describe("i18n 双语完整性（settings.diagnostics.doctor 子树）", () => {
  type Json = Record<string, unknown>;

  function doctorNode(locale: "zh" | "en"): Json {
    const settings = (locale === "zh" ? zhSettings : enSettings).settings as Json;
    const diag = settings.diagnostics as Json;
    return diag.doctor as Json;
  }

  /** 递归收集全部叶子路径（断言嵌套结构本身——带点扁平 key 会原样出现在路径里）。 */
  function leafPaths(node: unknown, prefix = ""): string[] {
    if (node && typeof node === "object") {
      return Object.entries(node).flatMap(([k, v]) => leafPaths(v, `${prefix}${k}.`));
    }
    return [prefix.replace(/\.$/, "")];
  }

  const zh = doctorNode("zh");
  const en = doctorNode("en");

  it("zh / en key 树完全一致（漏 key 会运行时回退或 IntlError）", () => {
    expect(leafPaths(en).sort()).toEqual(leafPaths(zh).sort());
  });

  it("fixes.* 覆盖全部 15 个映射 key 且双语非空", () => {
    const zhFixes = zh.fixes as Json;
    const enFixes = en.fixes as Json;
    for (const key of Object.values(DOCTOR_FIX_HINT)) {
      expect(typeof zhFixes[key], `zh fixes.${key}`).toBe("string");
      expect((zhFixes[key] as string).length).toBeGreaterThan(0);
      expect(typeof enFixes[key], `en fixes.${key}`).toBe("string");
      expect((enFixes[key] as string).length).toBeGreaterThan(0);
    }
  });

  it("surfaces 五面 / levels 三级 / 折叠与降级文案 key 齐全", () => {
    for (const s of DOCTOR_SURFACES) {
      expect(typeof (zh.surfaces as Json)[s]).toBe("string");
      expect(typeof (en.surfaces as Json)[s]).toBe("string");
    }
    for (const l of ["ok", "warn", "error"] as const) {
      expect(typeof (zh.levels as Json)[l]).toBe("string");
    }
    for (const k of ["title", "subtitle", "description", "run", "running", "rerun", "ranAt", "unavailable", "summaryError", "summaryWarn", "summaryTotal", "allPass", "noItems"]) {
      expect(typeof zh[k], `zh doctor.${k}`).toBe("string");
      expect(typeof en[k], `en doctor.${k}`).toBe("string");
    }
  });

  it("禁止带点扁平 key（IntlError 教训）：doctor 子树所有 key 不含 '.'", () => {
    const hasDottedKey = (node: unknown): boolean => {
      if (node && typeof node === "object") {
        return (
          Object.keys(node).some((k) => k.includes(".")) ||
          Object.values(node).some((v) => hasDottedKey(v))
        );
      }
      return false;
    };
    expect(hasDottedKey(zh)).toBe(false);
    expect(hasDottedKey(en)).toBe(false);
  });
});

describe("组件契约（读源码钉住，渲染真实性由 packaged UI e2e 覆盖）", () => {
  it("诊断页挂载 DoctorPanel 且按钮触发 getDoctor", () => {
    expect(COMPONENT).toContain("<DoctorPanel />");
    expect(COMPONENT).toContain("await api.getDoctor()");
  });

  it("ok 折叠行 / 降级 / ran_at / 修复提示回退都接线", () => {
    expect(COMPONENT).toContain('t("allPass", { n: g.okCount })');
    expect(COMPONENT).toContain('t("unavailable")');
    expect(COMPONENT).toContain('t("ranAt"');
    expect(COMPONENT).toContain("return key ? t(`fixes.${key}`) : id;");
  });

  it("level 三色徽标样式表存在（error 红 / warn 琥珀 / ok 绿）", () => {
    expect(COMPONENT).toContain('error: "bg-red/15 text-red"');
    expect(COMPONENT).toContain('warn: "bg-yellow/15 text-yellow"');
    expect(COMPONENT).toContain('ok: "bg-green/15 text-green"');
  });
});

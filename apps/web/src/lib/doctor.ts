/**
 * Config Doctor（配置健康检查）纯逻辑层：契约类型 + surface 分组 + 修复提示映射。
 *
 * 后端 GET /api/doctor 返回五面检查（mcp/skills/hooks/settings/runtime），
 * 每面若干 check（id + level + items）。本模块只做数据整形，不碰网络、
 * 不碰 React——渲染在 DiagnosticsSettings 的 Doctor 区，网络在 api/sessions
 * 的 getDoctor（node 环境可直接单测，照 A2aInboundCard 纯逻辑范式）。
 *
 * 兼容性纪律（学 ISSUE_LABEL）：未映射的 check id 回退显示 id 本身，前端
 * 不必与后端同步发版；未知 surface/level 一律容错跳过或归为 unknown。
 */

// ---- 契约类型（与后端 GET /api/doctor 固定契约一一对应，勿改字段名） ----

export type DoctorLevel = "ok" | "warn" | "error";

export interface DoctorCheckItem {
  name: string;
  detail: string;
}

export interface DoctorCheck {
  surface: string; // 契约为五面之一，宽进严出：未知面 groupDoctorChecks 会丢弃
  id: string;
  level: DoctorLevel;
  items: DoctorCheckItem[];
}

export interface DoctorReport {
  ok: boolean;
  ran_at: string; // ISO
  summary: { error: number; warn: number; total: number };
  checks: DoctorCheck[];
}

// ---- surface 顺序（渲染顺序 = 该数组顺序） ----

export const DOCTOR_SURFACES = ["mcp", "skills", "hooks", "settings", "runtime"] as const;
export type DoctorSurface = (typeof DOCTOR_SURFACES)[number];

// ---- check id → 修复提示 i18n key（相对 settings.diagnostics.doctor 命名空间） ----
// 覆盖后端契约全部 15 个 id；新增 id 前端未映射时回退显示 id 本身。
export const DOCTOR_FIX_HINT = {
  "mcp.json-parse": "mcp_json_parse",
  "mcp.command-missing": "mcp_command_missing",
  "mcp.empty-config": "mcp_empty_config",
  "skills.frontmatter": "skills_frontmatter",
  "skills.name-collision": "skills_name_collision",
  "skills.broken-ref": "skills_broken_ref",
  "hooks.matcher-invalid": "hooks_matcher_invalid",
  "hooks.script-missing": "hooks_script_missing",
  "hooks.policy-shadowed": "hooks_policy_shadowed",
  "settings.unknown-keys": "settings_unknown_keys",
  "settings.permission-ignored": "settings_permission_ignored",
  "runtime.homebrew-path": "runtime_homebrew_path",
  "runtime.disk-space": "runtime_disk_space",
  "runtime.writable": "runtime_writable",
  "runtime.dev-override": "runtime_dev_override",
} as const;

/** 未映射返回 null（调用方回退显示 id 本身）。 */
export type DoctorFixKey = (typeof DOCTOR_FIX_HINT)[keyof typeof DOCTOR_FIX_HINT];
export function doctorFixHintKey(id: string): DoctorFixKey | null {
  return (DOCTOR_FIX_HINT as Record<string, DoctorFixKey>)[id] ?? null;
}

// ---- 分组 ----

export interface DoctorGroup {
  surface: DoctorSurface;
  /** 该组 error / warn 级 check 数（组标题徽标用）。 */
  error: number;
  warn: number;
  /** ok 级 check 数（折叠为「✓ N 项通过」一行）。 */
  okCount: number;
  /** 非 ok 的 check，error 在前 warn 在后（稳定排序）。 */
  failing: DoctorCheck[];
}

/** checks → 按固定 surface 顺序分组；只输出实际出现的面；未知 surface 丢弃。 */
export function groupDoctorChecks(checks: DoctorCheck[] | null | undefined): DoctorGroup[] {
  const by = new Map<DoctorSurface, DoctorGroup>();
  for (const c of checks ?? []) {
    if (!c || typeof c !== "object") continue;
    const surface = c.surface as DoctorSurface;
    if (!(DOCTOR_SURFACES as readonly string[]).includes(surface)) continue;
    let g = by.get(surface);
    if (!g) {
      g = { surface, error: 0, warn: 0, okCount: 0, failing: [] };
      by.set(surface, g);
    }
    if (c.level === "error") {
      g.error += 1;
      g.failing.push(c);
    } else if (c.level === "warn") {
      g.warn += 1;
      g.failing.push(c);
    } else {
      g.okCount += 1;
    }
  }
  for (const g of by.values()) {
    // error 优先于 warn；同级别保持后端返回顺序（sort 稳定）
    g.failing.sort((a, b) => (a.level === "error" ? 0 : 1) - (b.level === "error" ? 0 : 1));
  }
  return DOCTOR_SURFACES.filter((s) => by.has(s)).map((s) => by.get(s)!);
}

/** 契约形状守卫：ok 且 checks/summary 结构完整才算可用（后端未实现时
 *  404 页/HTML 会被 r.json() 炸或解析出非契约对象，统一走不可用降级）。 */
export function isDoctorReport(v: unknown): v is DoctorReport {
  if (!v || typeof v !== "object") return false;
  const r = v as Partial<DoctorReport>;
  return (
    r.ok === true &&
    typeof r.ran_at === "string" &&
    !!r.summary &&
    typeof r.summary.error === "number" &&
    typeof r.summary.warn === "number" &&
    typeof r.summary.total === "number" &&
    Array.isArray(r.checks)
  );
}

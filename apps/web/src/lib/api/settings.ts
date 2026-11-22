/**
 * Settings integrity & per-key env CAS client
 * （契约 /tmp/ginno-settings-integrity-contract.md，后端 api/env_settings.py）。
 *
 * - `GET /api/settings/env`：每键视图——普通键回明文 value，敏感键只回
 *   元数据（set/kind/url）+ version token（CAS 令牌，与值内容无关）。
 * - `PATCH /api/settings/env`：全事务每键 CAS。本客户端**不抛错**而是返回
 *   判别联合（EnvPatchResult）：调用方需要按 error 分支走不同 UI
 *   （409 冲突保草稿 / 409 损坏横幅 / 400 提示 / 500 磁盘问题），
 *   抛错模型会把这些语义压扁成字符串。
 * - `GET /api/settings/health`：三态健康（ok/missing/corrupt），驱动
 *   损坏横幅（SettingsCorruptBanner）。
 * - `POST /api/settings/backup` / `PUT /api/settings/repair`：安全恢复
 *   流程（非破坏备份 + 用户显式发起的修复，服务端落盘前自动 pre-repair
 *   备份）。前端绝不自动调用 repair——只在横幅上用户显式点击。
 */

import { currentLocale } from "../../i18n/provider";
import { BASE, H } from "./core";

// ---- 类型（契约 §1/§2） --------------------------------------------------- //

export type EnvKind = "plain" | "secret-name" | "url-with-auth";

export interface EnvUrlMeta {
  scheme: string;
  host?: string;
  has_credentials: boolean;
}

export interface EnvEntryView {
  sensitive: boolean;
  kind: EnvKind;
  /** 每键 CAS token；null = 尚无 token（IO 降级视图或创建语义）。 */
  version: string | null;
  /** plain 键的明文值（敏感键响应体全文不含值，此字段恒缺省）。 */
  value?: unknown;
  /** 敏感键：已设置。 */
  set?: boolean;
  /** url-with-auth 的非秘密元数据（scheme/host，已剥 userinfo）。 */
  url?: EnvUrlMeta | null;
}

export interface EnvViewState {
  status: "ok" | "corrupt";
  entries: Record<string, EnvEntryView>;
  parseError?: string;
}

export interface EnvConflict {
  key: string;
  current_version: string | null;
}

export type EnvUpdateOp =
  | { key: string; version: string | null; value: string | number | boolean }
  | { key: string; version: string | null; remove: true };

export type EnvPatchResult =
  | { ok: true; view: EnvViewState }
  | { ok: false; error: "version_conflict"; conflicts: EnvConflict[]; detail: string }
  | { ok: false; error: "settings_corrupt"; detail: string }
  | { ok: false; error: "invalid_request"; detail: string }
  | { ok: false; error: "write_failed"; detail: string }
  | { ok: false; error: "network" };

export interface SettingsHealth {
  status: "ok" | "missing" | "corrupt";
  error: string | null;
  path: string;
  size?: number;
  mtime?: number;
}

export interface BackupResult {
  ok: boolean;
  path?: string;
  error?: string;
}

export interface RepairResult {
  ok: boolean;
  /** 修复成功：落盘前自动做的 pre-repair 备份路径。 */
  backup?: string | null;
  restart_required?: boolean;
  error?: string;
  detail?: string;
}

// ---- 内部：带状态码的原生 fetch（错误体要按 error 字段分流，不能走 jsonStrict） ---- //

async function call(
  method: string,
  path: string,
  body?: unknown,
): Promise<{ status?: number; ok?: boolean; data: Record<string, unknown> | null }> {
  const r = await fetch(`${BASE}${path}`, {
    method,
    headers: { ...H, "X-Ginno-Language": currentLocale() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data: Record<string, unknown> | null = null;
  try {
    data = (await r.json()) as Record<string, unknown>;
  } catch {
    /* 非 JSON 响应体（如代理错误页） */
  }
  return { status: r.status, ok: r.ok, data };
}

const str = (v: unknown, fb = ""): string => (typeof v === "string" ? v : fb);

/** 响应体 → 视图（corrupt 如实透传 parseError；坏形状降级为空 entries）。 */
function normalizeView(data: Record<string, unknown> | null): EnvViewState {
  if (data && data.status === "corrupt") {
    return { status: "corrupt", entries: {}, parseError: str(data.parseError) };
  }
  const entries =
    data && typeof data.entries === "object" && data.entries !== null && !Array.isArray(data.entries)
      ? (data.entries as Record<string, EnvEntryView>)
      : {};
  return { status: "ok", entries };
}

// ---- env 每键 CAS（契约 §2/§3） ------------------------------------------ //

/** null = 网络失败/非 2xx（调用方走 loadFailed，绝不把错误体当视图）。
 * 损坏是 200 + status:"corrupt"（后端如实呈现，不拒读）。 */
export async function getEnvSettings(): Promise<EnvViewState | null> {
  try {
    const { ok, data } = await call("GET", "/settings/env");
    if (!ok) return null;
    return normalizeView(data);
  } catch {
    return null;
  }
}

export async function patchEnvSettings(updates: EnvUpdateOp[]): Promise<EnvPatchResult> {
  let res: Awaited<ReturnType<typeof call>>;
  try {
    res = await call("PATCH", "/settings/env", { updates });
  } catch {
    return { ok: false, error: "network" };
  }
  const { status, ok, data } = res;
  if (ok) return { ok: true, view: normalizeView(data) };
  const error = str(data?.error);
  const detail = str(data?.detail, `HTTP ${status ?? "?"}`);
  if (status === 409 && error === "version_conflict") {
    const conflicts = Array.isArray(data?.conflicts)
      ? (data.conflicts as EnvConflict[]).map((c) => ({
          key: str(c?.key),
          current_version: typeof c?.current_version === "string" ? c.current_version : null,
        }))
      : [];
    return { ok: false, error: "version_conflict", conflicts, detail };
  }
  if (status === 409) return { ok: false, error: "settings_corrupt", detail };
  if (status === 400) return { ok: false, error: "invalid_request", detail };
  return { ok: false, error: "write_failed", detail };
}

// ---- 三态健康与安全恢复（契约 §1/§5） ------------------------------------ //

/** null = 网络失败（横幅静默不渲染；损坏判定需要可靠证据）。 */
export async function getSettingsHealth(): Promise<SettingsHealth | null> {
  try {
    const { data } = await call("GET", "/settings/health");
    if (!data || typeof data !== "object") return null;
    const status = data.status;
    if (status !== "ok" && status !== "missing" && status !== "corrupt") return null;
    return {
      status,
      error: typeof data.error === "string" ? data.error : null,
      path: str(data.path),
    };
  } catch {
    return null;
  }
}

export async function backupSettings(): Promise<BackupResult> {
  try {
    const { data } = await call("POST", "/settings/backup");
    if (data?.ok === true) return { ok: true, path: str(data.path) };
    return { ok: false, error: str(data?.error, "backup failed") };
  } catch {
    return { ok: false, error: "runtime unreachable" };
  }
}

/** 用户显式发起的修复（唯一损坏态写通道）；body 必须是完整 JSON object。 */
export async function repairSettings(doc: Record<string, unknown>): Promise<RepairResult> {
  try {
    const { ok, data } = await call("PUT", "/settings/repair", doc);
    if (ok && data?.ok === true) {
      return {
        ok: true,
        backup: typeof data.backup === "string" ? data.backup : null,
        restart_required: data.restart_required === true,
      };
    }
    return {
      ok: false,
      error: str(data?.error, "repair failed"),
      detail: str(data?.detail),
    };
  } catch {
    return { ok: false, error: "runtime unreachable" };
  }
}

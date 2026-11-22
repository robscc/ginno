/**
 * EnvVarsSettings 的纯逻辑层（node 环境可单测，无 React/DOM）——CAS 版。
 *
 * 后端契约（/tmp/ginno-settings-integrity-contract.md §2/§3）：
 * GET /api/settings/env 回每键视图（敏感键只回元数据 + version token，
 * 值永不回传）；PATCH /api/settings/env 是全事务每键 CAS。本模块负责：
 *
 *  - 视图 entries ↔ 编辑行互转（敏感行值恒空串，绝不回填）；
 *  - 本地敏感预判（键名/值形态，镜像后端 env_store 的正则）——新增行在
 *    保存前就能以 password 输入呈现；
 *  - 脏判定与 PATCH updates 构造：按「最终意图 vs 基线」diff，每键至多
 *    一条 op（后端同请求同键两次 → 400），未触碰键不发（服务端保留
 *    原值与类型，number/boolean 不会被字符串化）；
 *  - 409 冲突的「覆盖重试」载荷重建（用服务端 current_version）。
 *
 * 渲染层交互（锁定/重试/冲突保草稿）由组件持有，此处不涉及。
 */

import type { EnvEntryView, EnvUpdateOp } from "./api/settings";

export type { EnvUpdateOp };

/** 键名级敏感模式（镜像后端 env_store._SECRET_KEY_RE；HTTP_PROXY 不命中）。 */
const SECRET_KEY_RE =
  /(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|PRIVATE_?KEY|DSN|CONN(ECTIO)?N)/i;

/** 值级：带 userinfo 的 URL（scheme://user:pass@host）= 认证连接串。 */
const URL_USERINFO_RE = /^[A-Za-z][A-Za-z0-9+.\-]*:\/\/[^\s/@]+:[^\s/@]*@/;

export const keyIsSensitive = (name: string): boolean => SECRET_KEY_RE.test(name);
export const valueIsSensitive = (v: string): boolean => URL_USERINFO_RE.test(v);

export interface EnvRow {
  /** React 稳定 key（行删除/插入后 index 会漂）。 */
  id: number;
  /** 编辑中的键名（已有敏感键锁定不可改——值未知，重命名语义不成立）。 */
  key: string;
  /** 普通键：明文值；敏感键：恒空串（值永不回填/显示）。 */
  value: string;
  /** 敏感键：替换值草稿（password 输入）。非空 = 将整值替换。 */
  replaceDraft: string;
  /** 敏感替换输入框展开中（纯 UI 态，脏判定只看 replaceDraft）。 */
  replacing: boolean;
  /** 基线 CAS token；本地新增行 null。 */
  version: string | null;
  /** 服务端原键名；null = 本地新增行（含 rename 的目标语义走最终意图 diff）。 */
  originKey: string | null;
  sensitive: boolean;
  kind: "plain" | "secret-name" | "url-with-auth";
  /** 敏感键：服务端已设置（「已设置」徽标）。 */
  set: boolean;
  url: { scheme: string; host?: string; has_credentials: boolean } | null;
}

/** 服务端 env 值 → 行内字符串：字符串原样，其余 JSON 序列化（number/bool/null/对象）。 */
export const normValue = (v: unknown): string =>
  typeof v === "string" ? v : JSON.stringify(v);

let rowSeq = 0;
export const nextRowId = (): number => ++rowSeq;

/** GET env 视图 entries → 编辑行（保服务端插入序；敏感行 value 恒空）。 */
export function rowsFromEntries(entries: Record<string, EnvEntryView>): EnvRow[] {
  return Object.entries(entries).map(([key, e]) => ({
    id: nextRowId(),
    key,
    value: e.sensitive ? "" : normValue(e.value),
    replaceDraft: "",
    replacing: false,
    version: typeof e.version === "string" ? e.version : null,
    originKey: key,
    sensitive: e.sensitive === true,
    kind: e.kind ?? (e.sensitive ? "secret-name" : "plain"),
    set: e.sensitive ? e.set !== false : false,
    url: e.url ?? null,
  }));
}

/** 本地新增空行（key/value 空、version null；敏感态随 key 输入即时重算）。 */
export function blankRow(): EnvRow {
  return {
    id: nextRowId(),
    key: "",
    value: "",
    replaceDraft: "",
    replacing: false,
    version: null,
    originKey: null,
    sensitive: false,
    kind: "plain",
    set: false,
    url: null,
  };
}

/** 新 key 输入后的本地敏感预判（镜像服务端键名规则 + 值形态规则）。 */
export function sensitiveForKey(key: string, draftValue: string): boolean {
  return keyIsSensitive(key) || valueIsSensitive(draftValue);
}

/** 规范形：trim key、丢空 key 行；同 key 后行覆盖前行（dict 语义，写盘同口径）。 */
export function canonRows(rows: EnvRow[]): EnvRow[] {
  const out = new Map<string, EnvRow>();
  for (const r of rows) {
    const key = r.key.trim();
    if (key === "") continue;
    out.set(key, { ...r, key });
  }
  return [...out.values()];
}

/** 基线 = GET env 的 entries（版本 token 与敏感元数据的真源）。 */
export type EnvBase = Record<string, EnvEntryView>;

/**
 * PATCH updates 构造：「行的最终意图」vs 基线视图。
 *
 * - 意图键不在基线 → create（version null）；敏感行必须已输替换值，
 *   空白敏感新行直接不算改动；
 * - 意图键在基线 → 普通行值变才发 update；敏感行输了替换值才发
 *   （值未知，「未动」无从比较，不发即保留）；
 * - 基线键不在意图（行被删/key 被清空）→ remove（version=基线 token）；
 * - rename（改 key）= 目标键的 create/update + 原键的 remove，
 *   由「最终意图 diff」自然得出，且每键至多一条 op。
 */
export function buildUpdates(rows: EnvRow[], base: EnvBase): EnvUpdateOp[] {
  const intent = new Map(canonRows(rows).map((r) => [r.key, r]));
  const ops: EnvUpdateOp[] = [];
  for (const [key, row] of intent) {
    const b = base[key];
    if (!b) {
      if (row.sensitive) {
        if (row.replaceDraft !== "") {
          ops.push({ key, version: null, value: row.replaceDraft });
        }
      } else {
        ops.push({ key, version: null, value: row.value });
      }
    } else if (b.sensitive || row.sensitive) {
      if (row.replaceDraft !== "") {
        ops.push({ key, version: b.version ?? null, value: row.replaceDraft });
      }
    } else if (row.value !== normValue(b.value)) {
      ops.push({ key, version: b.version ?? null, value: row.value });
    }
  }
  for (const [key, b] of Object.entries(base)) {
    if (!intent.has(key)) {
      ops.push({ key, version: b.version ?? null, remove: true });
    }
  }
  return ops;
}

/** 脏判定 = 存在任一 PATCH op（含删除：行没了/key 清空；空白新行不算）。 */
export const rowsChanged = (rows: EnvRow[], base: EnvBase): boolean =>
  buildUpdates(rows, base).length > 0;

/**
 * 409 覆盖重试：用服务端 conflicts 里的 current_version 重建载荷。
 * current_version=null（键已被删）→ version null（创建语义）。
 * 未冲突的 op 原样保留——后端是全事务拒绝，重试必须整批重发。
 */
export function resolveConflicts(
  ops: EnvUpdateOp[],
  conflicts: ReadonlyArray<{ key: string; current_version: string | null }>,
): EnvUpdateOp[] {
  const cur = new Map(conflicts.map((c) => [c.key, c.current_version ?? null]));
  return ops.map((op) => (cur.has(op.key) ? { ...op, version: cur.get(op.key) ?? null } : op));
}

/**
 * envVarsRows（CAS 版纯逻辑层）单元测试。
 *
 * 覆盖：敏感判定镜像、entries→行（敏感值恒空）、canon（trim/空 key/
 * 后行覆盖）、buildUpdates 的最终意图 diff（create/update/remove/rename/
 * 敏感替换/未触碰不发=保类型/每键至多一条）、resolveConflicts 覆盖重试。
 */
import { describe, expect, it } from "vitest";
import type { EnvEntryView } from "./api/settings";
import {
  blankRow,
  buildUpdates,
  canonRows,
  keyIsSensitive,
  normValue,
  resolveConflicts,
  rowsChanged,
  rowsFromEntries,
  sensitiveForKey,
  valueIsSensitive,
  type EnvRow,
} from "./envVarsRows";

const plain = (version = "v1"): EnvEntryView => ({
  sensitive: false,
  kind: "plain",
  value: "http://127.0.0.1:7890",
  version,
});
const secret = (version = "v2"): EnvEntryView => ({
  sensitive: true,
  kind: "secret-name",
  set: true,
  version,
});
const dsn = (version = "v3"): EnvEntryView => ({
  sensitive: true,
  kind: "url-with-auth",
  set: true,
  version,
  url: { scheme: "postgres", host: "db.internal", has_credentials: true },
});

/** 便捷造行：覆盖默认字段。 */
function row(over: Partial<EnvRow> & Pick<EnvRow, "key">): EnvRow {
  return { ...blankRow(), ...over };
}

describe("敏感判定（镜像后端正则）", () => {
  it("键名命中：TOKEN/SECRET/PASSWORD/API_KEY/DSN/CONNECTION…", () => {
    for (const k of ["MY_API_TOKEN", "API_KEY", "client_secret", "DB_PASSWORD", "SENTRY_DSN", "CONNECTION_STRING", "pwd"]) {
      expect(keyIsSensitive(k)).toBe(true);
    }
  });
  it("代理等普通键不命中", () => {
    for (const k of ["HTTP_PROXY", "HTTPS_PROXY", "PATH", "NO_PROXY"]) {
      expect(keyIsSensitive(k)).toBe(false);
    }
  });
  it("值形态：带 userinfo 的 URL 是敏感（键名无关）", () => {
    expect(valueIsSensitive("postgres://u:p@db.internal/x")).toBe(true);
    expect(valueIsSensitive("postgres://db.internal/x")).toBe(false);
    expect(valueIsSensitive("http://127.0.0.1:7890")).toBe(false);
  });
  it("本地预判 = 键名 ∨ 值形态", () => {
    expect(sensitiveForKey("FOO_TOKEN", "")).toBe(true);
    expect(sensitiveForKey("DATABASE_URL", "postgres://u:p@h/x")).toBe(true);
    expect(sensitiveForKey("HTTP_PROXY", "http://127.0.0.1:7890")).toBe(false);
  });
});

describe("rowsFromEntries", () => {
  it("普通键回明文、敏感键值恒空串且带元数据/version", () => {
    const rows = rowsFromEntries({ HTTP_PROXY: plain("v1"), MY_TOKEN: secret("v2"), DATABASE_URL: dsn("v3") });
    expect(rows.map((r) => r.key)).toEqual(["HTTP_PROXY", "MY_TOKEN", "DATABASE_URL"]);
    expect(rows[0]).toMatchObject({ value: "http://127.0.0.1:7890", sensitive: false, version: "v1", originKey: "HTTP_PROXY", set: false });
    expect(rows[1]).toMatchObject({ value: "", sensitive: true, kind: "secret-name", set: true, version: "v2" });
    expect(rows[2]).toMatchObject({ sensitive: true, kind: "url-with-auth", url: { scheme: "postgres", host: "db.internal" } });
  });
  it("version 缺失/非字符串 → null（IO 降级视图兼容）", () => {
    const rows = rowsFromEntries({ A: { sensitive: false, kind: "plain", value: 1, version: null } });
    expect(rows[0].version).toBeNull();
  });
});

describe("canonRows", () => {
  it("trim key、丢空 key 行、同 key 后行覆盖", () => {
    const out = canonRows([row({ key: "  A  ", value: "1" }), row({ key: "", value: "drop" }), row({ key: "A", value: "2" })]);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ key: "A", value: "2" });
  });
});

describe("buildUpdates（最终意图 diff）", () => {
  const base = {
    PROXY: plain("v1"),
    TOKEN_A: secret("v2"),
    KEEP: { sensitive: false, kind: "plain", value: 42, version: "v4" } as EnvEntryView,
  };

  it("未触碰 → 空 ops（number 类型由服务端原样保留）", () => {
    const rows = rowsFromEntries(base);
    expect(buildUpdates(rows, base)).toEqual([]);
    expect(rowsChanged(rows, base)).toBe(false);
  });

  it("普通键改值 → update 带基线 version", () => {
    const rows = rowsFromEntries(base);
    rows[0].value = "http://127.0.0.1:7891";
    expect(buildUpdates(rows, base)).toEqual([{ key: "PROXY", version: "v1", value: "http://127.0.0.1:7891" }]);
  });

  it("普通键值字符串化相等（42 → \"42\"）→ 不发，保类型", () => {
    const rows = rowsFromEntries(base);
    expect(rows.find((r) => r.key === "KEEP")?.value).toBe("42"); // normValue(42)
    expect(buildUpdates(rows, base)).toEqual([]);
  });

  it("敏感键未输替换值 → 不发（无从比较，保留）；输了 → 整值替换", () => {
    const rows = rowsFromEntries(base);
    rows[1].replacing = true; // 只展开输入框不算改动
    expect(buildUpdates(rows, base)).toEqual([]);
    rows[1].replaceDraft = "new-secret";
    expect(buildUpdates(rows, base)).toEqual([{ key: "TOKEN_A", version: "v2", value: "new-secret" }]);
  });

  it("新增普通键 → create(version null)；空白敏感新行不算改动；敏感新键输值才算", () => {
    const rows = [...rowsFromEntries(base), row({ key: "NEW", value: "x" }), row({ key: "FRESH_TOKEN", sensitive: true })];
    const ops = buildUpdates(rows, base);
    expect(ops).toContainEqual({ key: "NEW", version: null, value: "x" });
    expect(ops).not.toContainEqual(expect.objectContaining({ key: "FRESH_TOKEN" }));
    rows[4].replaceDraft = "s3cret";
    expect(buildUpdates(rows, base)).toContainEqual({ key: "FRESH_TOKEN", version: null, value: "s3cret" });
  });

  it("删行 → remove 带基线 version（含敏感键）", () => {
    const rows = rowsFromEntries(base).filter((r) => r.key !== "TOKEN_A");
    expect(buildUpdates(rows, base)).toEqual([{ key: "TOKEN_A", version: "v2", remove: true }]);
  });

  it("key 被清空 = 删除该键", () => {
    const rows = rowsFromEntries(base);
    rows[0].key = "   ";
    expect(buildUpdates(rows, base)).toEqual([{ key: "PROXY", version: "v1", remove: true }]);
  });

  it("普通键 rename = remove 原键 + create 新键，且目标键至多一条 op", () => {
    const rows = rowsFromEntries(base);
    rows[0].key = "RENAMED";
    const ops = buildUpdates(rows, base);
    expect(ops).toContainEqual({ key: "PROXY", version: "v1", remove: true });
    expect(ops).toContainEqual({ key: "RENAMED", version: null, value: "http://127.0.0.1:7890" });
    expect(ops.filter((o) => o.key === "RENAMED")).toHaveLength(1);
  });

  it("rename 到基线已有键 = 该键 update（值来自 rename 行），原键 remove，无重复键", () => {
    const b: Record<string, EnvEntryView> = { A: { sensitive: false, kind: "plain", value: "a", version: "va" }, B: { sensitive: false, kind: "plain", value: "b", version: "vb" } };
    const rows = rowsFromEntries(b);
    rows[1].key = "A"; // B → A
    rows[1].value = "b2";
    const ops = buildUpdates(rows, b);
    expect(ops).toEqual([{ key: "A", version: "va", value: "b2" }, { key: "B", version: "vb", remove: true }]);
  });

  it("只加一个空白行不算修改（无修改不可保存的关键判据）", () => {
    const rows = [...rowsFromEntries(base), blankRow()];
    expect(rowsChanged(rows, base)).toBe(false);
  });

  it("草稿 key 带首尾空白但内容一致 → 规范后不脏", () => {
    const rows = rowsFromEntries(base);
    rows[0].key = " PROXY ";
    expect(rowsChanged(rows, base)).toBe(false);
  });
});

describe("resolveConflicts（409 覆盖重试）", () => {
  it("冲突键用 current_version 重建；未冲突 op 原样保留", () => {
    const ops = [
      { key: "A", version: "va-old", value: "1" },
      { key: "B", version: "vb", remove: true } as const,
    ];
    expect(resolveConflicts(ops, [{ key: "A", current_version: "va-new" }])).toEqual([
      { key: "A", version: "va-new", value: "1" },
      { key: "B", version: "vb", remove: true },
    ]);
  });
  it("current_version=null（键已被删）→ version null（创建语义）", () => {
    expect(resolveConflicts([{ key: "A", version: "va", value: "1" }], [{ key: "A", current_version: null }])).toEqual([
      { key: "A", version: null, value: "1" },
    ]);
  });
});

describe("normValue", () => {
  it("字符串原样透传，非字符串 JSON 序列化", () => {
    expect(normValue("x")).toBe("x");
    expect(normValue("null")).toBe("null");
    expect(normValue(7890)).toBe("7890");
    expect(normValue(false)).toBe("false");
    expect(normValue(null)).toBe("null");
  });
});

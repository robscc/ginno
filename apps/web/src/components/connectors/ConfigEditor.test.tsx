/* ConfigEditor.fieldText 纯逻辑单测（node 环境）：a2a 连接器（枢纽 "a2a"
 * 与 peer 实例 "a2a-<slug>"）字段文案优先取 a2a 域 i18n（field-<键名>-title/
 * -desc），缺 key 回落 schema.title/description；其他连接器完全走 schema。
 * 渲染层留给 packaged UI e2e。 */

import { describe, expect, it, vi } from "vitest";
import { fieldText } from "./ConfigEditor";
import type { ConnectorSchemaProperty } from "@/lib/runtime";
import a2aEn from "../../../messages/en/a2a.json";

// ConfigEditor 仅运行时用到 api.patchConnectorConfig；mock 掉避免拉进
// sidecar 客户端整条依赖链（纯函数测试不需要真实 API）。
vi.mock("@/lib/runtime", () => ({ patchConnectorConfig: vi.fn() }));

const catalog = a2aEn.a2a as Record<string, string>;

/** 与 next-intl t.has/t 语义一致的查表（存在性判定 + 取值）。 */
const lookup = Object.assign((key: string) => catalog[key] ?? key, {
  has: (key: string) => Object.prototype.hasOwnProperty.call(catalog, key),
});

const HUB_KEYS = [
  "a2a_enabled", "inbound_enabled", "bind_mode", "bind_interface",
  "port", "admin_enabled", "audit_enabled",
];
const PEER_KEYS = [
  "enabled", "endpoint", "peer_fingerprint", "auth_scheme", "token",
  "trust", "caps", "confirm_mode", "allow_local_files",
  "task_timeout_s", "max_concurrent",
];

describe("fieldText：a2a 连接器优先 i18n", () => {
  it("枢纽 id=\"a2a\"：18 个 schema 字段全部命中 field-*-title/-desc", () => {
    for (const key of [...HUB_KEYS, ...PEER_KEYS]) {
      const spec: ConnectorSchemaProperty = {
        type: "string",
        title: `schema-${key}`,
        description: `schema-desc-${key}`,
      };
      const base = `field-${key.replace(/_/g, "-")}`;
      const r = fieldText("a2a", key, spec, lookup);
      expect(r.title, key).toBe(catalog[`${base}-title`]);
      expect(r.desc, key).toBe(catalog[`${base}-desc`]);
      // 确实走了 i18n 而不是 schema 回落
      expect(r.title).not.toBe(`schema-${key}`);
    }
  });

  it("peer 实例 id 以 a2a- 开头同样命中（下划线键名转连字符）", () => {
    const spec: ConnectorSchemaProperty = { type: "integer", title: "Task timeout" };
    const r = fieldText("a2a-expense-bot", "task_timeout_s", spec, lookup);
    expect(r.title).toBe("Task timeout (seconds)");
    expect(r.desc).toBe(catalog["field-task-timeout-s-desc"]);
  });

  it("catalog 缺 key → 回落 schema.title/description", () => {
    const spec: ConnectorSchemaProperty = {
      type: "string",
      title: "Custom field",
      description: "From backend schema",
    };
    const r = fieldText("a2a", "custom_field", spec, lookup);
    expect(r.title).toBe("Custom field");
    expect(r.desc).toBe("From backend schema");
  });

  it("schema 无 title → 回落键名本身；无 description → 空串", () => {
    const r = fieldText("a2a", "custom_field", { type: "string" }, lookup);
    expect(r.title).toBe("custom_field");
    expect(r.desc).toBe("");
  });
});

describe("fieldText：其他连接器不受影响", () => {
  it("非 a2a 前缀：即使 catalog 有同名 field-* 键也走 schema", () => {
    const spec: ConnectorSchemaProperty = {
      type: "integer",
      title: "端口",
      description: "钉钉回调端口",
    };
    // port 在 a2a catalog 里有 field-port-title，但 dingtalk 不得消费
    const r = fieldText("dingtalk", "port", spec, lookup);
    expect(r.title).toBe("端口");
    expect(r.desc).toBe("钉钉回调端口");
  });

  it("前缀边界：a2 / xa2a 不算 a2a 连接器", () => {
    // schema 文案刻意与 catalog 的 field-enabled-title（"Enabled"）不同，
    // 前缀判定若误命中会立刻暴露
    const spec: ConnectorSchemaProperty = { type: "boolean", title: "启用" };
    expect(fieldText("a2", "enabled", spec, lookup).title).toBe("启用");
    expect(fieldText("xa2a", "enabled", spec, lookup).title).toBe("启用");
  });
});

/* ConfigEditor.fieldText 纯逻辑单测（node 环境）：查表优先、缺 key 回落
 * schema——a2a 连接器（枢纽 "a2a" 与 peer 实例 "a2a-<slug>"）查 a2a 域
 * （field-<键名>-title/-desc），其余连接器查 conn 域
 * （field-<连接器 id>-<键名>-title/-desc）。渲染层留给 packaged UI e2e。 */

import { describe, expect, it, vi } from "vitest";
import { fieldText } from "./ConfigEditor";
import type { ConnectorSchemaProperty } from "@/lib/runtime";
import a2aEn from "../../../messages/en/a2a.json";
import connEn from "../../../messages/en/conn.json";
import connZh from "../../../messages/zh-CN/conn.json";

// ConfigEditor 仅运行时用到 api.patchConnectorConfig；mock 掉避免拉进
// sidecar 客户端整条依赖链（纯函数测试不需要真实 API）。
vi.mock("@/lib/runtime", () => ({ patchConnectorConfig: vi.fn() }));

const catalog = a2aEn.a2a as Record<string, string>;
const connCatalog = connEn.conn as unknown as Record<string, string>;
const connCatalogZh = connZh.conn as unknown as Record<string, string>;

/** 与 next-intl t.has/t 语义一致的查表（存在性判定 + 取值）。 */
const lookup = Object.assign((key: string) => catalog[key] ?? key, {
  has: (key: string) => Object.prototype.hasOwnProperty.call(catalog, key),
});
const connLookup = Object.assign((key: string) => connCatalog[key] ?? key, {
  has: (key: string) => Object.prototype.hasOwnProperty.call(connCatalog, key),
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

describe("fieldText：其他连接器查 conn 域", () => {
  it("内置连接器命中 conn 域 field-<cid>-<键名>-title/-desc（下划线转连字符）", () => {
    const spec: ConnectorSchemaProperty = {
      type: "string",
      title: "schema-title",
      description: "schema-desc",
    };
    const r = fieldText("wecom-bot", "default_agent_id", spec, lookup, connLookup);
    expect(r.title).toBe(connCatalog["field-wecom-bot-default-agent-id-title"]);
    expect(r.desc).toBe(connCatalog["field-wecom-bot-default-agent-id-desc"]);
    // 确实走了 i18n 而不是 schema 回落
    expect(r.title).not.toBe("schema-title");
  });

  it("conn 域缺 key（mod / 未配键的连接器）→ 回落 schema.title/description", () => {
    const spec: ConnectorSchemaProperty = {
      type: "integer",
      title: "端口",
      description: "钉钉回调端口",
    };
    // port 的 a2a catalog 里有 field-port-title，但 conn 域没有
    // field-dingtalk-port-*，非 a2a 连接器不得消费 a2a 域
    const r = fieldText("dingtalk", "port", spec, lookup, connLookup);
    expect(r.title).toBe("端口");
    expect(r.desc).toBe("钉钉回调端口");
  });

  it("未传 conn 查表（旧调用形态）→ 走 schema 兜底，行为与改造前一致", () => {
    const r = fieldText("wecom-bot", "bot_id", { type: "string", title: "BotID" }, lookup);
    expect(r.title).toBe("BotID");
    expect(r.desc).toBe("");
  });

  it("前缀边界：a2 / xa2a 不算 a2a 连接器", () => {
    // schema 文案刻意与 catalog 的 field-enabled-title（"Enabled"）不同，
    // 前缀判定若误命中会立刻暴露
    const spec: ConnectorSchemaProperty = { type: "boolean", title: "启用" };
    expect(fieldText("a2", "enabled", spec, lookup, connLookup).title).toBe("启用");
    expect(fieldText("xa2a", "enabled", spec, lookup, connLookup).title).toBe("启用");
  });

  it("en/zh catalog 对同一连接器字段集对称（漏配 zh 会在切换语言时回落英文）", () => {
    const enFieldKeys = Object.keys(connCatalog).filter((k) => k.startsWith("field-"));
    for (const k of enFieldKeys) {
      expect(connCatalogZh, k).toHaveProperty(k);
      expect(connCatalogZh[k], k).not.toBe("");
    }
  });
});

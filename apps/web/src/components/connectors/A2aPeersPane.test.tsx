/* A2aPeersPane 纯逻辑 + 契约单测（node 环境，照 A2aInboundCard.test.tsx 风格：
 * 无 DOM，不渲染组件——渲染层验证走 packaged UI e2e test_packaged_ui_a2a_peers.py）。
 *
 * 这里钉住：
 * 1. peerStatusKey：停用优先于连接状态、未知 status 降级；
 * 2. trustLabelKey：后端枚举内映射 i18n key、枚举外回落 null；
 * 3. i18n 双语齐全：本组件消费的 a2a 域 key 在 en/zh-CN 两个 catalog 都存在
 *    （漏 key 是 @ 菜单事故的教训——i18n 断裂编译期不报错）；
 * 4. 源码契约：单编辑器纪律（editingSlug 单值 + 卸载清 dirty）、
 *    子配置走 ConfigEditor、添加对端复用 A2aPeerAddModal、e2e 消费的
 *    data-testid 全部存在。 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { peerStatusKey, trustLabelKey } from "./A2aPeersPane";
import a2aEn from "../../../messages/en/a2a.json";
import a2aZh from "../../../messages/zh-CN/a2a.json";

const SRC = readFileSync(
  fileURLToPath(new URL("./A2aPeersPane.tsx", import.meta.url)),
  "utf8",
);

const EN = a2aEn.a2a as Record<string, string>;
const ZH = a2aZh.a2a as Record<string, string>;

describe("peerStatusKey（状态徽标 i18n key 派生）", () => {
  it("停用优先：enabled=false 一律停用徽标，探针/状态无意义", () => {
    expect(peerStatusKey({ enabled: false, status: "connected" })).toBe(
      "peers-status-disabled",
    );
    expect(peerStatusKey({ enabled: false, status: "" })).toBe(
      "peers-status-disabled",
    );
  });

  it("启用时按 registry status 映射；空/未知 status 降级 unknown", () => {
    expect(peerStatusKey({ enabled: true, status: "connected" })).toBe(
      "peers-status-connected",
    );
    expect(peerStatusKey({ enabled: true, status: "disconnected" })).toBe(
      "peers-status-disconnected",
    );
    expect(peerStatusKey({ enabled: true, status: "error" })).toBe(
      "peers-status-error",
    );
    expect(peerStatusKey({ enabled: true, status: "" })).toBe(
      "peers-status-unknown",
    );
    expect(peerStatusKey({ enabled: true, status: "bogus" })).toBe(
      "peers-status-unknown",
    );
  });

  it("派生出的 key 全部在两个 catalog 里存在（漏 key 渲染层断裂）", () => {
    for (const key of [
      "peers-status-connected",
      "peers-status-disconnected",
      "peers-status-error",
      "peers-status-disabled",
      "peers-status-unknown",
    ]) {
      expect(EN[key], `en 缺 ${key}`).toBeTruthy();
      expect(ZH[key], `zh-CN 缺 ${key}`).toBeTruthy();
    }
  });
});

describe("trustLabelKey（信任级别 i18n key）", () => {
  it("后端枚举内映射；枚举外回落 null（渲染层显示原值）", () => {
    expect(trustLabelKey("readonly")).toBe("trust-readonly");
    expect(trustLabelKey("normal")).toBe("trust-normal");
    expect(trustLabelKey("full")).toBe("trust-full");
    expect(trustLabelKey("weird")).toBeNull();
  });
});

describe("i18n 双语齐全（本组件消费的全部 key）", () => {
  const KEYS = [
    "peers-subtitle",
    "peers-empty",
    "peers-count",
    "peers-refresh",
    "peers-retry",
    "peers-load-failed",
    "peers-config",
    "peers-config-opened",
    "peers-config-title",
    "peers-config-load-failed",
    "peers-collapse",
    "peers-collapse-dirty-hint",
    "peers-reconnect",
    "peers-reconnecting",
    "peers-reconnect-ok",
    "peers-reconnect-failed",
    "peers-delete",
    "peers-delete-confirm-title",
    "peers-delete-confirm-ok",
    "peers-delete-done",
    "peers-delete-failed",
    "peer-add",
    "inbound-confirm-cancel",
    "wizard-added",
  ];
  it.each(KEYS)("%s 在 en 与 zh-CN 都存在", (key) => {
    expect(EN[key], `en 缺 ${key}`).toBeTruthy();
    expect(ZH[key], `zh-CN 缺 ${key}`).toBeTruthy();
  });
});

describe("源码契约（单编辑器纪律 / 复用积木 / e2e 定位点）", () => {
  it("子配置编辑内嵌 ConfigEditor，PATCH 目标是 peer 实例 id", () => {
    expect(SRC).toMatch(/<ConfigEditor\b/);
    expect(SRC).toContain("connectorId={p.id}");
  });

  it("添加对端复用 A2aPeerAddModal 向导", () => {
    expect(SRC).toContain('<A2aPeerAddModal');
  });

  it("dirty 双通道：透传外层守卫 + 卸载无条件清（防困住用户）", () => {
    expect(SRC).toMatch(/onDirtyChange\(dirty\)/);
    expect(SRC).toMatch(/useEffect\(\(\) => \(\) => onDirtyChange\(false\)/);
  });

  it("同时只有一个编辑器：editingSlug 单值，dirty 时禁止切换/另开", () => {
    expect(SRC).toContain("if (editorDirty) return;");
    expect(SRC).toContain("disabled={editorDirty && !editing}");
  });

  it("e2e 消费的 data-testid 定位点齐全", () => {
    for (const tid of [
      'data-testid="a2a-peers-pane"',
      'data-testid="a2a-peers-add"',
      "a2a-peer-row-${p.slug}",
      "a2a-peer-config-${p.slug}",
      "a2a-peer-reconnect-${p.slug}",
      "a2a-peer-delete-${p.slug}",
      "a2a-peer-delete-confirm-${p.slug}",
      "a2a-peer-collapse-${p.slug}",
    ]) {
      expect(SRC).toContain(tid);
    }
  });
});

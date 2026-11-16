/* RemotePeerBadge + TopBar 接线（remote-session-design.md R3 会话头远端
 * 标识）的源码断言测试——node 无 DOM，照 ConnectorDetail.test.tsx 范式。
 * 渲染真实性由 packaged UI e2e（test_packaged_ui_remote_session.py）覆盖。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enShell from "../../../messages/en/shell.json";
import zhShell from "../../../messages/zh-CN/shell.json";

const SRC = readFileSync(
  fileURLToPath(new URL("./RemotePeerBadge.tsx", import.meta.url)),
  "utf8",
);
const TOPBAR = readFileSync(
  fileURLToPath(new URL("./TopBar.tsx", import.meta.url)),
  "utf8",
);

describe("RemotePeerBadge", () => {
  it("机器图标 + 「远端 · name」文案键", () => {
    expect(SRC).toMatch(/<Icon name="server"/);
    expect(SRC).toContain('tr("remote-badge", { name })');
  });

  it("peer 名从脱敏读面解析，miss 回落 slug（归属事实不丢）", () => {
    expect(SRC).toContain("useA2aPeers(true)");
    expect(SRC).toMatch(/const peer = peers\.find\(\(p\) => p\.slug === slug\)/);
    expect(SRC).toMatch(/const name = peer\?\.name \|\| slug/);
  });

  it("tooltip 带完整 endpoint；无 endpoint 用降级键（不渲染空括号）", () => {
    expect(SRC).toContain('tr("remote-badge-title", { name, endpoint })');
    expect(SRC).toContain('tr("remote-badge-title-noendpoint", { name })');
  });

  it("e2e 定位点：data-testid=remote-peer-badge", () => {
    expect(SRC).toContain('data-testid="remote-peer-badge"');
  });
});

describe("TopBar 接线", () => {
  it("仅 session.remote_peer 存在时渲染徽标（本地会话零变化）", () => {
    expect(TOPBAR).toContain('import { RemotePeerBadge } from "./RemotePeerBadge"');
    expect(TOPBAR).toMatch(/\{session\?\.remote_peer && <RemotePeerBadge slug=\{session\.remote_peer\} \/>\}/);
  });
});

describe("i18n：新键双语齐全且扁平连字符（无点）", () => {
  const keys = ["remote-badge", "remote-badge-title", "remote-badge-title-noendpoint"] as const;
  for (const k of keys) {
    it(`${k} 在 en 与 zh-CN 都存在且含 {name} 占位`, () => {
      const en = (enShell.shell as Record<string, unknown>)[k];
      const zh = (zhShell.shell as Record<string, unknown>)[k];
      expect(typeof en).toBe("string");
      expect(typeof zh).toBe("string");
      expect(en as string).toContain("{name}");
      expect(zh as string).toContain("{name}");
      expect(k).not.toContain(".");
    });
  }
  it("zh-CN 徽标文案含「远端」（e2e 断言词依赖）", () => {
    expect((zhShell.shell as unknown as Record<string, string>)["remote-badge"]).toContain("远端");
  });
});
/* AgentChips 远端 peer 执行方选择（remote-session-design.md R3 定案 #10/#11）
 * 的源码断言测试——node 环境无 DOM，照 ConnectorDetail.test.tsx 范式读源码
 * 字符串钉住渲染层契约，渲染真实性由 packaged UI e2e
 * （test_packaged_ui_remote_session.py）覆盖。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enChat from "../../../../messages/en/chat.json";
import zhChat from "../../../../messages/zh-CN/chat.json";

const SRC = readFileSync(
  fileURLToPath(new URL("./AgentChips.tsx", import.meta.url)),
  "utf8",
);

describe("AgentChips：peer 与本地 agent 并列（定案 #10）", () => {
  it("peer 列表走 useA2aPeers，且只在首页拉取（!session）", () => {
    expect(SRC).toContain('import { useA2aPeers } from "@/lib/useA2aPeers"');
    expect(SRC).toMatch(/const peers = useA2aPeers\(!session\)/);
  });

  it("peer chips 仅首页渲染（!session 守卫），与 agent chips 同一行容器", () => {
    // agent map 与 peer map 必须在同一个 return 片段里（并列，不是二选一）
    expect(SRC).toMatch(/\{g\.agents\.map\(\(a\) => \{/);
    expect(SRC).toMatch(/\{!session &&\s*\n?\s*peers/);
  });

  it("只列 enabled 的 peer", () => {
    expect(SRC).toMatch(/peers\s*\n?\s*\.filter\(\(p\) => p\.enabled\)/);
  });

  it("peer 项带机器图标（server）与地址提示（peerEndpointHint）", () => {
    expect(SRC).toMatch(/<Icon name="server"/);
    expect(SRC).toContain("peerEndpointHint(p.endpoint)");
    // 完整 endpoint 进 tooltip（地址提示的完整版）
    expect(SRC).toMatch(/title=\{tc\("remote-peer-chip-title", \{ name: p\.name, endpoint: p\.endpoint \}\)\}/);
  });

  it("选中态用 peer:<slug> 前缀串，点击走既有 onSelect（不改签名）", () => {
    expect(SRC).toContain("peerTargetValue(p.slug)");
    expect(SRC).toMatch(/const sel = target === val/);
    expect(SRC).toMatch(/onClick=\{\(\) => onSelect\(sel \? null : val\)\}/);
  });

  it("e2e 定位点：data-testid=remote-peer-chip-<slug>", () => {
    expect(SRC).toContain("data-testid={`remote-peer-chip-${p.slug}`}");
  });

  it("本地 agent 选择行为不变：仍按 id 选中/取消，推荐标签保留", () => {
    expect(SRC).toMatch(/const sel = \(target \?\? session\?\.agent_id\) === a\.id/);
    expect(SRC).toMatch(/onClick=\{\(\) => onSelect\(sel \? null : a\.id\)\}/);
    expect(SRC).toContain('tc("agentChips.recommended")');
  });
});

describe("AgentChips：远端会话不可切换执行方（定案 #11）", () => {
  it("session.remote_peer 挂上后整行 chips 隐藏", () => {
    expect(SRC).toMatch(/if \(session\?\.remote_peer\) return null/);
  });

  it("守卫在全部 hooks 之后（React hooks 顺序不可条件化）", () => {
    const hookIdx = SRC.indexOf("useA2aPeers(!session)");
    const guardIdx = SRC.indexOf("if (session?.remote_peer) return null");
    expect(hookIdx).toBeGreaterThan(-1);
    expect(guardIdx).toBeGreaterThan(hookIdx);
  });
});

describe("i18n：新键双语齐全且扁平连字符（无点）", () => {
  const keys = ["remote-peer-chip-ask", "remote-peer-chip-title"] as const;
  for (const k of keys) {
    it(`${k} 在 en 与 zh-CN 都存在且含 {name} 占位`, () => {
      const en = (enChat.chat as Record<string, unknown>)[k];
      const zh = (zhChat.chat as Record<string, unknown>)[k];
      expect(typeof en).toBe("string");
      expect(typeof zh).toBe("string");
      expect(en as string).toContain("{name}");
      expect(zh as string).toContain("{name}");
      expect(k).not.toContain(".");
    });
  }
  it("title 键带 {endpoint} 占位（地址提示）", () => {
    expect((enChat.chat as unknown as Record<string, string>)["remote-peer-chip-title"]).toContain("{endpoint}");
    expect((zhChat.chat as unknown as Record<string, string>)["remote-peer-chip-title"]).toContain("{endpoint}");
  });
});
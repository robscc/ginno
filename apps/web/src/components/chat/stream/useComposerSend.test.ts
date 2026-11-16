/* useComposerSend 远端执行方分流（remote-session-design.md R3）的源码断言
 * 测试——node 无 DOM，照 ConnectorDetail.test.tsx 范式。钉住三条契约：
 * ① 首页选 peer → createSession 带 remote_peer 且本地 agent_id 不下发；
 * ② 本地 agent 懒创建行为逐字不变；③ peer 前缀串绝不漏进会话内发送路径。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(
  fileURLToPath(new URL("./useComposerSend.ts", import.meta.url)),
  "utf8",
);

describe("createAndSend：peer 选择 → remote_peer 建会话", () => {
  it("target 经 parsePeerTarget 分流", () => {
    expect(SRC).toContain('import { parsePeerTarget } from "./peerTarget"');
    expect(SRC).toMatch(/const peerSlug = parsePeerTarget\(target\)/);
  });

  it("选了 peer：agentId=null，remote_peer=slug 随 newSession 下发", () => {
    expect(SRC).toMatch(/const agentId = peerSlug \? null : \(target \?\? g\.agents\[0\]\?\.id \?\? null\)/);
    expect(SRC).toMatch(/remote_peer: peerSlug \?\? undefined/);
  });

  it("未选 peer：本地懒创建链不变（target ?? 首个 agent）", () => {
    // peerSlug=null 时 agentId 表达式退化为原实现；newSession 首参兼容 undefined
    expect(SRC).toMatch(/g\.newSession\(agentId \?\? undefined, \{/);
    expect(SRC).toContain("...(homeModel ?? {})");
  });
});

describe("send()：会话内路径对 peer 前缀设防", () => {
  it("localTarget 消毒：peer 前缀按未暂选处理", () => {
    expect(SRC).toMatch(/const localTarget = parsePeerTarget\(target\) \? null : target/);
  });

  it("parked / steer / 常规发送三处 agentId 解析都走 localTarget", () => {
    const uses = [...SRC.matchAll(/localTarget \?\? session\.agent_id \?\? g\.agents\[0\]\?\.id \?\? null/g)];
    expect(uses.length).toBe(3);
    // 原始 target 不得再直接参与 agentId 解析
    expect(SRC).not.toMatch(/[^l]target \?\? session\.agent_id/);
  });

  it("远端会话不换绑本地 agent（定案 #11：setSessionAgent 加 remote_peer 守卫）", () => {
    expect(SRC).toMatch(/if \(!session\.remote_peer && agentId && agentId !== session\.agent_id\) g\.setSessionAgent/);
  });
});
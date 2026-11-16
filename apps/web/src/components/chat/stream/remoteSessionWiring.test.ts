/* R3 远端会话的跨文件接线回归（remote-session-design.md）——源码断言范式。
 * 单点契约各自有测（peerTarget/a2a/AgentChips/useComposerSend/RemotePeerBadge），
 * 这里钉住「接缝」：store 转发、切换预告条分流、图标注册、runtime 重导出。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const read = (p: string) => readFileSync(fileURLToPath(new URL(p, import.meta.url)), "utf8");

const CHAT_STREAM = read("../ChatStream.tsx");
const USE_SESSIONS = read("../../../lib/store/useSessions.ts");
const STORE = read("../../../lib/store.tsx");
const ICONS = read("../../icons.tsx");
const RUNTIME = read("../../../lib/runtime.ts");
const SESSIONS_API = read("../../../lib/api/sessions.ts");
const TYPES = read("../../../lib/types.ts");

describe("store 链路：remote_peer 一路透传到 POST /sessions", () => {
  it("useSessions.newSession opts 声明并转发 remote_peer", () => {
    expect(USE_SESSIONS).toMatch(/remote_peer\?: string/);
    expect(USE_SESSIONS).toMatch(/remote_peer: opts\?\.remote_peer/);
  });

  it("store.tsx 的 newSession 接口类型同步（否则调用方 tsc 红）", () => {
    expect(STORE).toMatch(/remote_peer\?: string/);
  });

  it("createSession 参数类型含 remote_peer?: string", () => {
    expect(SESSIONS_API).toMatch(/remote_peer\?: string/);
  });

  it("SessionMeta 带 remote_peer 只读字段（会话头徽标的数据源）", () => {
    expect(TYPES).toMatch(/remote_peer\?: string \| null/);
  });
});

describe("ChatStream：切换预告条不把 peer 当本地 agent", () => {
  it("switchTarget 条件排除 peer 前缀 target", () => {
    expect(CHAT_STREAM).toContain('import { parsePeerTarget } from "./stream/peerTarget"');
    expect(CHAT_STREAM).toMatch(/target && session && !isChannel && !parsePeerTarget\(target\) && target !== session\.agent_id/);
  });
});

describe("图标与 API 注册", () => {
  it("icons 注册 server（lucide Server，机器语义）", () => {
    expect(ICONS).toMatch(/\bServer,?\s*\n/);
    expect(ICONS).toMatch(/server: Server/);
  });

  it("runtime.ts 重导出 a2a 域（api.listA2aPeers 可达）", () => {
    expect(RUNTIME).toContain('export * from "./api/a2a"');
  });
});
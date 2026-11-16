/* 会话未读「写入/清除」对称性回归（2026-11-16）。
 *
 * 事故：手动「标为未读」后点开会话，紫点与「标为已读」菜单项都不消失。
 * 根因：setSessionUnread(id,true) 乐观写 s.unread=true，但 clearSessionUnread
 * 只清 unreadRef map + 上报 /seen，**不碰 sessions 数组** → 本地 s.unread
 * 残留 true。服务端 forced_unread 其实已清（实测 API 正常），只有下次
 * reloadSessions 才纠正，用户看到的就是「点了没变已读」。
 *
 * node 环境无 DOM，照仓库既有范式做源码断言 + 纯逻辑直测。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(
  fileURLToPath(new URL("./useSessions.ts", import.meta.url)),
  "utf8",
);

/** 取出某个 useCallback 的函数体（到匹配的 }, [deps]); 为止）。 */
function bodyOf(name: string): string {
  const start = SRC.indexOf(`const ${name} = useCallback(`);
  expect(start, `未找到 ${name}`).toBeGreaterThan(-1);
  const rest = SRC.slice(start);
  const end = rest.indexOf("\n  );");
  expect(end, `${name} 函数体边界未找到`).toBeGreaterThan(-1);
  return rest.slice(0, end);
}

describe("clearSessionUnread 必须与写入对称", () => {
  it("清 unreadRef map（原有行为，不得回退）", () => {
    const b = bodyOf("clearSessionUnread");
    expect(b).toContain("delete next[id]");
    expect(b).toContain("setUnreadMap(next)");
  });

  it("上报服务端 /seen（原有行为，不得回退）", () => {
    const b = bodyOf("clearSessionUnread");
    expect(b).toContain("api.markSessionSeen(id)");
  });

  it("**清 sessions 数组里的 s.unread**（本次修复的核心）", () => {
    const b = bodyOf("clearSessionUnread");
    expect(
      b,
      "clearSessionUnread 必须清 s.unread，否则手动标未读后点开会话紫点不消",
    ).toContain("setSessions");
    expect(b).toMatch(/unread:\s*false/);
  });

  it("清 s.unread 时只改目标行，且无变化时返回原数组（避免无谓重渲染）", () => {
    const b = bodyOf("clearSessionUnread");
    expect(b).toMatch(/prev\.some\(\(s\) => s\.id === id && s\.unread\)/);
    expect(b).toMatch(/prev\.map\(\(s\) => \(s\.id === id \? \{ \.\.\.s, unread: false \} : s\)\)/);
    // 三元回落 prev，保持引用相等
    expect(b).toMatch(/:\s*prev,?\s*\n?\s*\)/);
  });

  it("写入侧仍乐观写 s.unread=true（对称性的另一半）", () => {
    const b = bodyOf("setSessionUnread");
    expect(b).toMatch(/unread:\s*true/);
    expect(b).toContain("api.markSessionUnread(id)");
  });

  it("setSessionUnread(false) 走 clearSessionUnread（不另造清除路径）", () => {
    const b = bodyOf("setSessionUnread");
    expect(b).toContain("clearSessionUnread(id)");
  });
});

describe("打开会话即已读的链路完整", () => {
  it("setActiveSession 打开会话时调 clearSessionUnread", () => {
    const b = bodyOf("setActiveSession");
    expect(b).toMatch(/if \(id\) clearSessionUnread\(id\)/);
  });
});

describe("紫点与菜单判据都依赖 s.unread（故必须清它）", () => {
  const ROW = readFileSync(
    fileURLToPath(
      new URL("../../components/shell/SidebarSessionRow.tsx", import.meta.url),
    ),
    "utf8",
  );

  it("紫点条件含 s.unread", () => {
    expect(ROW).toMatch(/unreadSessions\[s\.id\] !== undefined \|\| s\.unread/);
  });

  it("菜单 unreadNow 含 s.unread", () => {
    expect(ROW).toMatch(/const unreadNow = !!s\.unread \|\| unreadSessions\[s\.id\] !== undefined/);
  });
});

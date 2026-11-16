// 遗言待确认续跑的**拉取式兜底**接线回归（2026-11-16）。
//
// 为什么需要拉取而不能只靠 WS 广播：后端 _push_global_event 只遍历
// **当前活着的 socket**（server_shared.py），无缓冲无重放；而遗言恢复在
// lifespan +2s 就广播，前端 WS 重连间隔 3s → **真实帧经常先于 socket
// 就绪发出而静默丢失**。这是用户「遗言好像没触发过」的最后一环
// （前两环：notify 无交互提示、pending 条件限 auto+dev-swap，均已修）。
// packaged e2e 作者实测撞上此竞态，不得不按契约补投一帧才测得下去。
//
// node 环境无 DOM，照仓库既有范式读源码字符串做结构断言。
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const WEB = join(__dirname, "..", "..", "..", "..");
const read = (p: string) => readFileSync(join(WEB, p), "utf8");

describe("lastwords pending 拉取式兜底", () => {
  const engine = read("src/components/chat/useChatStreamEngine.ts");
  const api = read("src/lib/api/sessions.ts");

  it("API 层有 listLastwordsPending 打 GET /lastwords/pending", () => {
    expect(api).toContain("export async function listLastwordsPending");
    expect(api).toContain("${BASE}/lastwords/pending");
  });

  it("engine 引入了该 API", () => {
    expect(engine).toContain("listLastwordsPending");
    expect(engine).toMatch(/import\s*\{[^}]*listLastwordsPending[^}]*\}\s*from\s*"@\/lib\/runtime"/s);
  });

  it("拉取挂在 sock.onopen 里（与 resyncDevSwapRun 同范式）", () => {
    const open = engine.indexOf("sock.onopen = () => {");
    expect(open).toBeGreaterThan(-1);
    const pull = engine.indexOf("await listLastwordsPending()");
    expect(pull).toBeGreaterThan(open);
    // 必须在同一个 onopen 块内（下一个顶层函数之前）
    const nextFn = engine.indexOf("\n  function ", open);
    expect(pull).toBeLessThan(nextFn === -1 ? engine.length : nextFn);
    expect(engine).toContain("void resyncDevSwapRun();");
  });

  it("拉取结果喂**同一个** handle 分发器，不另写一套卡片渲染", () => {
    expect(engine).toMatch(
      /handle\(sid,\s*\{\s*event:\s*"runtime\.lastwords_pending",\s*\.\.\.it\s*\}\)/,
    );
  });

  it("逐条遍历且跳过无 session_id 的条目", () => {
    expect(engine).toMatch(/for\s*\(const it of r\?\.pending \?\? \[\]\)/);
    expect(engine).toContain("if (!it?.session_id) continue;");
  });

  it("拉取失败不拦连接（catch 静默，真实广播仍可弹卡）", () => {
    const pull = engine.indexOf("await listLastwordsPending()");
    const seg = engine.slice(pull, pull + 700);
    expect(seg).toContain("catch");
  });

  it("handler 侧 dedupeKey 保证真实帧与拉取帧不双弹", () => {
    const h = read("src/components/chat/engine/handle.ts");
    expect(h).toContain("lastwords-pending:${lpSid}");
  });
});

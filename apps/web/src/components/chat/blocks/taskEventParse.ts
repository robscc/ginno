import type { Block } from "./types";

// 后台任务事件注入带（runtime tasks/inject.py 的原文首行）在历史重放时折成
// 极薄 task_event 标记行——与 live 侧 steer.absorbed 的 ginno_task_event 折卡
// 对齐（handle.ts「绝不渲染 "[task finished]…" 原文带」）。tail 输出保留为
// 后续 text block（任务 store 滚动清理后 TaskCard 不一定还在，不能丢信息）。
//
// 注入带两种形态（inject.py STATUS_TEXT）：
//   [task finished] "{title}" ({tid}) exit {code} · ran {dur}
//   [task failed to start] "{title}" ({tid}) {note}
// 注：不带 m-flag 的 JS `$` 只匹配绝对串尾——注入带是多行（tail 在后），
// 故不锚 `$`；`(.*)` 不跨行，恰好吸收首行余下部分。
const TASK_EVENT_BAND_RE =
  /^\[task (finished|failed to start)\] "([^"]*)" \((t_[0-9a-zA-Z]+)\)(.*)/;

export function foldTaskEventBlocks(blocks: Block[]): Block[] {
  const out: Block[] = [];
  for (const b of blocks) {
    if (b.kind !== "text" || typeof b.text !== "string") {
      out.push(b);
      continue;
    }
    const m = TASK_EVENT_BAND_RE.exec(b.text);
    if (!m) {
      out.push(b);
      continue;
    }
    const [, evRaw, title, taskId, rest = ""] = m;
    const exit = / exit (-?\d+)/.exec(rest)?.[1];
    const dur = / · ran (.+?)(?:\n|$)/.exec(rest)?.[1];
    out.push({
      kind: "task_event",
      taskId,
      ev: evRaw === "finished" ? "finished" : "failed_to_start",
      title,
      ...(exit != null ? { exit: Number(exit) } : {}),
      ...(dur != null ? { dur } : {}),
    } as Block);
    // tail（--- tail --- 之后的输出与 Full log 行）保留为普通 text block
    const nl = b.text.indexOf("\n");
    if (nl >= 0 && b.text.slice(nl + 1).trim()) {
      out.push({ kind: "text", text: b.text.slice(nl + 1) });
    }
  }
  return out;
}

// workerQuestion — 订阅方收到的 <ginno_worker_question> 信封解析（2026-11-21
// worker ask_user 中断透传的前端折卡片层）。信封是 runtime _relay_instructions
// 注入的普通文本消息（worker_bus/relay.py：忙 → steer 吸收；闲 → coalesce
// wake），此前按裸文本渲染成文字墙——这里把它解析成结构化对象，供
// WorkerQuestionCard 折叠展示。
//
// 只做展示层切分：不解析就不改写消息本体、不触碰 checkpoint/WS 协议（与
// MentionText 的 mention chip 化同一纪律）。解析失败的信封原样透出，永不
// 吞文本。

export interface RelaySubQuestion {
  q: string;
  options: string[];
}

export interface RelayQuestion {
  worker: string; // worker session id
  qid: string;
  title: string; // worker 标题（head 行「」/ 引号内）
  reminder: boolean; // 已问未答重注入
  subs: RelaySubQuestion[]; // 单问题也归一为 1 元素数组
  raw: string; // 信封全文（卡片「展开原文」用）
}

const ENVELOPE_RE =
  /<ginno_worker_question worker="([^"]*)" qid="([^"]*)">([\s\S]*?)<\/ginno_worker_question>/g;

/** head 行提取 worker 标题：zh「title」/ en "title"，兜底 worker sid 前 8 位。 */
function parseTitle(body: string, worker: string): string {
  const m = body.match(/Worker\s*[「"]([^」"]+?)[」"]/);
  return (m?.[1]?.trim()) || worker.slice(0, 8);
}

/** 单问题体：「问题：q\n选项：\n- a\n- b」。选项行以「- 」开头，终止于
 *  空行或「转达方式/How to relay」段。多问题体：「问题（多问题卡…）：」+
 *  「- 子问题 N：q（选项：a / b）」行。 */
function parseSubs(body: string): RelaySubQuestion[] {
  const lines = body.split("\n");
  const subs: RelaySubQuestion[] = [];
  let mode: "single" | "multi" | null = null;
  let cur: RelaySubQuestion | null = null;
  for (const line of lines) {
    if (mode === null) {
      if (/^问题（多问题卡/.test(line.trim()) || /^问题\(多问题卡/.test(line.trim())) {
        mode = "multi";
      } else {
        const m = line.match(/^问题[:：]\s*(.+)$/);
        if (m) {
          mode = "single";
          cur = { q: m[1].trim(), options: [] };
        }
      }
      continue;
    }
    if (mode === "multi") {
      const m = line.match(/^-\s*子问题\s*\d+[:：]\s*(.+?)（选项[:：]\s*(.+?)）\s*$/);
      if (m) subs.push({ q: m[1].trim(), options: m[2].split(/\s*\/\s*/) });
      continue;
    }
    // single：收集选项行
    const trimmed = line.trim();
    if (!trimmed || /^(转达方式|How to relay)/.test(trimmed)) {
      if (cur && trimmed) break; // 进入转达段，收尾
      continue;
    }
    const m = trimmed.match(/^-\s*(.+)$/);
    if (m && cur) cur.options.push(m[1].trim());
  }
  if (mode === "single" && cur) subs.push(cur);
  return subs;
}

/** 解析一段消息文本：抽走全部信封，返回剩余文本 + 结构化列表。
 *  rest 保留信封前后的普通文本（多为空——注入消息就是信封本体）。 */
export function parseWorkerQuestions(text: string): {
  rest: string;
  items: RelayQuestion[];
} {
  const items: RelayQuestion[] = [];
  let rest = "";
  let last = 0;
  for (const m of text.matchAll(ENVELOPE_RE)) {
    const idx = m.index ?? 0;
    rest += text.slice(last, idx);
    last = idx + m[0].length;
    const body = m[3] ?? "";
    items.push({
      worker: m[1] ?? "",
      qid: m[2] ?? "",
      title: parseTitle(body, m[1] ?? ""),
      reminder: /\[reminder|\[提醒/.test(body),
      subs: parseSubs(body),
      raw: m[0],
    });
  }
  rest += text.slice(last);
  return { rest, items };
}

export function hasWorkerQuestion(text: string): boolean {
  ENVELOPE_RE.lastIndex = 0;
  return ENVELOPE_RE.test(text);
}

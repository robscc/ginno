import { parseDelegation } from "./delegation";
import type { Block } from "./types";

/** 解析契约 3 的注入消息文本（整个 text 块恰为一个结果标签时才算命中）。
 *  属性名容忍乱序；正文是 result_summary 原文。属性值经 runtime 侧
 *  _xml_attr 转义（goal 是模型自由文本，引号不转义会截断属性），在此还原。 */
function parseEnvelope(text: string): Document | null {
  if (typeof DOMParser === "undefined") return null;
  try {
    return new DOMParser().parseFromString(text, "text/html");
  } catch {
    return null;
  }
}

/** 契约 3：注入消息里的全部 <ginno_subagent_result>（合并注入时一条消息可能带
 *  多个）。属性 session/goal 由 runtime 转义，DOMParser 会自行解码。 */
export function parseSubagentResults(
  text: string,
): Array<{ sessionId: string; goal: string; summary: string }> {
  const doc = parseEnvelope(text);
  if (!doc) return [];
  const out: Array<{ sessionId: string; goal: string; summary: string }> = [];
  for (const el of Array.from(doc.getElementsByTagName("ginno_subagent_result"))) {
    out.push({
      sessionId: el.getAttribute("session") ?? "",
      goal: el.getAttribute("goal") ?? "",
      summary: (el.textContent ?? "").trim(),
    });
  }
  return out;
}

/** 单条结果标签（非合并场景）。 */
export function parseSubagentResult(
  text: string,
): { sessionId: string; goal: string; summary: string } | null {
  const all = parseSubagentResults(text);
  return all.length ? all[0] : null;
}

/** 子会话首条消息的 <ginno_subagent_brief>：段标签取内容（段名与 spawn_subagent
 *  的参数面一一对应），persona（agent_type 的正文）在标签外，报告格式与输出纪律
 *  在标签内的对应段。解析一律 DOMParser，不再用正则切文本（2026-10-01）。 */
export function parseSubagentBrief(
  text: string,
): {
  goal: string;
  constraints?: string;
  acceptance?: string;
  fork?: boolean;
  extra?: string;
  persona?: string;
  notes?: string;
} | null {
  const doc = parseEnvelope(text);
  if (!doc) return null;
  const brief = doc.getElementsByTagName("ginno_subagent_brief")[0];
  if (!brief) return null;
  const section = (tag: string) =>
    (brief.getElementsByTagName(tag)[0]?.textContent ?? "").trim();
  const goal = section("goal");
  if (!goal) return null;
  const extra = section("other");
  const notes = [section("report_format"), section("output_discipline")]
    .filter(Boolean)
    .join("\n\n");
  // 标签外的前置文本：命中 agent_type 时是类型 persona 正文
  let before = "";
  for (const node of Array.from(brief.parentNode?.childNodes ?? [])) {
    if (node === brief) break;
    before += node.textContent ?? "";
  }
  before = before.trim();
  return {
    goal,
    constraints: section("constraints") || undefined,
    acceptance: section("acceptance") || undefined,
    // "(fork)" is the runtime-side ASCII marker (lang-independent); the
    // Chinese phrase matches checkpoints persisted before English prompts.
    fork: /\(fork\)|并行分支（fork）/.test(`${extra}\n${before}`),
    extra: extra || undefined,
    persona: before || undefined,
    notes: notes || undefined,
  };
}

/** Worker 协议（worker-protocol-design.md §7）：注入消息里的全部
 *  <ginno_worker_result>（watch 终态回传 / digest）。属性 from=worker 标题、
 *  status=协议状态、kind=digest 标识进度节选；正文是摘要/节选原文。 */
export function parseWorkerResults(
  text: string,
): Array<{
  sessionId: string;
  from: string;
  status: string;
  goal: string;
  summary: string;
  digest: boolean;
}> {
  const doc = parseEnvelope(text);
  if (!doc) return [];
  const out: Array<{
    sessionId: string;
    from: string;
    status: string;
    goal: string;
    summary: string;
    digest: boolean;
  }> = [];
  for (const el of Array.from(doc.getElementsByTagName("ginno_worker_result"))) {
    out.push({
      sessionId: el.getAttribute("session") ?? "",
      from: el.getAttribute("from") ?? "",
      status: el.getAttribute("status") ?? "",
      goal: el.getAttribute("goal") ?? "",
      summary: (el.textContent ?? "").trim(),
      digest: el.getAttribute("kind") === "digest",
    });
  }
  return out;
}

/** assign_worker 注入的 <ginno_worker_assign>：worker 会话侧的任务简报
 *  （from=派发方标题、session=派发方 id，段名与工具参数一一对应）。 */
export function parseWorkerAssign(
  text: string,
): {
  fromSessionId: string;
  from: string;
  goal: string;
  constraints?: string;
  acceptance?: string;
} | null {
  const doc = parseEnvelope(text);
  if (!doc) return null;
  const el = doc.getElementsByTagName("ginno_worker_assign")[0];
  if (!el) return null;
  const section = (tag: string) =>
    (el.getElementsByTagName(tag)[0]?.textContent ?? "").trim();
  const goal = section("goal");
  if (!goal) return null;
  return {
    fromSessionId: el.getAttribute("session") ?? "",
    from: el.getAttribute("from") ?? "",
    goal,
    constraints: section("constraints") || undefined,
    acceptance: section("acceptance") || undefined,
  };
}

/** 历史重放用：把持久化的注入消息（HumanMessage 原文）折成结果卡片块，避免原始
 *  XML 标签以用户气泡形式出现；子会话首条 <ginno_subagent_brief> 同样折成简报卡。
 *  合并注入（同一 wake turn 携带多个子代理回传）时一条消息里有多个结果标签——
 *  每个标签一张卡片，标签之间的合并头文字丢弃。解析一律走 DOMParser。 */
export function foldSubagentResultBlocks(blocks: Block[]): Block[] {
  const out: Block[] = [];
  for (const b of blocks) {
    if (b.kind !== "text") {
      out.push(b);
      continue;
    }
    // 清单状态信封（plan-orchestration §4）：折专用清单卡（同一注入通道，
    // child key 是 plan-<id>），与结果卡区分开。
    const planDoc = parseEnvelope(b.text);
    const planEls = planDoc
      ? Array.from(planDoc.getElementsByTagName("ginno_plan_update"))
      : [];
    if (planEls.length) {
      for (const el of planEls) {
        const raw = (el.textContent ?? "").trim();
        const body = raw.split("You can:", 1)[0].trim() || raw;
        out.push({
          kind: "plan_update",
          planId: el.getAttribute("plan") ?? "",
          text: body,
        });
      }
      continue;
    }
    const brief = parseSubagentBrief(b.text);
    if (brief) {
      out.push({ kind: "subagent_brief", ...brief });
      continue;
    }
    // worker 协议信封（worker-protocol-design §7）：员工任务简报 / 回传
    // 折专用卡——检查在 subagent 结果信封之前（两类信封互斥）。
    const wAssign = parseWorkerAssign(b.text);
    if (wAssign) {
      out.push({ kind: "worker_assign", ...wAssign });
      continue;
    }
    const wResults = parseWorkerResults(b.text);
    if (wResults.length) {
      for (const r of wResults) {
        out.push({
          kind: "worker_result",
          sessionId: r.sessionId,
          from: r.from || undefined,
          status: r.status || undefined,
          goal: r.goal || undefined,
          summary: r.summary,
          digest: r.digest,
        });
      }
      continue;
    }
    // 旧版委托注入（2026-10-05 信封化之前落库的）：裸机器头 + delegation=
    // 溯源行的 HumanMessage——同样折成结果卡（sessionId 取溯源行），重载后
    // 不再以原始文本气泡出现。goal 无从恢复，卡片标题退回 backend。
    const rawDel = parseDelegation(b.text);
    if (rawDel?.sid) {
      // 展示层去掉机器头/delegation= 行/--- output --- 分隔线;错误态把
      // [diagnostic] 提为首行警示(原始 b.text 保留给解析,不进卡片)。
      let summary = b.text
        .replace(/^\[delegate[^\]]*\]\n?/, "")
        .replace(/^delegation=[0-9a-f]+\n?/m, "")
        .replace(/^--- output ---\n?/m, "")
        .replace(/^\[diagnostic\] (.+)$/m, "⚠️ $1")
        .trim();
      if (rawDel.stop === "error" && !summary.startsWith("⚠️")) {
        summary = "⚠️ " + (summary || "delegation failed");
      }
      out.push({
        kind: "subagent_result",
        sessionId: rawDel.sid,
        summary: summary || b.text,
      });
      continue;
    }
    const results = parseSubagentResults(b.text);
    if (!results.length) {
      out.push(b);
      continue;
    }
    for (const r of results) {
      out.push({
        kind: "subagent_result",
        sessionId: r.sessionId,
        goal: r.goal || undefined,
        summary: r.summary,
      });
    }
  }
  return out;
}

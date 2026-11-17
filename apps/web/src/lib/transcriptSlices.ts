/**
 * transcriptSlices — 会话切换首屏「时间分片挂载」的纯逻辑层
 * （2026-11-17 perf，vitest 覆盖；渲染/滚动补偿在 TranscriptList.tsx）。
 *
 * 背景：窗口化（首屏 ≤80 条）之后，重会话窗口内 ~1700 blocks 仍是一次性
 * 同步 React mount，构成切换剩余 max longtask（实测 ~341ms）的主体。
 * 分片策略：**尾部先挂**（最近的消息首帧可见），更旧的消息按 block 预算
 * 逐片向上 prepend，片间经 requestIdleCallback 让出主线程（范式同
 * Markdown.tsx 的逐块 idle 高亮）。
 *
 * 按 block 预算而非固定条数切片：消息块数分布极不均匀（工具重轮可达
 * 数十块/条），固定条数会让重片重新变成百毫秒级长任务。预算制把每片
 * 成本钉在 ~120 blocks（≈25-40ms mount），max longtask 压到 50ms 阈值下。
 */

/** 首帧同步挂载的 block 预算（尾部起算）。≈ 最近几条重消息，首屏即有内容。 */
export const SLICE_INITIAL_BLOCKS = 160;
/** 每个 idle 片追加的 block 预算。 */
export const SLICE_STEP_BLOCKS = 120;
/** 单片消息条数上限——超轻消息（1 block/条）也不把片切得过碎。 */
export const SLICE_MAX_MSGS = 16;
/** 任何时刻至少渲染的尾部条数——会话切换/历史到达的首帧不允许空转录。 */
export const SLICE_MIN_RENDERED = 4;
/** 一次性新增超过该条数视为「整批历史到达」（流式追加 ≤2-3 条，不触发重置）。 */
export const SLICE_RESET_JUMP = 16;

/**
 * 从 end（不含）向头部按 block 预算累计，返回本片的起始下标。
 * 至少挂 1 条（单条超预算也整条挂——消息内部不再细分）；达到预算或
 * maxMsgs 即停。counts[i] = 第 i 条消息的 block 数。
 */
export function sliceStart(
  counts: number[],
  end: number,
  budget: number,
  maxMsgs: number = SLICE_MAX_MSGS,
): number {
  const e = Math.max(0, Math.min(end, counts.length));
  let start = e;
  let blocks = 0;
  while (start > 0 && e - start < maxMsgs) {
    const c = counts[start - 1] ?? 0;
    if (e - start >= 1 && blocks + c > budget) break;
    blocks += c;
    start -= 1;
  }
  return start;
}

/**
 * 渲染用 skip 的夹紧：不超过 visibleLen - minRendered（保证尾部至少
 * minRendered 条首帧可见），不小于 0。覆盖：旧会话 headSkip 残留大于新
 * 窗口长度（切会话首帧 messages 还是旧值）、microcompact 消息缩水等。
 */
export function clampSkip(
  skip: number,
  visibleLen: number,
  minRendered: number = SLICE_MIN_RENDERED,
): number {
  return Math.max(0, Math.min(skip, visibleLen - minRendered));
}

/**
 * 分片重置判定（render 期派生，不能等 effect——否则首帧已整窗同步
 * mount，长任务照样发生）：会话切换，或整批历史到达（长度跳变）。
 * 流式追加/重试替换的小 delta 不重置，避免直播期间反复重切。
 */
export function sliceResetNeeded(
  prev: { sid: string | undefined; len: number },
  sid: string | undefined,
  len: number,
): boolean {
  return prev.sid !== sid || len - prev.len > SLICE_RESET_JUMP;
}
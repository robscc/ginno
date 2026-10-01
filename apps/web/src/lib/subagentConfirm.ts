// 子代理结果卡片的「已确认」本地态（subagent P2 任务 3）。
//
// 确认是纯 UI 定稿动作（不回流 runtime），但结果卡片在历史重放里每次都从注入
// 消息重新折出（foldSubagentResultBlocks），组件状态活不过一次重载——所以落在
// localStorage：per-viewer 便利态，丢了大不了再点一次确认，不存在一致性风险。
// 读写都包 try/catch（隐私窗口/清理过站点数据的浏览器里直接退化为不记忆）。

const KEY = "ginno-subagent-confirmed";
// 上限防膨胀：只存 session id，超出裁掉最旧的（写入序近似时间序）。
const MAX_ENTRIES = 500;

function readAll(): string[] {
  try {
    const raw = localStorage.getItem(KEY);
    const list = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(list) ? list.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function isSubagentConfirmed(sessionId: string): boolean {
  return readAll().includes(sessionId);
}

export function markSubagentConfirmed(sessionId: string): void {
  try {
    const list = readAll().filter((id) => id !== sessionId);
    list.push(sessionId);
    localStorage.setItem(KEY, JSON.stringify(list.slice(-MAX_ENTRIES)));
  } catch {
    /* storage unavailable — the click still reads as confirmed for this mount */
  }
}

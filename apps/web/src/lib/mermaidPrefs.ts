/**
 * Mermaid 图形渲染开关（Settings → 通用 → 对话内渲染 Mermaid 图形）。
 *
 * 存 localStorage 而不是 settings.json：这是一个**纯前端渲染偏好**，runtime
 * 完全不需要知道它（图形渲染没有任何后端参与，见 docs/design/mermaid-render-design.md）。
 * 与 `ginno-theme` 同一个取舍：界面临时偏好走 localStorage，跨端/需要 runtime
 * 读的配置才走 settings.json。
 *
 * 读取是同步的（渲染路径在首帧就要决定出图还是出源码），写入后同步通知所有
 * 已挂载的 MermaidBlock，避免用户关掉开关后要重进会话才生效。
 */

const KEY = "ginno-mermaid";

/** 默认开：图形化是这个能力的主路径，代码块是降级路径。 */
export const MERMAID_DEFAULT = true;

const subs = new Set<(enabled: boolean) => void>();

/** 同步读取当前值。localStorage 不可用（隐私模式等）时返回默认值。 */
export function readMermaidPref(): boolean {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw === null) return MERMAID_DEFAULT;
    return raw !== "0";
  } catch {
    return MERMAID_DEFAULT;
  }
}

export function writeMermaidPref(enabled: boolean): void {
  try {
    localStorage.setItem(KEY, enabled ? "1" : "0");
  } catch {
    /* ignore — 只是偏好，丢了不影响功能 */
  }
  // 立即回调，订阅方不必等下一次事件；开关抖动也不会堆积
  for (const fn of Array.from(subs)) {
    try {
      fn(enabled);
    } catch {
      /* 单个订阅方抛错不能拖垮其他图 */
    }
  }
}

export function subscribeMermaidPref(fn: (enabled: boolean) => void): () => void {
  subs.add(fn);
  return () => {
    subs.delete(fn);
  };
}

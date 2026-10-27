/** 剪贴板写入，带回退：非安全上下文（http/Tauri 某些窗口）没有
 *  navigator.clipboard，用隐形 textarea + execCommand 兜底——与
 *  TurnIdChip/CopyBubbleButton 共用，别再各自复制一份。 */
export async function copyText(t: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(t);
    return true;
  } catch {
    try {
      const ta = document.createElement("textarea");
      ta.value = t;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch {
      return false;
    }
  }
}

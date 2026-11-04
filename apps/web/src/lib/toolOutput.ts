/** bash 类工具输出的分段解析（runtime builtin.py 固定格式）：首行
 * `[exit N]`（或 `[timeout after …]`）+ `--- stderr ---` 分隔行。解析失败
 * → null（其他工具的输出原样渲染，天然兼容）。
 * 展示约定（2026-11-04 用户需求）：不分色块——exit 行 0=绿 / 非 0=红；
 * stdout 默认色；stderr 琥珀色文字。 */
export interface ShellOutput {
  exitLine: string;
  ok: boolean;
  stdout: string;
  stderr: string | null;
}

export function parseShellOutput(s: string): ShellOutput | null {
  const m = /^\[([^\]]+)\]\n/.exec(s);
  if (!m) return null;
  const exitLine = m[1]; // "exit 0" | "timeout after 30s — partial output kept"
  const ok = /^exit 0$/.test(exitLine);
  const rest = s.slice(m[0].length);
  const sep = "\n--- stderr ---\n";
  const idx = rest.indexOf(sep);
  if (idx < 0) return { exitLine, ok, stdout: rest, stderr: null };
  return { exitLine, ok, stdout: rest.slice(0, idx), stderr: rest.slice(idx + sep.length) };
}

/** delegate_agent 回执解析：首行机器头 [delegate backend=… mode=… stop=…] +
 *  delegation=<id> 溯源行（external-agents-design.md §9）。turns/duration/tokens
 *  仅终态回执携带；started 回执没有 stop 键——显示 started 而不是 ?。 */
export function parseDelegation(content: string): {
  backend: string;
  mode: string;
  stop: string;
  sid?: string;
  turns?: string;
  duration?: string;
  tokens?: string;
} | null {
  const first = content.split("\n", 1)[0] || "";
  const m = first.match(/^\[delegate\s+(.*)\]$/);
  if (!m) return null;
  const kv: Record<string, string> = {};
  for (const part of m[1].split(/\s+/)) {
    const eq = part.indexOf("=");
    if (eq > 0) kv[part.slice(0, eq)] = part.slice(eq + 1);
  }
  const sid = (content.match(/^delegation=([0-9a-f]+)$/m) || [])[1];
  return {
    backend: kv.backend || "?",
    mode: kv.mode || "?",
    stop: kv.stop || (kv.started !== undefined ? "started" : "?"),
    sid,
    turns: kv.turns,
    duration: kv.duration,
    tokens: kv.tokens,
  };
}

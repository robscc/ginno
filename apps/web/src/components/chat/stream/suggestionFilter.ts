import type { SessionSuggestion } from "@/lib/api/sessions";
import type { SessionMeta } from "@/lib/types";

/** 渠道只读会话（type="channel"，#170 readonly）不显示 compact 动作卡。
 *  对话在 IM 侧进行，桌面无 composer，「立即压缩」无从谈起——压缩要么 IM 侧
 *  agent 自动做、要么用户在 IM 侧 /compact（2026-11-18 截图实录）。按动作
 *  类型过滤（context.pressure 与 failure.context_length 都挂 compact），
 *  open_settings / switch_model 卡仍可操作，保留。零运行时依赖（仅 type
 *  import），vitest node 环境可直测。
 */
export function filterVisibleSuggestions(
  suggestions: SessionSuggestion[],
  session: Pick<SessionMeta, "id" | "type"> | null,
  dismissed: Record<string, true>,
): SessionSuggestion[] {
  if (!session) return [];
  return suggestions.filter(
    (s) =>
      !dismissed[`${session.id}:${s.id}`] &&
      !(session.type === "channel" && s.action?.type === "compact"),
  );
}

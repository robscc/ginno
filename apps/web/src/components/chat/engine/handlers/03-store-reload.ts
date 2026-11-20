// 03-store-reload — 弹层请求与 store 侧 reload 类事件。等价迁移自 handle()
// switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { pushNotice } from "@/lib/notice";
import type { SessionMeta } from "@/lib/types";

export function onPermissionRequest(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  ctx.permsRef.current[sid] = { tool: ev.tool as string, args: ev.args };
}

export function onVersionPropose(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  ctx.proposeRef.current[sid] = {
    workflow_id: ev.workflow_id as string,
    from_version: (ev.from_version as number) ?? 0,
    diff: (ev.diff as string) ?? "",
    rationale: (ev.rationale as string) ?? "",
  };
}

export function onTodosChanged(ctx: EngineCtx, sid: string) {
  ctx.g.reloadTodos();
}

export function onSkillsChanged(ctx: EngineCtx, sid: string) {
  // A turn (install_skills tool, bash) or the Settings page mutated
  // ~/.ginno/skills — refresh the slash menu's skill list live.
  ctx.g.reloadSkills();
}

export function onMemoryChanged(ctx: EngineCtx, sid: string) {
  // Memory refinery transition (auto-draft ready, applied, discarded) —
  // refresh the Memory tab badge; the panel itself refetches on open.
  ctx.g.reloadMemoryBadge();
}

export function onAgentsChanged(ctx: EngineCtx, sid: string) {
  // Agent CRUD in Settings — keep the picker/mention list in sync.
  ctx.g.reloadAgents();
}

export function onSessionModelChanged(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 模型热切回流（model-assignment-design.md §3.2）：服务端已原地换掉
  // 会话条目的模型对象，下一个 superstep 生效。发起端乐观更新过，这里
  // 兜底其它视图（回放页/另一窗口）并给一条轻提示。applySessionPatch
  // 是裸 spread，未携带的键不能以 undefined 进 patch（会清字段）。
  const patch: Partial<SessionMeta> = {};
  if (typeof ev.provider === "string") patch.provider = ev.provider;
  if (typeof ev.model === "string") patch.model = ev.model;
  // 思考档位随事件同步（thinking-effort-design §6）。
  const evEffort = (ev as Record<string, unknown>).thinking_effort;
  if (typeof evEffort === "string") {
    patch.thinking_effort = evEffort as SessionMeta["thinking_effort"];
  }
  if (patch.provider || patch.model || patch.thinking_effort) {
    ctx.g.applySessionPatch(sid, patch);
  }
  // 轻提示走**公共通知层**（notice-center-design.md）：旧实现借用
  // chat/mod 的右下角 toast 宿主，语义错位且 mods 未挂载时帧直接丢弃。
  // dedupeKey 按会话去重——多窗口/回放页收到同一事件只弹一条。
  // inbox:false（2026-11-08 用户反馈「切换模型没必要进通知面板」）：
  // 操作确认类反馈——toast 几秒即逝，不进通知中心不留档。
  {
    const { tr } = ctx.i18nRef.current;
    const key = patch.model ? "model.switchToast" : "model.switchToastNoModel";
    pushNotice("success", tr(key, patch.model ? { model: patch.model } : undefined), {
      source: "model",
      dedupeKey: `model:${sid}`,
      inbox: false,
    });
  }
}

export function onWorkflowsChanged(ctx: EngineCtx, sid: string) {
  ctx.g.reloadWorkflows();
  ctx.g.reloadWorkflowRuns();
}

export function onSynthesisEvent(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // Background summarization progressed — refresh the 总结 panel list
  // (started/attempt/finished all change what it shows). If THIS is the
  // case the summarize button is waiting on, resolve the wait (the ref
  // guard inside finishSynthesisWait dedupes vs the polling fallback).
  void ctx.g.reloadSynthesisCases();
  if (ev.kind === "finished" && ev.synthesis_id === ctx.sumPendingRef.current) {
    void ctx.finishSynthesisWait(ev.synthesis_id as string);
  }
}

// 08-inline — 无 graph turn 的内联文本回执（@ 投递报告 / 内置命令 notice）。
// 等价迁移自 handle() switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { ensureLive, markDelivered, mutateLive, stripFromLive } from "../liveBubble";

export function onDeliverReport(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // @session 投递回执（at-session-design §5.1）：系统提示条，非气泡——
  // 各目标状态 started/queued/missing/no_body + 「附件未随投递」注明。
  const targets: Array<{ label?: string; state?: string }> = Array.isArray(ev.targets)
    ? ev.targets
    : [];
  const stateText = (st: string, label: string) => {
    if (st === "started") return ctx.i18nRef.current.tr("deliver.started", { label });
    if (st === "queued") return ctx.i18nRef.current.tr("deliver.queued", { label });
    if (st === "missing") return ctx.i18nRef.current.tr("deliver.missing", { label });
    return ctx.i18nRef.current.tr("deliver.noBody", { label });
  };
  const lines = targets.map((t) => stateText(String(t.state), String(t.label ?? "")));
  if (ev.context_skipped) lines.push(ctx.i18nRef.current.tr("deliver.ctxSkipped"));
  markDelivered(ctx, sid);
  // notice 同款：live 气泡内一段 system 文本（无 graph turn，非 delta 流）。
  ensureLive(ctx, sid, typeof ev.turn_id === "string" ? ev.turn_id : undefined);
  mutateLive(ctx, sid, {
    event: "token.delta",
    content: lines.join("\n"),
  });
}

export function onNotice(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // Built-in command reply (e.g. /help): no graph turn ran, so the server
  // pushes the rendered text directly into the live bubble as one delta.
  // 事件契约：带 i18n_key 时落地即翻译，否则原样直显 message。
  markDelivered(ctx, sid);
  let _txt = ctx.i18nRef.current.evText(ev, (ev.message as string) || "");
  // 瞬态模型错误提示（auto_retry / model_error_backoff）——包成 code
  // span + ⚠️，气泡里呈现为琥珀色等宽小条而不是混入正文的普通文本
  // （用户反馈样式不显眼）。model_error_backoff 于 2026-11-08 并入：
  // 此前它走通用分支，GLM 429 退避横幅被拼进消息开头且重试成功后
  // 不撤（用户截图实录），多轮退避还会堆多行。
  if (
    (ev.i18n_key as string) === "stream.auto_retry" ||
    (ev.i18n_key as string) === "stream.model_error_backoff"
  ) {
    // 原位更新（2026-10-11 用户反馈：此前每次 append，N 轮重试堆 N 行）：
    // 新一轮提示替换上一轮；文本里带分隔符整体进出，替换/撤行都精确。
    const prev = ctx.retryNoticeRef.current[sid];
    if (prev) stripFromLive(ctx, sid, prev);
    const live = (ctx.storeRef.current[sid] ?? []).find(
      (m) => m.id === ctx.liveBySessionRef.current[sid],
    );
    const lastB = (live?.blocks[live.blocks.length - 1] ?? null) as {
      kind?: string;
      text?: string;
    } | null;
    const sep =
      lastB && (lastB.kind === "text" || lastB.kind === "thinking") && lastB.text
        ? "\n\n"
        : "";
    const chunk = `${sep}\`⚠️ ${_txt}\``;
    ctx.retryNoticeRef.current[sid] = chunk;
    mutateLive(ctx, sid, { event: "token.delta", content: chunk });
    return;
  }
  mutateLive(ctx, sid, {
    event: "token.delta",
    content: _txt,
  });
}

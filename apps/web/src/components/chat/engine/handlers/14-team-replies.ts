// 14-team-replies — AgentTeam 投递回执与成员回帖（agent-team-design.md §5.2
// 步骤 6）。等价迁移自 handle() switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import type { Block } from "@/components/chat/blocks";
import { mid } from "../../streamCore";
import { markDelivered } from "../liveBubble";
import { isWatching } from "../notifyOut";
import { notifyNative } from "@/lib/desktop";
import { pushNotice } from "@/lib/notice";
import { shouldNotify } from "@/lib/notifyHub";

export function onTeamDelivered(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // AgentTeam 投递回执（backlog P0-1 + P1-1）。此前全仓库零 handler：
  // runtime 发完就 continue，前端 busy 恒真 → composer 永久锁死。
  // runtime 侧已补 message.end，这里做防御性收口 + 回执可视化。
  //
  // 三态语义严格区分（§5.3.3 投递原子性）：
  //   delivered → 清 sending
  //   stalled   → **也已发送**，只是 turn 未在窗口内起跑。绝不标 failed：
  //               标 failed 会给重试按钮，一点就是重复投递
  //   failed    → 确定没发（blocked 原子拒绝一个字都没发 / 成员不存在），
  //               标 failed 允许重试才是对的
  const receipt = ((ev.receipt as string) || "delivered") as
    "delivered" | "stalled" | "failed";
  const meta = {
    receipt,
    channel: (ev.channel as string) || "",
    reason: (ev.reason as string) || "",
    error: (ev.error as string) || "",
    displayName: (ev.display_name as string) || (ev.agent_id as string) || "",
    memberSessionId: (ev.session_id as string) || "",
    teamId: (ev.team_id as string) || "",
    instanceId: (ev.instance_id as string) || "",
  };
  // 空占位收口用的 liveId：必须在下面置 null **之前**捕获。
  const endedLiveId = ctx.liveBySessionRef.current[sid] ?? null;
  // turnId 兜底判据（不能只靠 liveId）：广场投递是异步的，上一条的
  // message.end 可能**晚于**下一条的发送到达——它会把 liveBySessionRef
  // 清成 null，而那时引用已经指向新一条的占位了。此时只按 liveId 过滤
  // 会拿到 null → 新占位漏删 → 又一个「(空回复)」（2026-11-15 e2e 实测
  // 偶发：第一条 @ 投递 + 第二条紧跟，第二条的空气泡时有时无）。
  // 客户端 turnId 经 invoke.turn_id 原样传给服务端并回显（ws.py:350），
  // 所以 ev.turn_id 与气泡 turnId 是同一把钥匙，跨事件顺序稳定。
  const endTurn = (ev.turn_id as string) || null;
  const list = ctx.storeRef.current[sid] ?? [];
  // 挂到最后一条用户消息上（回执属于「我发的那条」，不是新气泡）。
  let lastUser = -1;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].role === "user") { lastUser = i; break; }
  }
  if (lastUser >= 0) {
    const next = [...list];
    const prev = next[lastUser];
    // 多目标投递：同一用户消息可能收到 N 条回执，合并成数组语义——
    // 这里保留最严重的一条（failed > stalled > delivered），避免后到的
    // delivered 覆盖掉先前的 blocked 拒绝。
    const rank = { failed: 2, stalled: 1, delivered: 0 } as const;
    const keep = !prev.teamDelivery
      || rank[receipt] >= rank[prev.teamDelivery.receipt]
      ? meta : prev.teamDelivery;
    next[lastUser] = {
      ...prev,
      teamDelivery: keep,
      ...(receipt === "failed"
        ? { status: "failed" as const, failReason: meta.error || meta.reason || "delivery failed" }
        : { status: undefined, failReason: undefined }),
    };
    ctx.storeRef.current[sid] = next;
  } else if (receipt === "failed") {
    markDelivered(ctx, sid);
  } else {
    markDelivered(ctx, sid);
  }
  // 防御性收口：即使 message.end 因故未到，也不让 composer 卡死。
  ctx.busyBySessionRef.current[sid] = false;
  ctx.serverRunningRef.current[sid] = false;
  ctx.liveBySessionRef.current[sid] = null;
  // 丢弃空占位气泡（与 turn.stopped / error 同一范式，那两处的注释写的
  // 就是同一个「(空回复)」症状——唯独投递分支漏了这道收口）。
  //
  // 广场投递**不起主会话 turn**（ws.py 的 AgentTeam 路由分支只发
  // team.delivered + message.end），所以 ChatStream.attemptSend 乐观插入的
  // `blocks: []` 占位永远等不到流事件。此前只清 liveBySessionRef，空壳仍
  // 留在 store 里 → message.end 的 stampTurnTs 按 turnId 兜底命中它并盖上
  // ts → 渲染成「(空回复)」+「刚刚」，永久留在转录里（2026-11-15 用户截图：
  // 每发一条必现一个）。成员真正的产出稍后走 team.reply 回帖卡，与它无关。
  //
  // 只删 blocks 为空的那一个：空团队分支的 notice 提示、以及任何已流出的
  // 正文都在这个气泡里，删过头会把合法内容一起抹掉。
  if (endedLiveId || endTurn) {
    ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).filter(
      (m) =>
        !(
          m.role === "assistant" &&
          m.blocks.length === 0 &&
          (m.id === endedLiveId || (!!endTurn && m.turnId === endTurn))
        ),
    );
  }
  ctx.syncDisplay(sid);
}

export function onTeamReply(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 成员在自己的 session 跑完，产出以回帖写回主会话 checkpoint，并广播本事件。
  // 这里把它作为一条**完成的** assistant 消息追加到主会话视图——否则用户要手动
  // reload 才看得到（checkpoint 写入不触发主会话的实时流）。
  //
  // 去重：用服务端的 message_id 作 ChatMsg.id。它与 history reload 时
  // mapHistory 读到的 AIMessage.id 相同，所以「实时追加」与「之后 reload」
  // 指向同一条，不会双渲染。
  const msgId = (ev.message_id as string) || mid();
  const store = ctx.storeRef.current[sid] ?? [];
  if (store.some((m) => m.id === msgId)) return; // 已存在（reload 先到）
  const text = (ev.text as string) || "";
  ctx.storeRef.current[sid] = [
    ...store,
    {
      id: msgId,
      role: "assistant" as const,
      agentId: (ev.agent_id as string) || null,
      blocks: [{ kind: "text", text } as Block],
      teamReply: {
        team_id: (ev.team_id as string) || "",
        instance_id: (ev.instance_id as string) || "",
        member_session_id: (ev.member_session_id as string) || "",
        display_name: (ev.display_name as string) || "",
        truncated: Boolean(ev.truncated),
      },
    },
  ];
  ctx.syncDisplay(sid);
  // P1-18：成员回帖 → 通知中心 + OS 通知（此前 team.reply 零通知，
  // 用户离开广场就不知道成员干完了）。走与 reply 同一套门：
  // 总开关 ∧ kinds.team ∧ 不在盯 ∧ 不在 DnD。
  {
    const watching = isWatching(ctx, sid);
    const name = (ev.display_name as string) || (ev.agent_id as string) || "";
    const body = ctx.i18nRef.current.tr("team.notifyReply", { name });
    pushNotice("info", body, { dedupeKey: `team-reply-${ev.instance_id}` });
    if (shouldNotify("team", watching)) {
      const title = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
      // 新契约同上：唯一出口内部裁隐私/推提示音，这里只给原文 + notifyKind。
      void notifyNative({
        kind: "session",
        notifyKind: "team",
        id: sid,
        title: `👥 ${title}`,
        body,
      });
    }
  }
}

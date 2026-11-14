"use client";

// DeliveryBriefCard — 团队投递的结构化 brief 卡（backlog P2-13，原型
// design-mocks/agent-team-design.html 的 .brief 卡）。
//
// 成员收到的投递此前是原文透传：看不出「要做什么 / 在哪个共享 workspace /
// 带着什么团队上下文 / 完成后会怎样」。本卡把投递消息按四要素结构化展示在
// 成员工作台顶部（ChatStream 之上——ChatStream 在本批次边界外，卡片不进
// 气泡流内，这是有意的落点取舍，见交付报告）。
//
// 数据来源（已核实链路，不改 runtime）：
//   · deliver 新 turn 路径把 {team_id, instance_id} 写进成员会话入口
//     HumanMessage.additional_kwargs.ginno_team_delivery（deliver.py →
//     runner._run_managed_turn → turn.py user_extra_kwargs）；
//   · messages_ui 历史序列化把该标记透传为用户气泡的 teamDelivery 字段；
//   · 这里从成员会话 history 倒找最后一条带标记的用户消息，取其正文作「目标」。
//   · steer 路径的投递是 turn 中注入（渲染为 steer 带），不产生新的入口
//     消息——brief 仍展示最近一次入口投递，语义正确。
//
// 低频：打开工作台拉一次 + 成员状态跃迁 working 时延迟补拉（投递先落
// checkpoint 再起 turn，1.2s 足够落盘）+ 手动刷新。不轮询。
//
// 旧数据兜底：P2-11 修复前的投递没有标记 → 找不到就不渲染卡片（不拿普通
// 直聊消息冒充投递）。团队上下文 / workspace / 完成后说明全部来自已有团队
// 数据（team.has_team_md / shared_workspace），不依赖新端点。

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { Team, TeamMemberStatus } from "@/lib/types";

interface Delivery {
  text: string;
  teamId: string;
}

export function DeliveryBriefCard({
  team,
  instanceId,
  sessionId,
  memberStatus,
}: {
  team: Team;
  instanceId: string;
  sessionId: string;
  /** 成员当前语义态：跃迁 working 时触发一次延迟补拉。 */
  memberStatus?: TeamMemberStatus;
}) {
  const tc = useTranslations("chat");
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(true);
  const [mdSummary, setMdSummary] = useState<string | null>(null);
  const reqRef = useRef(0);

  const load = useCallback(async () => {
    const req = ++reqRef.current;
    setLoading(true);
    try {
      const res = await api.getSessionHistory(sessionId);
      if (req !== reqRef.current) return;
      // teamDelivery 未在 getSessionHistory 的返回类型里声明（该函数在边界
      // 外），但 runtime 序列化确实透传——这里局部断言读取，不改 API 客户端。
      const msgs = (res.messages ?? []) as Array<{
        role: "user" | "assistant";
        blocks?: Array<{ kind?: string; text?: string }>;
        teamDelivery?: { teamId?: string };
      }>;
      let found: Delivery | null = null;
      for (let i = msgs.length - 1; i >= 0; i--) {
        const m = msgs[i];
        if (m.role !== "user" || !m.teamDelivery) continue;
        const text = (m.blocks ?? [])
          .filter((b) => b.kind === "text" && typeof b.text === "string")
          .map((b) => b.text as string)
          .join("\n")
          .trim();
        found = { text, teamId: m.teamDelivery.teamId ?? "" };
        break;
      }
      setDelivery(found);
      setLoaded(true);
    } catch {
      // sidecar 抖动：按「无投递」处理，手动刷新可重试；不弹错不阻塞工作台。
      if (req === reqRef.current) setLoaded(true);
    } finally {
      if (req === reqRef.current) setLoading(false);
    }
  }, [sessionId]);

  // 打开 / 切换工作台会话：拉一次。
  useEffect(() => {
    setDelivery(null);
    setLoaded(false);
    setOpen(true);
    void load();
  }, [load]);

  // 成员进入 working：投递消息刚落地（或即将落地），延迟补拉一次。
  const prevStatus = useRef<TeamMemberStatus | undefined>(memberStatus);
  useEffect(() => {
    const prev = prevStatus.current;
    prevStatus.current = memberStatus;
    if (memberStatus === "working" && prev !== "working") {
      const id = setTimeout(() => void load(), 1200);
      return () => clearTimeout(id);
    }
  }, [memberStatus, load]);

  // TEAM.md 摘要：has_team_md 时取首个非空行（去标题符号、截断 80 字）。
  // 一次 GET，随团队切换重置；失败按「无摘要」降级，只留「已注入」chip。
  useEffect(() => {
    if (!team.has_team_md) {
      setMdSummary(null);
      return;
    }
    let alive = true;
    api
      .getTeamMd(team.id)
      .then((r) => {
        if (!alive) return;
        const line =
          (r.text ?? "")
            .split("\n")
            .map((l) => l.replace(/^#+\s*/, "").trim())
            .find(Boolean) ?? "";
        setMdSummary(line.length > 80 ? `${line.slice(0, 80)}…` : line);
      })
      .catch(() => {
        if (alive) setMdSummary(null);
      });
    return () => {
      alive = false;
    };
  }, [team.id, team.has_team_md]);

  // 没找到带标记的投递（直聊会话 / 旧数据）→ 不渲染，绝不拿直聊冒充投递。
  if (!loaded || !delivery) return null;

  const row = (team.member_rows ?? []).find((r) => r.instance_id === instanceId);
  const ws =
    team.shared_workspace?.primary_folder || team.shared_workspace?.workspace || "";

  return (
    <div className="mx-4 mt-2 rounded-[11px] border border-blue-400/30 bg-blue-400/[0.05] px-3 py-2 text-[11.5px] leading-relaxed text-muted">
      <div className="flex items-center gap-2">
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-xs font-semibold text-blue-400"
        >
          {open ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0" />
          )}
          <span className="truncate">📥 {tc("team.briefTitle")}</span>
          {row && (
            <span className="shrink-0 rounded-full border border-line px-1.5 py-px text-[10px] font-normal text-faint">
              @{row.display_name}
            </span>
          )}
        </button>
        <button
          onClick={() => void load()}
          className="shrink-0 rounded p-0.5 text-faint transition-colors hover:text-blue-400"
          title={tc("team.briefRefresh")}
        >
          <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
        </button>
      </div>
      {open && (
        <div className="mt-1.5 max-h-48 space-y-1 overflow-y-auto">
          <div>
            <span className="font-semibold text-blue-400">{tc("team.briefGoal")}</span>
            {": "}
            <span className="whitespace-pre-wrap break-words text-txt">
              {delivery.text || "—"}
            </span>
          </div>
          <div>
            <span className="font-semibold text-blue-400">{tc("team.briefWorkspace")}</span>
            {": "}
            {ws ? (
              <span className="break-all font-mono text-[11px]">{ws}</span>
            ) : (
              <span className="text-yellow-500/90">{tc("team.briefWorkspaceUnset")}</span>
            )}
          </div>
          <div>
            <span className="font-semibold text-blue-400">{tc("team.briefContext")}</span>
            {": "}
            {team.has_team_md
              ? mdSummary
                ? tc("team.briefTeamMdSummary", { summary: mdSummary })
                : tc("team.briefTeamMdOn")
              : tc("team.briefTeamMdOff")}
          </div>
          <div>
            <span className="font-semibold text-blue-400">{tc("team.briefAfter")}</span>
            {": "}
            {tc("team.briefAfterText")}
          </div>
        </div>
      )}
    </div>
  );
}
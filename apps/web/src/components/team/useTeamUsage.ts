"use client";

// useTeamUsage — 团队成本聚合的低频拉取（backlog P2-5，设计 §5.6 硬要求）。
//
// runtime 已把 team-member 的 usage_source 标为 "team"，并提供独立端点
// GET /api/teams/{id}/usage?days=N（每实例 + 团队合计）。**刻意不塞进
// member_row 状态热路径**（runtime 侧注释同口径：每次状态刷新都读 usage
// 日志会拖慢整个面板），前端这里同样低频：
//   · 团队切换 / 面板挂载时拉一次；
//   · 60s 定时刷新（远低于状态 WS 的实时频率）；
//   · 手动刷新按钮（MemberPanel 脚注 / 总览头部）。
// 请求序号防竞态：切团队后旧响应不得覆盖新团队的数据。

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "@/lib/runtime";

/** 单实例成本（端点 members[] 的镜像）。 */
export interface TeamUsageMember {
  instance_id: string;
  display_name: string;
  agent_id: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  calls: number;
}

/** 团队成本聚合（端点 ok:true 时的有效载荷）。 */
export interface TeamUsage {
  days: number;
  members: TeamUsageMember[];
  total: {
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    calls: number;
  };
}

/** 人类可读 token 数：987 → "987"，12345 → "12.3k"，2_500_000 → "2.50M"。
 *  面板行内空间有限，精确值放 tooltip（usageTip）。 */
export function fmtTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

/** 低频刷新周期：60s。成本是慢变量，不需要更密。 */
const REFRESH_MS = 60_000;
/** 统计窗口（天）：与端点默认一致，tooltip 里向用户明示口径。 */
const USAGE_DAYS = 30;

export function useTeamUsage(teamId: string | null | undefined) {
  const [usage, setUsage] = useState<TeamUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const reqRef = useRef(0);

  const refresh = useCallback(async () => {
    if (!teamId) return;
    const req = ++reqRef.current;
    setLoading(true);
    try {
      const r = await api.getTeamUsage(teamId, USAGE_DAYS);
      if (req !== reqRef.current) return; // 已切团队 / 已有更新的请求
      if (r.ok && r.total) {
        setUsage({
          days: r.days ?? USAGE_DAYS,
          members: r.members ?? [],
          total: r.total,
        });
        setFailed(false);
      } else {
        setFailed(true);
      }
    } catch {
      if (req === reqRef.current) setFailed(true);
    } finally {
      if (req === reqRef.current) setLoading(false);
    }
  }, [teamId]);

  useEffect(() => {
    // 切团队先清旧数据——宁可短暂无数字，也不显示别的团队的成本。
    setUsage(null);
    setFailed(false);
    if (!teamId) return;
    void refresh();
    const id = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(id);
  }, [teamId, refresh]);

  return { usage, loading, failed, refresh };
}
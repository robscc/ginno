"use client";

// PlanDag — 任务清单依赖图视图（PlanBoard 的 list/dag 双视图之一）。
// 视觉语言与 workflow 编辑器 WorkflowDag 同构：横向左→右、纯 SVG 圆角节点
// + 左侧状态点 + 水平 S 形贝塞尔边 + 箭头。状态色沿用清单自己的 ITEM_META
// （同一面板内不引入第二套颜色语义，running 绿与列表视图鸭子一致）。
//
// 2026-11-15 用户约束：
// ① 同步缩小不横滚——viewBox 等比缩放适配容器宽（ResizeObserver 实测），
//    只缩不放（scale ≤ 1），字随图缩；
// ② hover 显示原始字体大小——HTML 浮层（不受 SVG 缩放影响）展示完整
//    goal / constraints / 依赖，靠节点下缘，下半区节点翻转到上缘防溢出。
//
// 布局 = lib/planDagLayout（纯函数，vitest 覆盖）；hover 高亮整条上下游
// 依赖链（dependencyChain 传递闭包），无关节点/边淡出。

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import type { Plan, PlanItem, PlanItemStatus } from "@/lib/types";
import {
  DAG_NODE_H,
  DAG_NODE_W,
  dependencyChain,
  layoutPlanDag,
} from "@/lib/planDagLayout";
import { ITEM_META } from "@/components/chat/PlanBoard";

/** blocked 派生态（与 PlanBoard 同规则）：pending 但依赖未全 done。 */
function withBlocked(items: PlanItem[]): PlanItem[] {
  const byId = new Map(items.map((x) => [x.item_id, x.status]));
  return items.map((it) =>
    it.status === "pending" && (it.depends_on ?? []).some((d) => byId.get(d) !== "done")
      ? { ...it, status: "blocked" as PlanItemStatus }
      : it,
  );
}

const TIP_W = 248;
const TIP_H_EST = 96;

export function PlanDag({ plan }: { plan: Plan }) {
  const g = useGinno();
  const t = useTranslations("chat");
  const wrapRef = useRef<HTMLDivElement>(null);
  const [wrapW, setWrapW] = useState(720);
  const [hovered, setHovered] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  // 容器实宽 → 等比缩放系数。ResizeObserver 不可用时退回默认 720（仍只缩不放）。
  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setWrapW(e.contentRect.width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const items = useMemo(() => withBlocked(plan.items), [plan.items]);
  const layout = useMemo(() => layoutPlanDag(items), [items]);
  const byId = useMemo(() => new Map(items.map((i) => [i.item_id, i])), [items]);
  const chain = useMemo(
    () => (hovered ? dependencyChain(layout, hovered) : null),
    [hovered, layout],
  );

  if (layout.nodes.length === 0) return null;
  const scale = Math.min(1, wrapW / layout.width);
  const svgW = layout.width * scale;
  const svgH = layout.height * scale;
  const hovNode = hovered ? layout.nodes.find((n) => n.id === hovered) : null;
  const hovItem = hovNode ? byId.get(hovNode.id) : null;
  // 浮层位置：节点下缘；下半区翻转到上缘防容器溢出；水平 clamp 防右溢。
  const tipLeft = hovNode
    ? Math.max(4, Math.min(hovNode.x * scale + 8, wrapW - TIP_W - 4))
    : 0;
  const below = hovNode ? hovNode.y * scale + DAG_NODE_H * scale + 6 : 0;
  const tipTop = hovNode
    ? below + TIP_H_EST > svgH + 8
      ? Math.max(4, hovNode.y * scale - TIP_H_EST - 6)
      : below
    : 0;

  return (
    <div
      ref={wrapRef}
      className="relative rounded-lg border border-line bg-base/40 p-1"
      role="img"
      aria-label={t("planBoard.dagAria")}
    >
      <svg
        width={svgW}
        height={svgH}
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        className="mx-auto block"
        aria-hidden="true"
      >
        <defs>
          <marker
            id="plan-dag-arrow"
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="3"
            orient="auto"
          >
            <path d="M0,0 L7,3 L0,6 Z" fill="rgb(var(--faint))" />
          </marker>
        </defs>
        {/* 边：上游右边中点 → 下游左边中点，水平 S 贝塞尔（WorkflowDag 同构） */}
        {layout.edges.map((e) => {
          const a = layout.nodes.find((n) => n.id === e.from)!;
          const b = layout.nodes.find((n) => n.id === e.to)!;
          const x1 = a.x + DAG_NODE_W;
          const y1 = a.y + DAG_NODE_H / 2;
          const x2 = b.x;
          const y2 = b.y + DAG_NODE_H / 2;
          const mx = (x1 + x2) / 2;
          const inChain = !chain || (chain.has(e.from) && chain.has(e.to));
          return (
            <path
              key={`${e.from}->${e.to}`}
              d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
              fill="none"
              stroke={inChain && chain ? "rgb(var(--violet))" : "rgb(var(--line2))"}
              strokeWidth={inChain && chain ? 1.8 : 1.5}
              strokeOpacity={inChain ? 0.85 : 0.12}
              markerEnd="url(#plan-dag-arrow)"
              style={{ transition: "stroke-opacity 150ms, stroke 150ms" }}
            />
          );
        })}
        {/* 节点：圆角矩形 + 左状态点 + id 标题 + goal 副标题（字随 viewBox 缩放） */}
        {layout.nodes.map((n) => {
          const it = byId.get(n.id)!;
          const meta = ITEM_META[it.status] ?? ITEM_META.pending;
          const clickable = !!it.child_session_id;
          const dimmed = chain ? !chain.has(n.id) : false;
          const isSel = selected === n.id;
          const goalShort =
            it.goal.length > 14 ? it.goal.slice(0, 14) + "…" : it.goal;
          return (
            <g
              key={n.id}
              data-id={n.id}
              opacity={dimmed ? 0.22 : 1}
              style={{
                cursor: clickable ? "pointer" : "default",
                transition: "opacity 150ms",
              }}
              onMouseEnter={() => setHovered(n.id)}
              onMouseLeave={() => setHovered(null)}
              onClick={() => {
                setSelected(n.id);
                if (it.child_session_id) g.setActiveSession(it.child_session_id);
              }}
            >
              <rect
                x={n.x}
                y={n.y}
                width={DAG_NODE_W}
                height={DAG_NODE_H}
                rx={8}
                fill="rgb(var(--card))"
                stroke={isSel ? meta.color : "rgb(var(--line2))"}
                strokeWidth={isSel ? 2 : 1}
              />
              <circle cx={n.x + 12} cy={n.y + DAG_NODE_H / 2} r={4} fill={meta.color} />
              <text
                x={n.x + 22}
                y={n.y + 17}
                fill="rgb(var(--txt))"
                fontSize={11}
                fontWeight={600}
              >
                {it.item_id}
              </text>
              <text x={n.x + 22} y={n.y + 31} fill="rgb(var(--faint))" fontSize={9}>
                {goalShort}
              </text>
              {it.backend && it.backend !== "ginno" && (
                <text
                  x={n.x + DAG_NODE_W - 8}
                  y={n.y + 17}
                  textAnchor="end"
                  fill="rgb(var(--amber))"
                  fontSize={8}
                >
                  {it.backend}
                </text>
              )}
              {(it.attempts ?? 0) > 1 && (
                <text
                  x={n.x + DAG_NODE_W - 8}
                  y={n.y + 31}
                  textAnchor="end"
                  fill="rgb(var(--faint))"
                  fontSize={8}
                >
                  ×{it.attempts}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {/* hover 浮层：HTML 原始字体大小，不受 SVG 缩放影响 */}
      {hovNode && hovItem && (
        <div
          className="pointer-events-none absolute z-10 rounded-md border border-line2 bg-card2 px-2.5 py-1.5 shadow-lg"
          style={{ left: tipLeft, top: tipTop, width: TIP_W }}
          data-testid="plan-dag-tip"
        >
          <div className="flex items-center gap-1.5">
            <span
              className="h-2 w-2 shrink-0 rounded-full"
              style={{ background: (ITEM_META[hovItem.status] ?? ITEM_META.pending).color }}
            />
            <span className="text-[11px] font-semibold text-txt">{hovItem.item_id}</span>
            <span className="text-[10px] text-faint">{hovItem.status}</span>
            {hovItem.backend && hovItem.backend !== "ginno" && (
              <span className="rounded bg-amber/10 px-1 text-[9px] text-amber">
                {hovItem.backend}
              </span>
            )}
          </div>
          <div className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-muted">
            {hovItem.goal}
          </div>
          {hovItem.constraints && (
            <div className="mt-0.5 whitespace-pre-wrap break-words text-[10px] leading-relaxed text-faint">
              {hovItem.constraints}
            </div>
          )}
          {(hovItem.depends_on?.length ?? 0) > 0 && (
            <div className="mt-0.5 text-[10px] text-faint">
              ⇐ {hovItem.depends_on!.join(", ")}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
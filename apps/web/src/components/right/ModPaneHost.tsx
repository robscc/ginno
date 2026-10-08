"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import { ModNodeView } from "@/components/chat/mod/modElements";
import {
  sendModPanePress,
  subscribeModPanes,
  type ModPaneSnapshot,
} from "@/components/chat/mod/paneBus";

/**
 * 右栏 mods pane 宿主(claude-code-mods-design.md §7.4):RightPanel 内容区
 * 顶部的一块 pane 区,内部自带 tab 条(每个打开的 pane 一个 tab)。帧经
 * paneBus(modToast 同款模块级总线)到达;只显示当前会话的 pane,会话切换
 * 即清;press 沿 engine 的 socket 回发(sendModPanePress)。
 *
 * 树是不可信数据(mod 产出):渲染交给 modElements 的白名单渲染器。
 */

export function ModPaneHost() {
  const t = useTranslations("right.panel");
  const g = useGinno();
  const sid = g.activeSessionId;
  // 全部会话的 pane(帧先落这里),展示时过滤当前会话。
  const [panesBySid, setPanesBySid] = useState<Record<string, Record<string, ModPaneSnapshot>>>({});
  // 每个会话当前激活的 pane tab。
  const [activeBySid, setActiveBySid] = useState<Record<string, string>>({});

  useEffect(
    () =>
      subscribeModPanes((frameSid, kind, pane) => {
        setPanesBySid((prev) => {
          const forSid = { ...(prev[frameSid] ?? {}) };
          if (kind === "close") delete forSid[pane.id];
          else forSid[pane.id] = pane;
          return { ...prev, [frameSid]: forSid };
        });
        if (kind === "open") setActiveBySid((prev) => ({ ...prev, [frameSid]: pane.id }));
      }),
    [],
  );

  // 归属守卫:只保留当前会话的 pane(会话切换 = 移除,与 mod.ask 同语义)。
  const [localClosed, setLocalClosed] = useState<Record<string, string[]>>({});
  const curSid = sid ?? "";
  const panes = Object.values(curSid ? (panesBySid[curSid] ?? {}) : {}).filter(
    (p) => !(localClosed[curSid] ?? []).includes(p.id),
  );
  useEffect(() => {
    setPanesBySid((prev) => (sid ? (sid in prev ? { [sid]: prev[sid] } : {}) : {}));
  }, [sid]);

  if (panes.length === 0) return null;
  const activeId = curSid ? activeBySid[curSid] : undefined;
  const active = panes.find((p) => p.id === activeId) ?? panes[0];

  return (
    <div className="flex max-h-[45%] shrink-0 flex-col border-b border-line bg-panel">
      {/* pane tab 条:标题 + mod 名小标签 */}
      <div role="tablist" aria-label={t("modPaneTitle")} className="flex items-center gap-1 overflow-x-auto px-2 pt-2">
        {panes.map((p) => (
          <button
            key={p.id}
            role="tab"
            aria-selected={active.id === p.id}
            title={p.title}
            onClick={() => curSid && setActiveBySid((prev) => ({ ...prev, [curSid]: p.id }))}
            className={`flex shrink-0 items-center gap-1.5 rounded-t-lg border border-b-0 px-2 py-1 text-xs transition-colors ${
              active.id === p.id ? "border-line bg-card text-txt" : "border-transparent text-muted hover:text-txt"
            }`}
          >
            <span className="max-w-40 truncate font-medium">{p.title}</span>
            {p.mod && (
              <span className="rounded bg-violet/15 px-1 py-px font-mono text-[9px] leading-none text-violet">
                {p.mod}
              </span>
            )}
          </button>
        ))}
        <button
          onClick={() => sid && setLocalClosed((prev) => ({ ...prev, [sid]: [...(prev[sid] ?? []), active.id] }))}
          aria-label={t("modPaneClose")}
          title={t("modPaneClose")}
          className="ml-auto shrink-0 rounded p-1 text-faint hover:text-txt"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      {/* pane 内容:白名单树渲染;press 回发(过期代际由 broker 丢弃) */}
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-3">
        {active.tree.map((node, i) => (
          <ModNodeView
            key={i}
            node={node}
            onPress={(actionId, value) => curSid && sendModPanePress(curSid, active.generation, actionId, value)}
          />
        ))}
      </div>
    </div>
  );
}

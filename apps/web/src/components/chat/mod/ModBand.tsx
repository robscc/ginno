"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ModNodeView, type ModBandSnapshot } from "./modElements";

/**
 * 单条 mod band 容器(claude-code-mods-design.md §7.1):composer 上方的 mod
 * 渲染区。mod 标签可选(快照带 mod 名才显示),主体递归渲染序列化树;
 * Button 的 press 经 onPress 回 ChatStream → engine 回发 `mod.ui.press` 帧。
 */
export function ModBand({
  snapshot,
  onPress,
}: {
  snapshot: ModBandSnapshot;
  onPress: (actionId: string) => void;
}) {
  const t = useTranslations("chat.composer");
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div className="rounded-lg border border-line bg-card2/40 px-2.5 py-1.5">
      <div className="flex items-center gap-1.5">
        <button
          onClick={() => setCollapsed((c) => !c)}
          className="rounded p-0.5 text-faint hover:text-muted"
          aria-label={collapsed ? t("modBandExpand") : t("modBandCollapse")}
          title={collapsed ? t("modBandExpand") : t("modBandCollapse")}
        >
          {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </button>
        {snapshot.mod && (
          <span className="rounded bg-line/60 px-1.5 py-px font-mono text-[10px] text-muted">
            {snapshot.mod}
          </span>
        )}
        <span className="text-[10px] text-faint">#{snapshot.generation}</span>
      </div>
      {!collapsed && (
        <div className="mt-1 text-xs text-txt">
          {snapshot.tree.length === 0 ? (
            <span className="text-faint">∅</span>
          ) : (
            snapshot.tree.map((node, i) => (
              <ModNodeView key={i} node={node} onPress={onPress} />
            ))
          )}
        </div>
      )}
    </div>
  );
}

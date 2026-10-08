"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronRight } from "lucide-react";
import { ModNodeView, type SerializedNode } from "./modElements";

/**
 * 一个 mod 的 band 分段(claude-code-mods-design.md §7.1):broker 的
 * SurfaceTable 对每个注册了 ui.render hook 的 mod 各走一条链,各得一棵树、
 * 各有各的 generation(press 校验也按 mod 走——actionId 自带 `<mod>:` 前缀)。
 */
export interface ModBandGroup {
  /** 来源 mod 名(broker 的 mods 数组必带;旧单树帧可选)。 */
  mod?: string;
  generation: number;
  tree: (SerializedNode | string)[];
}

/**
 * mod band 容器(§7.1):composer 上方的 mod 渲染区。多 mod 各占一段,间距
 * 分隔、mod 名做小标签;空树段不渲染。主体递归渲染序列化树;Button 的
 * press 带着所属段的 generation 回 ChatStream → engine 回发 `mod.ui.press`。
 */
export function ModBand({
  groups,
  onPress,
}: {
  groups: ModBandGroup[];
  onPress: (actionId: string, generation: number) => void;
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
      </div>
      {!collapsed && (
        <div className="mt-1 text-xs text-txt">
          {groups.map((group, gi) => (
            <div
              key={group.mod ?? gi}
              className={gi > 0 ? "mt-2 border-t border-line/60 pt-2" : undefined}
            >
              {(group.mod || gi > 0) && (
                <div className="mb-1 flex items-center gap-1.5">
                  {group.mod && (
                    <span className="rounded bg-line/60 px-1.5 py-px font-mono text-[10px] text-muted">
                      {group.mod}
                    </span>
                  )}
                  <span className="text-[10px] text-faint">#{group.generation}</span>
                </div>
              )}
              {group.tree.length === 0 ? (
                <span className="text-faint">∅</span>
              ) : (
                group.tree.map((node, i) => (
                  <ModNodeView key={i} node={node} onPress={(actionId) => onPress(actionId, group.generation)} />
                ))
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

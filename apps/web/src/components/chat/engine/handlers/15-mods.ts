// 15-mods — Claude Code Mods 事件（claude-code-mods-design.md §7）。
// 等价迁移自 handle() switch（批次 6-2），case 体逐字一致。
// 归属守卫:frame_session 检查已在 socket onmessage 里统一做过,这里的
// sid 即归属会话,无需再防串线。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import type { ModBandGroup } from "@/components/chat/mod/ModBand";
import type { ModAskState } from "@/components/chat/mod/ModAskCard";
import { pushNotice, type NoticeLevel } from "@/lib/notice";
import { pushModPane, type ModPaneSnapshot } from "@/components/chat/mod/paneBus";

export function onModBands(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 整树替换(§7.1 per-mod:每 mod 一段,generation 按 mod 递增,过期
  // press 由 broker 侧按 mod 校验忽略)。旧帧形状(单树 {generation,
  // tree, mod})兜底成一段,树空则清空。
  const groups: ModBandGroup[] = [];
  if (Array.isArray(ev.mods)) {
    for (const entry of ev.mods) {
      if (!entry || typeof entry !== "object") continue;
      const tree = Array.isArray(entry.tree) ? (entry.tree as ModBandGroup["tree"]) : [];
      if (tree.length === 0) continue; // 空树 mod 不占位
      groups.push({
        mod: typeof entry.mod === "string" ? entry.mod : undefined,
        generation: Number(entry.generation) || 0,
        tree,
      });
    }
  } else if (Array.isArray(ev.tree)) {
    const tree = ev.tree as ModBandGroup["tree"];
    if (tree.length > 0) {
      groups.push({
        mod: typeof ev.mod === "string" ? ev.mod : undefined,
        generation: Number(ev.generation) || 0,
        tree,
      });
    }
  }
  ctx.setModBands((prev) => ({ ...prev, [sid]: groups }));
}

export function onModToast(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 收编进全局 notice 总线（notice-unification-design.md §4）：ModToastHost
  // 已退役，右下角统一容器渲染。事件契约不变：带 i18n_key 时落地翻译
  // (evText)，否则成品文本原样直显（broker toast / mod 自己的文案）。
  // 帧里没有稳定的 mod id 可做 dedupeKey，不传——保留多条并存语义
  //（NOTICE_MAX=3 堆叠），避免不同文案被 TTL 去重合并。
  const level: NoticeLevel = ev.level === "warn" || ev.level === "error" ? ev.level : "info";
  pushNotice(level, ctx.i18nRef.current.evText(ev, String(ev.text ?? "")), { source: "mod" });
}

export function onModStateChanged(ctx: EngineCtx, sid: string) {
  // mod 装载/启停/配置变化:设置页若开着就重拉;没有对应的 store 动作,
  // 页面不在时该事件自然无处消费,静默。
  try {
    window.dispatchEvent(new CustomEvent("ginno:mods-state-changed"));
  } catch {
    /* 非浏览器环境(SSG) */
  }
}

export function onModPromptFill(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // $.prompt.fill(mods ops):接管 composer 草稿(fill 语义=覆盖,与委派卡
  // 的追加预填不同,走专用事件,ChatStream 侧替换落字)。
  const text = String(ev.text ?? "");
  if (!text) return;
  try {
    window.dispatchEvent(
      new CustomEvent("ginno:mod-fill-input", { detail: { sessionId: sid, text } }),
    );
  } catch {
    /* 非浏览器环境(SSG) */
  }
}

export function onModPromptSuggest(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // $.prompt.suggest(mods ops):幽灵建议的 v1 落地——composer 为空才
  // 落字(建议语义),非空丢弃不打扰。ChatStream 侧裁决空与否。
  const text = String(ev.text ?? "");
  if (!text) return;
  try {
    window.dispatchEvent(
      new CustomEvent("ginno:suggest-input", { detail: { sessionId: sid, text } }),
    );
  } catch {
    /* 非浏览器环境(SSG) */
  }
}

export function onModAsk(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // $.ui.ask(§7.3):问题浮层,按 sid 存最新一条。choices 可为字符串,
  // 也兼容 {label}/{value} 对象形状;自由输入恒可用。
  const rawChoices = Array.isArray(ev.choices) ? ev.choices : [];
  const ask: ModAskState = {
    id: String(ev.id ?? ""),
    mod: typeof ev.mod === "string" ? ev.mod : undefined,
    message: String(ev.message ?? ""),
    choices: rawChoices
      .map((c) =>
        typeof c === "string"
          ? c
          : c && typeof c === "object"
            ? String((c as { label?: unknown; value?: unknown }).label ??
              (c as { value?: unknown }).value ?? "")
            : "",
      )
      .filter((c) => c !== ""),
  };
  if (!ask.id) return; // 无法回发的问题直接丢弃
  ctx.setModAsks((prev) => ({ ...prev, [sid]: ask }));
}

/** mod.pane.open / update / close 共享体（§7.4,P2）:右栏 pane 区,经 paneBus
 *  交给 ModPaneHost;本 handler 只做转发,归属(会话切换移除)由宿主按 sid
 *  自行守卫。 */
export function onModPane(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  const kind = ev.event === "mod.pane.open" ? "open" : ev.event === "mod.pane.update" ? "update" : "close";
  pushModPane(sid, kind, {
    id: String(ev.id ?? ""),
    title: typeof ev.title === "string" ? ev.title : String(ev.id ?? ""),
    mod: typeof ev.mod === "string" ? ev.mod : undefined,
    generation: Number(ev.generation) || 0,
    tree: Array.isArray(ev.tree) ? (ev.tree as ModPaneSnapshot["tree"]) : [],
  });
}

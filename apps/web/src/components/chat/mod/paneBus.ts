"use client";

/**
 * Mod pane 事件总线(claude-code-mods-design.md §7.4):ModToastHost 同款
 * 模块级总线——engine 的 WS 分发在任何组件树之外也能推送,右栏的 ModPaneHost
 * 挂载后订阅;press 回发沿 engine 已有的 socket(注册 sender,而不是右栏自己
 * 开连接)。pane 是 session-scoped 的:每一帧都带归属会话 id,宿主只显示当前
 * 会话的 pane,会话切换即清(与 mod.ask 同一归属守卫语义)。
 */

import type { SerializedNode } from "./modElements";

/** 一个打开的 pane(WS `mod.pane.open`/`update` 帧的累积状态)。 */
export interface ModPaneSnapshot {
  id: string;
  title: string;
  /** 来源 mod 名(小标签)。 */
  mod?: string;
  generation: number;
  tree: (SerializedNode | string)[];
}

export type ModPaneKind = "open" | "update" | "close";

export type ModPaneListener = (sid: string, kind: ModPaneKind, pane: ModPaneSnapshot) => void;

/** press 回发签名:value 仅 Input/Select 提交时携带(随帧多余字段透传)。 */
export type ModPanePressSender = (sid: string, generation: number, actionId: string, value?: string) => void;

let listeners: ModPaneListener[] = [];
let pressSender: ModPanePressSender | undefined;

/** engine 的 `mod.pane.*` 分支调用;宿主未挂载时帧自然丢弃。 */
export function pushModPane(sid: string, kind: ModPaneKind, pane: ModPaneSnapshot): void {
  for (const l of listeners) {
    try {
      l(sid, kind, pane);
    } catch {
      /* 单个订阅者异常不影响其他 */
    }
  }
}

/** engine 挂载时注册 press 回发(沿用 sendModUiPress 的 socket 路径)。 */
export function registerModPanePressSender(sender: ModPanePressSender): void {
  pressSender = sender;
}

/** ModPaneHost 的按钮/输入提交:sender 未注册(socket 未起)时静默。 */
export function sendModPanePress(sid: string, generation: number, actionId: string, value?: string): void {
  try {
    pressSender?.(sid, generation, actionId, value);
  } catch {
    /* socket closing */
  }
}

export function subscribeModPanes(listener: ModPaneListener): () => void {
  listeners.push(listener);
  return () => {
    listeners = listeners.filter((l) => l !== listener);
  };
}

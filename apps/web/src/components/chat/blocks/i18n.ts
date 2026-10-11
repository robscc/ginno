"use client";

import { useTranslations } from "next-intl";

/** chat 域翻译 + 动态 key 收敛。next-intl 的 key 类型约束只覆盖字面量；运行态
 *  key（状态枚举、runtime 事件带來的 dot 路径）经一次安全转型走 string 签名，
 *  存在性用 has 前置校验，未命中由调用方回退原文。 */
export function useChatT() {
  const tc = useTranslations("chat");
  const tr = tc as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  return { tc, tr };
}

/**
 * 渲染 runtime 事件文本的契约（i18n-design.md §3 数据契约）：
 * 事件可带 `i18n_key`（dot 路径，可能落在 chat / errors / 任意 messages 域）
 * 与 `params`——key 命中即翻译；key 未命中、params 缺失或无 i18n_key 时
 * 原样回退 fallback（历史会话重放的是成品字符串，不坏）。
 * 必须在 client 组件内调用（useTranslations 的 SSG 约束）。
 */
export function useEventI18nText() {
  const tRoot = useTranslations(); // 根级：跨域 key
  const tr = tRoot as unknown as {
    (key: string, values?: Record<string, string | number>): string;
    has(key: string): boolean;
  };
  return (
    // 索引签名让任意事件帧 / Block 都可直接传入（也规避 weak type 检查）
    ev: { i18n_key?: unknown; params?: unknown; [k: string]: unknown } | null | undefined,
    fallback: string,
  ): string => {
    if (!ev) return fallback;
    const key = typeof ev.i18n_key === "string" ? ev.i18n_key : "";
    if (!key || !tr.has(key)) return fallback;
    try {
      const params = (ev.params ?? undefined) as Record<string, string | number> | undefined;
      return tr(key, params);
    } catch {
      return fallback; // params 结构异常等——翻译永不挂掉渲染
    }
  };
}

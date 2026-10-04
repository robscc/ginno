/**
 * ui 域文案的非 hook 读取器。
 *
 * lib 层纯函数（runtime.ts / store.tsx / utils.ts 等）无法调用 useTranslations
 * （hook 只能在组件渲染期用），这些位置的用户可见文案改走本模块。架构与
 * toolLabels.ts 一致：静态 import 双语言 catalog + locale 镜像，由 i18n provider
 * 在 locale 变化时调 setUiTextLocale() 同步（渲染期镜像赋值，首帧即正确；
 * SSG 构建期无 provider，默认 en，与预渲染 HTML 一致）。
 *
 * key 与 useTranslations("ui") 完全同名（如 "common.cancel" / "net.unreachable"），
 * 类型上收敛为 en catalog 的点路径并集，拼错 key 编译失败。
 */

import uiEn from "../../messages/en/ui.json";
import uiZh from "../../messages/zh-CN/ui.json";
import { defaultLocale, type Locale } from "./config";

type UiTree = Record<string, unknown>;

const CATALOGS: Record<Locale, UiTree> = {
  en: uiEn.ui as UiTree,
  "zh-CN": uiZh.ui as UiTree,
};

/** en catalog（source 语言）的叶子点路径并集，如 "relTime.minAgo"。 */
type DotPaths<T> = T extends string
  ? never
  : { [K in keyof T & string]: T[K] extends string ? K : `${K}.${DotPaths<T[K]>}` }[keyof T &
      string];

export type UiKey = DotPaths<(typeof uiEn)["ui"]>;

let _locale: Locale = defaultLocale;

/** provider 在 locale 变化时同步（含首帧渲染期赋值，保证非 hook 调用即时生效）。 */
export function setUiTextLocale(locale: Locale): void {
  _locale = locale;
}

/** 按 a.b.c 路径取叶子字符串；路径中断或叶子非字符串返回 undefined。 */
function lookup(tree: UiTree, key: string): string | undefined {
  let node: unknown = tree;
  for (const seg of key.split(".")) {
    if (typeof node !== "object" || node === null) return undefined;
    node = (node as UiTree)[seg];
  }
  return typeof node === "string" ? node : undefined;
}

/**
 * 非 hook 场景读取 ui 域文案。fallback 链与 next-intl 一致：
 * 当前 locale → en → key 原样。params 做 {name} 简单插值（当前 ui catalog
 * 仅需位置插值，无 ICU 复数需求；P3 打磨复数时再升级）。
 */
export function uiText(key: UiKey, params?: Record<string, string | number>): string {
  const raw = lookup(CATALOGS[_locale], key) ?? lookup(CATALOGS[defaultLocale], key) ?? key;
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (m, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : m,
  );
}

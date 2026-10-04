/**
 * sched 域文案的非 hook 读取器（架构同 uiText.ts）。
 *
 * scheduled/shared.ts 是纯函数模块（不碰 React），无法调用 useTranslations；
 * 其中的状态名（statusMeta）、日期前缀（fmtDayPrefix）、计划描述（planDescription）
 * 等用户可见文案改走本模块。静态 import 双语言 catalog + locale 镜像，由 i18n
 * provider 在 locale 变化时调 setSchedTextLocale() 同步（渲染期镜像赋值，首帧
 * 即正确；SSG 构建期无 provider，默认 en，与预渲染 HTML 一致）。
 *
 * key 与 useTranslations("sched") 完全同名（如 "status.ok" / "dayPrefix.today"），
 * 类型上收敛为 en catalog 的点路径并集，拼错 key 编译失败。
 */

import schedEn from "../../messages/en/sched.json";
import schedZh from "../../messages/zh-CN/sched.json";
import { defaultLocale, type Locale } from "./config";

type SchedTree = Record<string, unknown>;

const CATALOGS: Record<Locale, SchedTree> = {
  en: schedEn.sched as SchedTree,
  "zh-CN": schedZh.sched as SchedTree,
};

/** en catalog（source 语言）的叶子点路径并集，如 "status.ok"。 */
type DotPaths<T> = T extends string
  ? never
  : { [K in keyof T & string]: T[K] extends string ? K : `${K}.${DotPaths<T[K]>}` }[keyof T &
      string];

export type SchedKey = DotPaths<(typeof schedEn)["sched"]>;

let _locale: Locale = defaultLocale;

/** provider 在 locale 变化时同步（含首帧渲染期赋值，保证非 hook 调用即时生效）。 */
export function setSchedTextLocale(locale: Locale): void {
  _locale = locale;
}

/** 按 a.b.c 路径取叶子字符串；路径中断或叶子非字符串返回 undefined。 */
function lookup(tree: SchedTree, key: string): string | undefined {
  let node: unknown = tree;
  for (const seg of key.split(".")) {
    if (typeof node !== "object" || node === null) return undefined;
    node = (node as SchedTree)[seg];
  }
  return typeof node === "string" ? node : undefined;
}

/**
 * 非 hook 场景读取 sched 域文案。fallback 链与 next-intl 一致：
 * 当前 locale → en → key 原样。params 做 {name} 简单插值。
 */
export function schedText(key: SchedKey, params?: Record<string, string | number>): string {
  const raw = lookup(CATALOGS[_locale], key) ?? lookup(CATALOGS[defaultLocale], key) ?? key;
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (m, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : m,
  );
}

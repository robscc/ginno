/**
 * tasks 域文案的非 hook 读取器（架构同 uiText.ts / schedText.ts）。
 *
 * lib/store/notifications.ts 的后台任务通知是纯函数模块（无 React），经本
 * 模块取当前 locale 文案。key 与 useTranslations("tasks") 同名。
 */

import tasksEn from "../../messages/en/tasks.json";
import tasksZh from "../../messages/zh-CN/tasks.json";
import { defaultLocale, type Locale } from "./config";

type Tree = Record<string, unknown>;

const CATALOGS: Record<Locale, Tree> = {
  en: tasksEn.tasks as Tree,
  "zh-CN": tasksZh.tasks as Tree,
};

/** en catalog（source 语言）的叶子点路径并集。 */
type DotPaths<T> = T extends string
  ? never
  : { [K in keyof T & string]: T[K] extends string ? K : `${K}.${DotPaths<T[K]>}` }[keyof T &
      string];

export type TasksKey = DotPaths<(typeof tasksEn)["tasks"]>;

let _locale: Locale = defaultLocale;

/** provider 在 locale 变化时同步（含首帧渲染期赋值）。 */
export function setTasksTextLocale(locale: Locale): void {
  _locale = locale;
}

/** 按 a.b.c 路径取叶子字符串；路径中断或叶子非字符串返回 undefined。 */
function leaf(tree: Tree, key: string): string | undefined {
  let node: unknown = tree;
  for (const part of key.split(".")) {
    if (typeof node !== "object" || node === null) return undefined;
    node = (node as Tree)[part];
  }
  return typeof node === "string" ? node : undefined;
}

/** 读取 tasks 域文案；key 未命中/参数缺失时回退英文再回退 key 本身。 */
export function tasksText(key: TasksKey, params?: Record<string, string>): string {
  let out = leaf(CATALOGS[_locale], key) ?? leaf(CATALOGS.en, key) ?? String(key);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      out = out.replaceAll(`{${k}}`, v);
    }
  }
  return out;
}

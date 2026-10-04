"use client";

/**
 * i18n Provider（next-intl 无 i18n 路由模式）。
 *
 * ⚠️ 约束（i18n-design.md §4.4）：静态导出（output: "export"）构建期 SSG 的
 * locale 固定为 en，因此所有使用 useTranslations 的组件必须是 client 组件
 * （文件顶部 "use client"），否则文案会在构建期固化为英文、运行时无法切换。
 * Ginno 前端本就近乎全 client，新增 server 组件时必须遵守此约束。
 *
 * locale 状态（i18n-design.md §4.1/4.2）：
 *   - 初始固定 en（与 SSG 预渲染 HTML 水合一致）；水合后、绘制前的布局阶段
 *     同步读 localStorage["ginno.language"] 镜像恢复（无则 auto→按系统语言
 *     解析），不闪帧也不等 settings 网络往返；
 *   - 挂载后拉取 settings 校正；
 *   - setLocale() 沿用 GeneralSettings 保存设置的同一 API 通道
 *     （GET /settings → 改 language → PUT /settings），同时写 localStorage
 *     镜像与 context，全树重渲染，不刷新页面。
 *
 * messages：直接静态 import messages/{locale}/ 下各域 json 并 deep-merge
 * 成单对象传给 NextIntlClientProvider——必然进静态导出产物，无漏打包风险。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import {
  NextIntlClientProvider,
  type AbstractIntlMessages,
  type Messages,
} from "next-intl";
import { getSettings, putSettings } from "@/lib/runtime";
import { setToolLabelsLocale } from "@/lib/toolLabels";
import { setUiTextLocale } from "./uiText";
import { setSchedTextLocale } from "./schedText";
import {
  LANGUAGE_LS_KEY,
  defaultLocale,
  normalizeLanguageSetting,
  resolveAuto,
  type LanguageSetting,
  type Locale,
} from "./config";

import coreEn from "../../messages/en/core.json";
import settingsEn from "../../messages/en/settings.json";
import toolEn from "../../messages/en/tool.json";
import chatEn from "../../messages/en/chat.json";
import composerEn from "../../messages/en/composer.json";
import uiEn from "../../messages/en/ui.json";
import connEn from "../../messages/en/conn.json";
import kbEn from "../../messages/en/kb.json";
import codeEn from "../../messages/en/code.json";
import rightEn from "../../messages/en/right.json";
import shellEn from "../../messages/en/shell.json";
import streamEn from "../../messages/en/stream.json";
import summaryEn from "../../messages/en/summary.json";
import coreZh from "../../messages/zh-CN/core.json";
import settingsZh from "../../messages/zh-CN/settings.json";
import toolZh from "../../messages/zh-CN/tool.json";
import chatZh from "../../messages/zh-CN/chat.json";
import composerZh from "../../messages/zh-CN/composer.json";
import uiZh from "../../messages/zh-CN/ui.json";
import connZh from "../../messages/zh-CN/conn.json";
import kbZh from "../../messages/zh-CN/kb.json";
import codeZh from "../../messages/zh-CN/code.json";
import rightZh from "../../messages/zh-CN/right.json";
import shellZh from "../../messages/zh-CN/shell.json";
import streamZh from "../../messages/zh-CN/stream.json";
import summaryZh from "../../messages/zh-CN/summary.json";
import pinEn from "../../messages/en/pin.json";
import toolsEn from "../../messages/en/tools.json";
import extEn from "../../messages/en/ext.json";
import pinZh from "../../messages/zh-CN/pin.json";
import toolsZh from "../../messages/zh-CN/tools.json";
import extZh from "../../messages/zh-CN/ext.json";
import wfEn from "../../messages/en/wf.json";
import wfZh from "../../messages/zh-CN/wf.json";
import schedEn from "../../messages/en/sched.json";
import schedZh from "../../messages/zh-CN/sched.json";
import goalEn from "../../messages/en/goal.json";
import goalZh from "../../messages/zh-CN/goal.json";

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 深合并 catalog 片段，后者叶子值优先。当前各域文件顶层键互不重叠，
 * 深合并是为将来可能出现的跨文件共享子树兜底。
 * 支持多 source 变参：各域任务向 merge 链尾追加域文件时直接并列传入。 */
function deepMerge(
  target: Record<string, unknown>,
  ...sources: Array<Record<string, unknown>>
): Record<string, unknown> {
  let out: Record<string, unknown> = { ...target };
  for (const source of sources) {
    for (const [k, v] of Object.entries(source)) {
      const prev = out[k];
      out[k] = isPlainObject(prev) && isPlainObject(v) ? deepMerge(prev, v) : v;
    }
  }
  return out;
}

const MESSAGES: Record<Locale, Messages> = {
  en: deepMerge(
    deepMerge(
      deepMerge(
        deepMerge(deepMerge(deepMerge(coreEn, settingsEn), toolEn), chatEn),
        composerEn,
      ),
      uiEn,
    ),
    deepMerge(deepMerge(deepMerge(connEn, kbEn), rightEn), codeEn),
    shellEn,
    streamEn,
    summaryEn,
    pinEn,
    toolsEn,
    extEn,
    wfEn,
    schedEn,
    goalEn,
  ) as Messages,
  "zh-CN": deepMerge(
    deepMerge(
      deepMerge(
        deepMerge(deepMerge(deepMerge(coreZh, settingsZh), toolZh), chatZh),
        composerZh,
      ),
      uiZh,
    ),
    deepMerge(deepMerge(deepMerge(connZh, kbZh), rightZh), codeZh),
    shellZh,
    streamZh,
    summaryZh,
    pinZh,
    toolsZh,
    extZh,
    wfZh,
    schedZh,
    goalZh,
  ) as Messages,
};

/** 系统语言 → effective locale（SSG/node 环境无 navigator，落 en）。 */
function systemLocale(): Locale {
  return typeof navigator !== "undefined" ? resolveAuto(navigator.language) : defaultLocale;
}

function effectiveOf(setting: LanguageSetting): Locale {
  return setting === "auto" ? systemLocale() : setting;
}

/** 读 localStorage 镜像（水合前布局阶段调用）；读不到/非法值按 "auto"。
 * SSG 期间无 window，不会走到这里。 */
function readStoredSetting(): LanguageSetting {
  try {
    const raw = window.localStorage.getItem(LANGUAGE_LS_KEY);
    if (raw === "en" || raw === "zh-CN" || raw === "auto") return raw;
  } catch {
    /* localStorage 不可用（隐私模式等）——按 "auto" */
  }
  return "auto";
}

// 客户端用 useLayoutEffect：在水合提交后、浏览器绘制前恢复镜像 locale——
// 既有 SSR/SSG HTML（en）水合一致（无 mismatch 告警），又不见英文闪帧；
// 服务端退化为 useEffect（no-op），避免 React 的 useLayoutEffect SSR 告警。
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? useLayoutEffect : useEffect;

// ---- 非 hook 场景的 locale 读取（模块级镜像） ----

let _currentLocale: Locale = defaultLocale;

/** 当前生效 locale 的同步读取。供发往模型的选边文案等非 hook 场景使用。 */
export function currentLocale(): Locale {
  return _currentLocale;
}

/**
 * 模型可见模板的中英选边（原 @/lib/promptLang 的 t）——改读 i18n 的
 * effective locale。仅用于发送给 LLM 的组合文案；纯 UI 文案请用 useTranslations。
 */
export function t(en: string, zh: string): string {
  return _currentLocale === "zh-CN" ? zh : en;
}

// ---- setLocale：模块级导出 + context 双通道 ----

type ApplyLocale = (v: LanguageSetting) => Promise<void>;
let _applyLocale: ApplyLocale | null = null;

/**
 * 切换语言（auto/en/zh-CN）：写 settings API（get→改→put，同 GeneralSettings
 * 既有通道）+ localStorage 镜像 + context 更新，不刷新页面。非 hook 场景可直调；
 * settings 写失败时 reject（UI locale 已先行生效），调用方决定如何提示。
 */
export function setLocale(v: LanguageSetting): Promise<void> {
  return _applyLocale ? _applyLocale(v) : Promise.resolve();
}

type LocaleCtxValue = {
  /** 解析后的生效 locale（auto 已展开）。 */
  locale: Locale;
  /** settings.language 原始值（含 "auto"）。 */
  languageSetting: LanguageSetting;
  setLocale: ApplyLocale;
};

const LocaleCtx = createContext<LocaleCtxValue>({
  locale: defaultLocale,
  languageSetting: "auto",
  setLocale: () => Promise.resolve(),
});

/** 读取 locale context（languageSetting 含 "auto"，locale 为解析后的生效值）。 */
export function useLocaleCtx(): LocaleCtxValue {
  return useContext(LocaleCtx);
}

export function I18nProvider({ children }: { children: React.ReactNode }) {
  // 初始固定 en：与 SSG 预渲染 HTML 一致（水合安全）；镜像 locale 在
  // 水合后、绘制前由布局阶段恢复，无闪帧也不等 settings 网络往返。
  const [languageSetting, setLanguageSetting] = useState<LanguageSetting>(defaultLocale);
  const locale = effectiveOf(languageSetting);

  // 首帧布局阶段：恢复 localStorage 镜像（"auto" 时按 navigator.language 解析）
  useIsomorphicLayoutEffect(() => {
    setLanguageSetting(readStoredSetting());
  }, []);

  // 模块级镜像：渲染期同步赋值（幂等），保证非 hook 的 t() 从首帧起读到正确 locale
  _currentLocale = locale;
  // ui / sched 域非 hook 读取器的 locale 镜像，同一时机同步
  setUiTextLocale(locale);
  setSchedTextLocale(locale);

  const applyLocale = useCallback(async (v: LanguageSetting) => {
    // 先落本地（UI 即时切换），再走 settings API；API 失败不回滚 UI，
    // 与既有 toggleBypass 等 get→改→put 保存路径的"先 set 再保存"行为一致。
    try {
      window.localStorage.setItem(LANGUAGE_LS_KEY, v);
    } catch {
      /* ignore */
    }
    setLanguageSetting(v);
    const s = await getSettings();
    s.language = v;
    await putSettings(s);
  }, []);

  useEffect(() => {
    _applyLocale = applyLocale;
    return () => {
      _applyLocale = null;
    };
  }, [applyLocale]);

  // locale 变化：同步 <html lang> 与 tool 标签 catalog（toolLabels 模块缓存按 locale 重建）
  useEffect(() => {
    document.documentElement.lang = locale;
    setToolLabelsLocale(locale);
  }, [locale]);

  // 挂载后拉取 settings 校正 locale（镜像可能缺失或过期；写回镜像减少下次首帧偏差）
  useEffect(() => {
    let cancelled = false;
    getSettings()
      .then((s) => {
        if (cancelled) return;
        const v = normalizeLanguageSetting(s.language);
        try {
          window.localStorage.setItem(LANGUAGE_LS_KEY, v);
        } catch {
          /* ignore */
        }
        setLanguageSetting(v);
      })
      .catch(() => {
        /* runtime 不可达——维持本地镜像的 locale */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const ctx = useMemo(
    () => ({ locale, languageSetting, setLocale: applyLocale }),
    [locale, languageSetting, applyLocale],
  );

  return (
    <LocaleCtx.Provider value={ctx}>
      {/* timeZone 固定 UTC：仅用于消除 SSG 构建期 use-intl 的
          ENVIRONMENT_FALLBACK 告警并保证预渲染确定性；当前无日期格式化调用，
          P3 打磨日期/数字时再按用户时区（settings 或本地时区）接管。 */}
      <NextIntlClientProvider
        locale={locale}
        timeZone="UTC"
        messages={MESSAGES[locale] as AbstractIntlMessages}
      >
        {children}
      </NextIntlClientProvider>
    </LocaleCtx.Provider>
  );
}

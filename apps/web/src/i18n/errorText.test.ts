/* errorText（apiErrorText）纯逻辑单测：后端直通错误串 → 前端 catalog
 * 双语映射。已知串（knowledge 类 + 搜索引擎配置类）按前缀匹配取当前
 * locale 文案；未知 / 动态 / 空串原样返回，调用方的 `|| t("fallback")`
 * 兜底语义不变。 */

import { afterEach, describe, expect, it } from "vitest";
import { apiErrorText, setErrorTextLocale } from "./errorText";
import coreEn from "../../messages/en/core.json";
import coreZh from "../../messages/zh-CN/core.json";

afterEach(() => {
  setErrorTextLocale("en");
});

describe("apiErrorText：已知错误串本地化", () => {
  it("knowledge not configured → zh 双语句子（kb 页 12 个端点共用此串）", () => {
    setErrorTextLocale("zh-CN");
    expect(apiErrorText("knowledge not configured")).toBe(
      (coreZh.errors as Record<string, string>).knowledgeNotConfigured,
    );
    expect(apiErrorText("knowledge not configured")).not.toBe("knowledge not configured");
  });

  it("knowledge vault not configured 优先于 knowledge not configured（长前缀先匹配）", () => {
    setErrorTextLocale("zh-CN");
    expect(apiErrorText("knowledge vault not configured")).toBe(
      (coreZh.errors as Record<string, string>).knowledgeVaultNotConfigured,
    );
  });

  it("en locale 取 en catalog（文案即后端原串语义）", () => {
    expect(apiErrorText("knowledge not configured")).toBe(
      (coreEn.errors as Record<string, string>).knowledgeNotConfigured,
    );
  });

  it("带动态尾巴的串：searxng/tavily 前缀命中，括号里的设置路径不参与匹配", () => {
    setErrorTextLocale("zh-CN");
    expect(
      apiErrorText("searxng base_url not configured (settings.web.engines.searxng.base_url)"),
    ).toBe((coreZh.errors as Record<string, string>).searxngNotConfigured);
    expect(apiErrorText("tavily api_key not configured (settings.web.engines.tavily.api_key)")).toBe(
      (coreZh.errors as Record<string, string>).tavilyNotConfigured,
    );
  });

  it("大小写不敏感（后端串大小写偶有漂移也不失配）", () => {
    setErrorTextLocale("zh-CN");
    expect(apiErrorText("Knowledge Not Configured")).toBe(
      (coreZh.errors as Record<string, string>).knowledgeNotConfigured,
    );
  });
});

describe("apiErrorText：未知 / 空串原样返回", () => {
  it("未知串（如 404 类 run not found）原样返回——列出不动的一类", () => {
    expect(apiErrorText("run not found")).toBe("run not found");
  });

  it("空串 / null / undefined / 纯空白 → 空串，保留 || 兜底语义", () => {
    expect(apiErrorText("")).toBe("");
    expect(apiErrorText(null)).toBe("");
    expect(apiErrorText(undefined)).toBe("");
    expect(apiErrorText("   ")).toBe("");
  });

  it("首尾空白被 trim（匹配与显示都基于净化后的串）", () => {
    setErrorTextLocale("zh-CN");
    expect(apiErrorText("  knowledge not configured  ")).toBe(
      (coreZh.errors as Record<string, string>).knowledgeNotConfigured,
    );
  });
});

describe("catalog 对称性", () => {
  it("en/zh errors.* 键集一致且无空值（漏配 zh 不应静默回落英文）", () => {
    const en = coreEn.errors as Record<string, string>;
    const zh = coreZh.errors as Record<string, string>;
    expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
    for (const k of Object.keys(en)) {
      expect(zh[k], k).not.toBe("");
      expect(en[k], k).not.toBe("");
    }
  });
});

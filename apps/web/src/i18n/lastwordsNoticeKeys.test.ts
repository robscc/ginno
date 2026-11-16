/* lastwords 恢复提示 i18n 键的双语齐全 + 扁平无点 + 占位符一致回归。
 *
 * 仓库曾有带点 key 炸 IntlError 的事故（CLAUDE.md / @ 菜单全灭），本文件
 * 钉住新增/改名的 lastwords notice 键：en 与 zh-CN 都必须有、键名扁平
 * （不含点）、两侧 ICU 占位符集合一致（漏一个参数渲染期才炸就晚了）。
 * node 环境直接读 messages/*.json，不渲染组件。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function loadNotice(locale: string): Record<string, string> {
  const p = fileURLToPath(
    new URL(`../../messages/${locale}/chat.json`, import.meta.url),
  );
  const json = JSON.parse(readFileSync(p, "utf8")) as {
    chat?: { notice?: Record<string, string> };
  };
  return json.chat?.notice ?? {};
}

const EN = loadNotice("en");
const ZH = loadNotice("zh-CN");

// 本次新增/改名的键（扁平连字符）。lastwordsRestored 是既有键，一并纳入 parity。
const KEYS = [
  "lastwords-pending-title",
  "lastwords-pending",
  "lastwords-no-head",
  "lastwords-resume-btn",
  "lastwords-dismiss-btn",
  "lastwords-resume-ok",
  "lastwords-resume-expired",
  "lastwords-resume-failed",
  "lastwords-dismissed",
  "lastwords-dismiss-failed",
  "lastwordsRestored",
];

/** 抽取 {name} 占位符集合（忽略 ICU  plural 等复杂语法，本组键均为简单插值）。 */
function placeholders(s: string): Set<string> {
  return new Set([...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]));
}

describe("lastwords notice 键双语齐全", () => {
  it.each(KEYS)("%s 在 en 与 zh-CN 都存在且非空", (k) => {
    expect(EN[k], `en 缺 ${k}`).toBeTruthy();
    expect(ZH[k], `zh-CN 缺 ${k}`).toBeTruthy();
  });

  it("新增键扁平连字符无点（防 IntlError 事故复发）", () => {
    for (const k of KEYS) {
      if (k === "lastwordsRestored") continue; // 既有 camelCase 键，不改名
      expect(k).not.toContain(".");
      expect(k).toMatch(/^[a-z]+(-[a-z]+)*$/);
    }
  });

  it("占位符集合 en/zh-CN 一致（漏参数渲染期才炸）", () => {
    for (const k of KEYS) {
      expect(placeholders(EN[k] ?? ""), `${k} en 占位符`).toEqual(
        placeholders(ZH[k] ?? ""),
      );
    }
  });

  it("pending 文案含 head 与 bgInterrupted 两个关键占位符", () => {
    for (const dict of [EN, ZH]) {
      const ph = placeholders(dict["lastwords-pending"]);
      expect(ph.has("head")).toBe(true);
      expect(ph.has("bgInterrupted")).toBe(true);
    }
  });

  it("旧的「输入继续」误导键已退役（不再引用）", () => {
    for (const dict of [EN, ZH]) {
      expect(dict["lastwordsPending"]).toBeUndefined();
      expect(dict["lastwordsPendingTitle"]).toBeUndefined();
    }
  });
});

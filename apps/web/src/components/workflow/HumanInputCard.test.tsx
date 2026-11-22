/* Quick-fix D5 回归（源码断言风格，node 环境无 DOM，照 ConfirmModal.test.tsx）：
 * human 节点的「跳过」曾被误读为门禁语义（跳过=放行/拦截/打回）。
 * 钉住三处澄清：按钮 title、按钮行下方的常驻 hint、提交后的 receipt 文案，
 * 且 en/zh 双语 catalog 都必须携带「不等人直接继续 / 空答案 / 非门禁」语义。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(
  fileURLToPath(new URL("./HumanInputCard.tsx", import.meta.url)),
  "utf8",
);

const catalogs = {
  en: JSON.parse(
    readFileSync(fileURLToPath(new URL("../../../messages/en/wf.json", import.meta.url)), "utf8"),
  ).wf.human,
  "zh-CN": JSON.parse(
    readFileSync(
      fileURLToPath(new URL("../../../messages/zh-CN/wf.json", import.meta.url)),
      "utf8",
    ),
  ).wf.human,
};

describe("human 节点「跳过」文案澄清（quick-fix D5）", () => {
  it("跳过按钮带 title 悬浮解释（skipTitle）", () => {
    const skipBtn = SRC.match(/onClick=\{\(\) => void submit\("skip"\)\}[\s\S]*?\/>/);
    expect(skipBtn, "未找到跳过按钮").toBeTruthy();
    expect(skipBtn![0]).toContain('title={t("skipTitle")}');
  });

  it("按钮行下方常驻一行 skipHint，提交后（done 态）不再显示", () => {
    expect(SRC).toMatch(/mt-1\.5 text-\[10\.5px\] leading-relaxed text-faint">\{t\("skipHint"\)\}/);
    const hintIdx = SRC.indexOf('t("skipHint")');
    const doneReturnIdx = SRC.indexOf("if (done)");
    expect(hintIdx).toBeGreaterThan(doneReturnIdx); // hint 在 done 早退之后
  });

  it("receipt 文案 skipped 也澄清语义（空答案继续）", () => {
    expect(SRC).toContain(': t("skipped")');
    expect(catalogs.en.skipped).toMatch(/empty answer/i);
    expect(catalogs["zh-CN"].skipped).toContain("未等待输入");
    expect(catalogs["zh-CN"].skipped).toContain("照常执行");
  });

  it("en/zh 双语 skipTitle 都明确「非门禁、不拦截不打回」", () => {
    expect(catalogs.en.skipTitle).toContain("not a gate");
    expect(catalogs.en.skipTitle).toContain("empty answer");
    expect(catalogs["zh-CN"].skipTitle).toContain("不等人直接继续");
    expect(catalogs["zh-CN"].skipTitle).toContain("空答案");
    expect(catalogs["zh-CN"].skipTitle).toContain("门禁");
  });

  it("en/zh 双语 skipHint 短句同样含「继续 / 空答案 / 非门禁」", () => {
    expect(catalogs.en.skipHint).toMatch(/continue without waiting/i);
    expect(catalogs.en.skipHint).toMatch(/empty answer/i);
    expect(catalogs.en.skipHint).toMatch(/not a gate/i);
    expect(catalogs["zh-CN"].skipHint).toContain("不等人直接继续");
    expect(catalogs["zh-CN"].skipHint).toContain("非门禁");
  });
});

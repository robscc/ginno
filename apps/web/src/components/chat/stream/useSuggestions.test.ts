/* suggestionFilter 单测——渠道只读会话（type="channel"）不显示 compact 动作卡
 * （#170 readonly 的 UI 语义收尾：桌面无 composer，「立即压缩」无从谈起）。
 * 零依赖纯函数，vitest node 环境直测；hook 接线（visibleSuggestions 调用点）
 * 由本文件源码断言钉住，渲染真实性归 packaged UI e2e。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { filterVisibleSuggestions } from "./suggestionFilter";

const SRC = readFileSync(
  fileURLToPath(new URL("./useSuggestions.ts", import.meta.url)),
  "utf8",
);

const PRESSURE = {
  id: "context.pressure",
  severity: "medium" as const,
  title: "上下文接近压缩阈值",
  detail: "已到自动压缩阈值的 90%。",
  action: { type: "compact" },
};

const FAILURE_CTX = {
  id: "failure.context_length",
  severity: "high" as const,
  title: "上下文超限",
  detail: "上次运行因上下文过长失败。",
  action: { type: "compact" },
};

const FAILURE_AUTH = {
  id: "failure.auth",
  severity: "high" as const,
  title: "模型密钥被拒",
  detail: "当前 provider 的 API key 无效。",
  action: { type: "open_settings", target: "model-api" },
};

const FAILURE_POLICY = {
  id: "failure.content_policy",
  severity: "medium" as const,
  title: "内容审核拦截",
  detail: "供应商审核拦截了请求。",
  action: { type: "switch_model" },
};

const channel = { id: "ch-1", type: "channel" as const };
const main = { id: "s-1", type: "main" as const };
const noDismiss: Record<string, true> = {};

describe("filterVisibleSuggestions：渠道只读会话隐藏 compact 卡", () => {
  it("channel：context.pressure / failure.context_length（compact）都不出现", () => {
    const out = filterVisibleSuggestions([PRESSURE, FAILURE_CTX], channel, noDismiss);
    expect(out).toEqual([]);
  });

  it("channel：open_settings / switch_model 卡保留（仍可操作）", () => {
    const out = filterVisibleSuggestions([FAILURE_AUTH, FAILURE_POLICY], channel, noDismiss);
    expect(out.map((s) => s.id)).toEqual(["failure.auth", "failure.content_policy"]);
  });

  it("channel：混排时只剔除 compact 卡，顺序与其余卡不受影响", () => {
    const out = filterVisibleSuggestions(
      [FAILURE_AUTH, PRESSURE, FAILURE_POLICY, FAILURE_CTX],
      channel,
      noDismiss,
    );
    expect(out.map((s) => s.id)).toEqual(["failure.auth", "failure.content_policy"]);
  });

  it("普通会话（main）：compact 卡不受影响", () => {
    const out = filterVisibleSuggestions([PRESSURE, FAILURE_CTX], main, noDismiss);
    expect(out.map((s) => s.id)).toEqual(["context.pressure", "failure.context_length"]);
  });

  it("session 为 null（首页/无会话）返回空数组", () => {
    expect(filterVisibleSuggestions([PRESSURE], null, noDismiss)).toEqual([]);
  });

  it("dismissed 键 `${sid}:${suggId}` 命中的卡隐藏，且与 channel 过滤叠加", () => {
    const dismissed = { "s-1:context.pressure": true as const, "ch-1:failure.auth": true as const };
    expect(filterVisibleSuggestions([PRESSURE], main, dismissed)).toEqual([]);
    expect(filterVisibleSuggestions([FAILURE_AUTH, FAILURE_POLICY], channel, dismissed)).toEqual([
      FAILURE_POLICY,
    ]);
  });
});

describe("useSuggestions 接线：visibleSuggestions 走 filterVisibleSuggestions", () => {
  it("hook 导入并调用过滤函数（单点覆盖 running/非 running 两条渲染路径）", () => {
    expect(SRC).toContain('import { filterVisibleSuggestions } from "./suggestionFilter"');
    expect(SRC).toContain(
      "const visibleSuggestions = filterVisibleSuggestions(suggestions, session, suggDismissed);",
    );
  });
});

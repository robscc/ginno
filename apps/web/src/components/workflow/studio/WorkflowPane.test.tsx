/* Quick-fix D3 回归（源码断言风格，照 ConfirmModal.test.tsx——node 环境、无 DOM）：
 * dry-run 面板必须在 ok 结果里展示「循环依赖」warning（黄字、不阻断），
 * 文案走 wf.common.cycles 的 en/zh 双语 catalog，数字来自后端
 * cycles / recursion_limit 字段，而不是前端自行猜测。 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const SRC = readFileSync(
  fileURLToPath(new URL("./WorkflowPane.tsx", import.meta.url)),
  "utf8",
);

const en = JSON.parse(
  readFileSync(fileURLToPath(new URL("../../../../messages/en/wf.json", import.meta.url)), "utf8"),
);
const zh = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../../../messages/zh-CN/wf.json", import.meta.url)),
    "utf8",
  ),
);

describe("dry-run 面板循环依赖 warning（quick-fix D3）", () => {
  it("ok 结果里按 cycles>0 渲染黄色 warning 行，不改变 ok 判定", () => {
    expect(SRC).toContain("(dry.result.cycles ?? 0) > 0");
    // 警告行沿用 unreachable 同款黄字样式，且在绿色通过框内（非 error 分支）
    expect(SRC).toMatch(
      /\(dry\.result\.cycles \?\? 0\) > 0 && \(\s*<div className="selectable pl-5 text-yellow">/,
    );
  });

  it("文案参数取后端字段 cycles / recursion_limit，前端不自算", () => {
    const m = SRC.match(/tCommon\("cycles", \{[\s\S]*?\}\)/);
    expect(m, "未找到 tCommon('cycles', {...}) 调用").toBeTruthy();
    expect(m![0]).toContain("count: dry.result.cycles ?? 0");
    expect(m![0]).toContain("limit: dry.result.recursion_limit ?? 0");
  });

  it("en/zh 双语 catalog 都有 wf.common.cycles 且含 {count}/{limit} 占位", () => {
    for (const [name, cat] of [["en", en], ["zh-CN", zh]] as const) {
      const msg = cat.wf.common.cycles;
      expect(msg, `${name} 缺 wf.common.cycles`).toBeTruthy();
      expect(msg).toContain("{count}");
      expect(msg).toContain("{limit}");
    }
  });

  it("zh 文案明确「不拒绝 / recursion_limit 兜底」语义，避免读成门禁", () => {
    expect(zh.wf.common.cycles).toContain("不拒绝");
    expect(zh.wf.common.cycles).toContain("recursion_limit={limit} 兜底");
  });

  it("DryRunResult 类型暴露 cycles / recursion_limit 可选字段", () => {
    const api = readFileSync(
      fileURLToPath(new URL("../../../lib/api/workflows.ts", import.meta.url)),
      "utf8",
    );
    expect(api).toMatch(/cycles\?: number;/);
    expect(api).toMatch(/recursion_limit\?: number;/);
  });
});

import { describe, expect, it } from "vitest";
import { hasWorkerQuestion, parseWorkerQuestions } from "./workerQuestion";

// 样本与 runtime worker_bus/relay.py::_relay_instructions 的模板逐字段对齐
// （zh 分支）——模板变格式时这里会红，前端折卡片不会静默退化成文字墙。

const SINGLE = `<ginno_worker_question worker="w-session-01" qid="q_abc123">
[waiting] Worker「数据整理」停在一个需要用户决策的问题上，请你代为转达：
问题：装到哪里？
选项：
- Ginno 全局
- 装到仓库

转达方式：
1. 用 ask_user 工具问你的用户。可以改述问题措辞，但每个选项的含义与数量必须与上面一致。
2. 待转达问题 ≤3 个时，建议一次 ask_user(questions=[…]) 一屏问完。
3. 用户回答后，把答案映射回上面的原选项，先写下映射理由，再调用 relay_answer。
4. relay_answer(worker_session="w-session-01", qid="q_abc123", choice="<上面原选项之一的完整文本>", mapping_reason="…")
</ginno_worker_question>`;

const MULTI = `<ginno_worker_question worker="w-2" qid="q_multi01">
[reminder: still unanswered] Worker「调研」等待转达的问题：
问题（多问题卡，逐题作答）：
- 子问题 1：先做哪个？（选项：A 方案 / B 方案）
- 子问题 2：要不要落盘？（选项：要 / 不要）

转达方式：
1. 用 ask_user 工具问你的用户。
4. choice 传数组（按子问题顺序逐题给原选项的完整文本），例如 relay_answer(worker_session="w-2", qid="q_multi01", choice=["<子问题1的原选项>", "<子问题2的原选项>"], mapping_reason="…")
</ginno_worker_question>`;

const EN_HEAD = `<ginno_worker_question worker="w-3" qid="q_en9">
[waiting] Worker "Researcher" is parked on a question that needs a user decision. Relay it to YOUR user:
问题：Which database?
选项：
- SQLite
- Postgres

转达方式：
1. Ask YOUR user with the ask_user tool.
</ginno_worker_question>`;

describe("parseWorkerQuestions", () => {
  it("单问题：worker/qid/title/问题/选项全解析，rest 为空", () => {
    const { rest, items } = parseWorkerQuestions(SINGLE);
    expect(rest.trim()).toBe("");
    expect(items).toHaveLength(1);
    const it0 = items[0];
    expect(it0.worker).toBe("w-session-01");
    expect(it0.qid).toBe("q_abc123");
    expect(it0.title).toBe("数据整理");
    expect(it0.reminder).toBe(false);
    expect(it0.subs).toEqual([
      { q: "装到哪里？", options: ["Ginno 全局", "装到仓库"] },
    ]);
  });

  it("多问题卡：子问题与「/」分隔选项逐条解析 + reminder 标记", () => {
    const { items } = parseWorkerQuestions(MULTI);
    expect(items).toHaveLength(1);
    const it0 = items[0];
    expect(it0.title).toBe("调研");
    expect(it0.reminder).toBe(true);
    expect(it0.subs).toEqual([
      { q: "先做哪个？", options: ["A 方案", "B 方案"] },
      { q: "要不要落盘？", options: ["要", "不要"] },
    ]);
  });

  it("英文 head：title 从双引号提取", () => {
    const { items } = parseWorkerQuestions(EN_HEAD);
    expect(items[0].title).toBe("Researcher");
    expect(items[0].subs[0].options).toEqual(["SQLite", "Postgres"]);
  });

  it("信封外普通文本保留在 rest（前后夹文本）", () => {
    const { rest, items } = parseWorkerQuestions(
      `前面一句。${SINGLE}后面一句。`,
    );
    expect(items).toHaveLength(1);
    expect(rest).toBe("前面一句。后面一句。");
  });

  it("多信封共存（多 worker 同时卡问）", () => {
    const { items } = parseWorkerQuestions(`${SINGLE}\n${MULTI}`);
    expect(items).toHaveLength(2);
    expect(items.map((i) => i.qid)).toEqual(["q_abc123", "q_multi01"]);
  });

  it("无信封：items 空、rest 原样（永不吞文本）", () => {
    const text = "普通消息，没有任何信封。<ginno_worker_question> 未闭合标签也不算";
    const { rest, items } = parseWorkerQuestions(text);
    expect(items).toHaveLength(0);
    expect(rest).toBe(text);
  });

  it("未知变体（问题体缺字段）：信封仍抽出（raw 兜底可展开），subs 为空数组", () => {
    const odd = `<ginno_worker_question worker="w-x" qid="q_x">
[waiting] Worker「怪」停在一个需要用户决策的问题上，请你代为转达：
（格式变了）
</ginno_worker_question>`;
    const { items, rest } = parseWorkerQuestions(odd);
    expect(rest.trim()).toBe("");
    expect(items[0].subs).toEqual([]);
    expect(items[0].raw).toBe(odd);
  });

  it("hasWorkerQuestion 快速探测", () => {
    expect(hasWorkerQuestion(SINGLE)).toBe(true);
    expect(hasWorkerQuestion("plain text")).toBe(false);
    expect(hasWorkerQuestion("plain text")).toBe(false); // 全局 RE 不受 .test 污染
  });
});

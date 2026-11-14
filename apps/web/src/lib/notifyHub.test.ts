import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ReplyMergeWindow, isAppAttentive, isSessionAttentive, shouldNotify, type MergeItem } from "./notifyHub";
import {
  DEFAULT_PREFS,
  fromWire,
  inDndWindow,
  KINDS,
  migrateKinds,
  osGateOpen,
} from "./notifyPrefs";

describe("ReplyMergeWindow", () => {
  let flushed: Array<[string, MergeItem[]]>;
  let clock: { t: number; timers: Array<{ at: number; fn: () => void }> };
  let w: ReplyMergeWindow;

  beforeEach(() => {
    flushed = [];
    clock = { t: 0, timers: [] };
    w = new ReplyMergeWindow({
      windowMs: 30_000,
      now: () => clock.t,
      schedule: (fn, ms) => {
        const entry = { at: clock.t + ms, fn };
        clock.timers.push(entry);
        return () => {
          const i = clock.timers.indexOf(entry);
          if (i >= 0) clock.timers.splice(i, 1);
        };
      },
      onFlush: (key, items) => flushed.push([key, items]),
    });
  });

  const advance = (ms: number) => {
    clock.t += ms;
    // fire due timers (sorted copy — firing may not enqueue more here)
    for (const e of [...clock.timers].sort((a, b) => a.at - b.at)) {
      if (e.at <= clock.t) e.fn();
    }
  };

  it("单条：窗口到点后原样 flush", () => {
    w.push("s1", { title: "A", body: "done" });
    expect(w.pending("s1")).toBe(1);
    advance(31_000);
    expect(flushed).toEqual([["s1", [{ title: "A", body: "done" }]]]);
    expect(w.pending("s1")).toBe(0);
  });

  it("窗口内多条合并 flush（不同 key 互不混）", () => {
    w.push("s1", { title: "A", body: "1" });
    advance(10_000);
    w.push("s1", { title: "A", body: "2" });
    w.push("s2", { title: "B", body: "x" });
    advance(21_000); // s1 窗口(0→30s)到期；s2 窗口(10s→40s)未到
    expect(flushed).toEqual([["s1", [{ title: "A", body: "1" }, { title: "A", body: "2" }]]]);
    expect(w.pending("s2")).toBe(1);
    advance(10_000);
    expect(flushed[1]).toEqual(["s2", [{ title: "B", body: "x" }]]);
  });

  it("flush 后新窗口重新起算", () => {
    w.push("s1", { title: "A", body: "1" });
    advance(30_000);
    w.push("s1", { title: "A", body: "2" });
    expect(w.pending("s1")).toBe(1);
    advance(30_000);
    expect(flushed.map(([k, items]) => [k, items.length])).toEqual([["s1", 1], ["s1", 1]]);
  });

  it("cancel 丢弃不 flush", () => {
    w.push("s1", { title: "A", body: "1" });
    w.cancel("s1");
    advance(60_000);
    expect(flushed).toEqual([]);
  });
});

describe("inDndWindow", () => {
  it("默认关闭恒 false", () => {
    expect(inDndWindow({ enabled: false, from: "22:00", to: "08:00" }, 300)).toBe(false);
  });
  it("跨午夜窗口：23:59 与 07:00 命中，12:00 不命中", () => {
    const dnd = { enabled: true, from: "22:00", to: "08:00" };
    expect(inDndWindow(dnd, 23 * 60 + 59)).toBe(true);
    expect(inDndWindow(dnd, 7 * 60)).toBe(true);
    expect(inDndWindow(dnd, 12 * 60)).toBe(false);
    expect(inDndWindow(dnd, 22 * 60)).toBe(true);
    expect(inDndWindow(dnd, 8 * 60)).toBe(false);
  });
  it("当日窗口：14:00-18:00", () => {
    const dnd = { enabled: true, from: "14:00", to: "18:00" };
    expect(inDndWindow(dnd, 15 * 60)).toBe(true);
    expect(inDndWindow(dnd, 13 * 60 + 59)).toBe(false);
    expect(inDndWindow(dnd, 18 * 60)).toBe(false);
  });
});

describe("fromWire / migrateKinds（kinds 语义 v2）", () => {
  it("老格式（V1 三键）补默认：全部 kinds 默认开", () => {
    const p = fromWire({ enabled: false, sound: true, sound_name: "Glass" });
    expect(p.enabled).toBe(false);
    for (const k of KINDS) expect(p.kinds[k], k).toBe(true);
    expect(p.privacy).toBe("full");
    expect(p.dnd.enabled).toBe(false);
  });

  // subagent 在 V2 里就是「预留」（旧 i18n 原文），至今没有生产者：linked.done
  // 在服务端已过滤掉 subagent/schedule origin（engine.py:1303 只放行
  // interactive/deliver），子代理结果走「注入父对话 + worker.* 应用内 notice」，
  // 从不发 OS 通知。留一个拨了没反应的开关比没有更糟 → 删。
  it("subagent 已从 kinds 移除，持久化里的残留键被忽略", () => {
    expect(KINDS).not.toContain("subagent");
    const p = fromWire({ kinds: { reply: false, subagent: true } });
    expect(p.kinds.reply).toBe(false);
    expect("subagent" in p.kinds).toBe(false);
  });

  it("schedule / bgTask 已补进 kinds（此前只受总开关约束）", () => {
    expect(KINDS).toContain("schedule");
    expect(KINDS).toContain("bgTask");
  });

  // v1 持久化下来的 workflow:false 不是用户的选择，而是「死开关的默认值被
  // saveNotifyPrefs 整对象写盘」的副产物。接真之后照抄会把用户一直在收的
  // 工作流通知静默掉——所以迁移拉回 true。
  it("v1 持久化的 workflow:false 被迁移拉回 true", () => {
    const raw = { kinds: { workflow: false } }; // 无 kinds_schema = v1
    const parsed = fromWire(raw);
    expect(parsed.kinds.workflow).toBe(false);
    const migrated = migrateKinds(raw, parsed);
    expect(migrated.kinds.workflow).toBe(true);
    expect(migrated).not.toBe(parsed); // 新对象 → 调用方据此决定回写
  });

  it("已盖 schema 标记的不再被迁移覆盖（此后是用户的真选择）", () => {
    const raw = { kinds_schema: 2, kinds: { workflow: false } };
    const parsed = fromWire(raw);
    const migrated = migrateKinds(raw, parsed);
    expect(migrated.kinds.workflow).toBe(false);
    expect(migrated).toBe(parsed); // 同一对象 = 无需回写
  });

  it("迁移不动其它 kinds 的用户选择", () => {
    const raw = { kinds: { reply: false, workflow: false, schedule: false } };
    const migrated = migrateKinds(raw, fromWire(raw));
    expect(migrated.kinds.reply).toBe(false);
    expect(migrated.kinds.schedule).toBe(false);
    expect(migrated.kinds.workflow).toBe(true);
  });
});

import { KindRateGate } from "./notifyHub";

describe("KindRateGate（跨会话 reply 冷却）", () => {
  let t = 0;
  beforeEach(() => (t = 0));

  it("窗口内吞并计数，放行时带回 suppressed 并清零", () => {
    const g = new KindRateGate(60_000, () => t);
    expect(g.hit("reply")).toEqual({ ok: true, suppressed: 0 });
    t = 10_000;
    expect(g.hit("reply")).toEqual({ ok: false, suppressed: 1 });
    t = 30_000;
    expect(g.hit("reply")).toEqual({ ok: false, suppressed: 2 });
    t = 61_000;
    expect(g.hit("reply")).toEqual({ ok: true, suppressed: 2 });
    t = 61_001;
    expect(g.hit("reply")).toEqual({ ok: false, suppressed: 1 });
  });

  it("各 kind 独立冷却", () => {
    const g = new KindRateGate(60_000, () => t);
    g.hit("reply");
    t = 1_000;
    expect(g.hit("schedule").ok).toBe(true);
    expect(g.hit("reply").ok).toBe(false);
  });
});

// ── 2026-11-14 回归：fromWire 曾就地改写模块级默认值 ────────────────────────
// `{...DEFAULT_PREFS}` 是浅拷贝，d.dnd 与 DEFAULT_PREFS.dnd 同一引用，随后
// `d.dnd.enabled = …` 会把用户设过的勿扰窗写进模块级默认——此后任何
// settings.json 缺 dnd 键的回退都落到被污染的默认上，勿扰再也关不掉。
describe("fromWire 不得污染 DEFAULT_PREFS", () => {
  it("写入 dnd 不改写模块级默认值", () => {
    const before = { ...DEFAULT_PREFS.dnd };
    fromWire({ dnd: { enabled: true, from: "00:00", to: "23:59" } });
    expect(DEFAULT_PREFS.dnd).toEqual(before);
    expect(DEFAULT_PREFS.dnd.enabled).toBe(false);
  });

  it("连续两次 fromWire 互不影响（第二次仍从干净默认起算）", () => {
    const a = fromWire({ dnd: { enabled: true, from: "01:00", to: "02:00" } });
    const b = fromWire({});
    expect(a.dnd.enabled).toBe(true);
    expect(a.dnd.from).toBe("01:00");
    expect(b.dnd.enabled).toBe(false);
    expect(b.dnd.from).toBe("22:00");
  });

  it("kinds 同样不被污染", () => {
    fromWire({ kinds: { reply: false, schedule: false, bgTask: false } });
    for (const k of KINDS) expect(DEFAULT_PREFS.kinds[k], k).toBe(true);
  });
});

// ── P1-4：注意力判据统一 ────────────────────────────────────────────────────
// 修复前 7 个发通知的站点各写各的：6 处只看 document.visibilityState，只有
// NoticeHost 的失焦兜底额外看了 document.hasFocus()——同一个「用户在看吗」在
// 不同站点有不同答案。现在统一走这两个函数。
describe("注意力判据（isAppAttentive / isSessionAttentive）", () => {
  afterEach(() => vi.unstubAllGlobals());

  const stubDoc = (visibilityState: string) =>
    vi.stubGlobal("document", { visibilityState });
  const stubWin = (pathname: string) =>
    vi.stubGlobal("window", { location: { pathname } });

  it("isAppAttentive：仅 visible 算在看", () => {
    stubDoc("visible");
    expect(isAppAttentive()).toBe(true);
    stubDoc("hidden");
    expect(isAppAttentive()).toBe(false);
  });

  it("isAppAttentive：无 document（node/SSR）判为不在看", () => {
    vi.stubGlobal("document", undefined);
    expect(isAppAttentive()).toBe(false);
  });

  it("isSessionAttentive：三条件缺一不可", () => {
    stubDoc("visible");
    stubWin("/");
    expect(isSessionAttentive("s1", "s1")).toBe(true);
    expect(isSessionAttentive("s1", "s2")).toBe(false); // 盯的是别的会话
    expect(isSessionAttentive(null, "s1")).toBe(false); // 没有当前会话
    stubWin("/settings");
    expect(isSessionAttentive("s1", "s1")).toBe(false); // 不在工作区路由
    stubWin("/");
    stubDoc("hidden");
    expect(isSessionAttentive("s1", "s1")).toBe(false); // 应用不可见
  });

  it("shouldNotify = 不在看 ∧ 偏好闸门（默认偏好全开、勿扰关）", () => {
    expect(shouldNotify("reply", false)).toBe(true);
    expect(shouldNotify("reply", true)).toBe(false);
    expect(shouldNotify("askUser", false)).toBe(true);
  });

  it("osGateOpen：test 类恒放行（设置页测试按钮必须在任何偏好下有反馈）", () => {
    expect(osGateOpen("test")).toBe(true);
  });
});

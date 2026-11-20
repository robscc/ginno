// CodePanel hooks 拆分（批次 3-3）——纯逻辑单测。
//
// vitest 是 node 环境无 DOM（vitest.config 的既定分工：渲染层归 packaged UI
// e2e），所以这里测的是拆分时从组件里提出来的**纯函数**：路径映射、LRU
// 逐出、关 tab 后的焦点回落、agent 标记映射、错误消息形状。hooks 的接线
// （wiring）与渲染由 test_packaged_ui_code_panel.py 覆盖。
//
// 拆分前这些逻辑内联在 CodePanel.tsx 的 useCallback 里不可测；现在钉住，
// 防「重构等价」漂移。

import { describe, expect, it } from "vitest";

import { CodeApiError } from "@/lib/runtime";
import type { CodeRoot } from "@/lib/codeTypes";
import {
  fsError,
  codeCmdErrParts,
  escapeRegExp,
  MAX_TABS,
  TREE_PX,
  WIDE_PX,
} from "./panelShared";
import { splitRootPathIn, absPathOfTab, pickDefaultRoot } from "./useCodeRoots";
import { pickEvictionVictim, nextActiveAfterClose } from "./useCodeTabs";
import { agentTouchedMap } from "./useGitStatus";

const root = (id: string, path: string, access: "rw" | "ro" = "rw"): CodeRoot => ({
  id,
  name: id,
  path,
  access,
  missing: false,
  is_repo: false,
  branch: null,
});

const ROOTS: CodeRoot[] = [
  root("session", "/ws/s1"),
  root("mount", "/Users/x/proj/"),
  root("nested", "/Users/x/proj/inner", "ro"),
];

describe("panelShared：常量与错误形状", () => {
  it("布局常量不漂移（design §3.3：side 阈值 600 / 树宽 260 / tab 上限 20）", () => {
    expect(WIDE_PX).toBe(600);
    expect(TREE_PX).toBe(260);
    expect(MAX_TABS).toBe(20);
  });

  it("fsError 优先服务端中文文案（CodeApiError），Error 次之，兜底 fallback", () => {
    expect(fsError(new CodeApiError("e", "当前文件夹以只读方式挂载"), "fallback")).toBe(
      "当前文件夹以只读方式挂载",
    );
    expect(fsError(new Error("boom"), "fallback")).toBe("boom");
    expect(fsError("裸字符串", "fallback")).toBe("fallback");
    expect(fsError(undefined, "fallback")).toBe("fallback");
  });

  it("codeCmdErrParts 只认 {code, detail} 对象（i18n-design.md §7），其余 null", () => {
    expect(codeCmdErrParts({ code: "outside-root", detail: "/etc/hosts" })).toEqual({
      code: "outside-root",
      detail: "/etc/hosts",
    });
    // detail 缺省/非字符串 → 空串（调用方插值安全）
    expect(codeCmdErrParts({ code: "x" })).toEqual({ code: "x", detail: "" });
    expect(codeCmdErrParts("string error")).toBeNull();
    expect(codeCmdErrParts({ detail: "no code" })).toBeNull();
    expect(codeCmdErrParts(null)).toBeNull();
  });

  it("escapeRegExp 让内容搜索按字面匹配（foo.bar 不再命中 fooXbar）", () => {
    expect(escapeRegExp("foo.bar")).toBe("foo\\.bar");
    expect(escapeRegExp("a+b(c)")).toBe("a\\+b\\(c\\)");
    expect(new RegExp(escapeRegExp("(?i)pa.th$")).test("(?i)paXth$")).toBe(false);
  });
});

describe("useCodeRoots：绝对路径 ↔ 根相对路径（S3）", () => {
  it("最长前缀胜：嵌套根解析到内层，而非外层", () => {
    expect(splitRootPathIn(ROOTS, "/Users/x/proj/inner/a.txt")).toEqual({
      rootId: "nested",
      rel: "a.txt",
    });
  });

  it("尾斜杠的根路径（挂载目录惯例）不影响匹配", () => {
    expect(splitRootPathIn(ROOTS, "/Users/x/proj/a.txt")).toEqual({
      rootId: "mount",
      rel: "a.txt",
    });
  });

  it("根自身命中 rel 为空；根外路径返回 null", () => {
    expect(splitRootPathIn(ROOTS, "/ws/s1")).toEqual({ rootId: "session", rel: "" });
    expect(splitRootPathIn(ROOTS, "/elsewhere/x")).toBeNull();
  });

  it("absPathOfTab：根相对 ↔ 绝对互逆（无 rel = 根本身）", () => {
    expect(absPathOfTab(ROOTS, { rootId: "session", path: "out.md" })).toBe("/ws/s1/out.md");
    expect(absPathOfTab(ROOTS, { rootId: "session", path: "" })).toBe("/ws/s1");
    expect(absPathOfTab(ROOTS, { rootId: "ghost", path: "x" })).toBeNull();
  });

  it("默认根选择：session 工作区优先，其次首个挂载（remote 过滤后回退）", () => {
    expect(pickDefaultRoot(ROOTS)?.id).toBe("session");
    const remoteView = ROOTS.filter((r) => r.id !== "session");
    expect(pickDefaultRoot(remoteView)?.id).toBe("mount");
    expect(pickDefaultRoot([])).toBeUndefined();
  });
});

describe("useCodeTabs：LRU 逐出只吃 CLEAN tab（design §4.7）", () => {
  const meta = (dirty: boolean) => ({ dirty, conflict: null, rev: 0, savedNonce: 0 });

  it("逐出最久未用的 clean tab，跳过 active 与新 tab 自身", () => {
    const open = ["a", "b", "c"];
    const recency = ["a", "b", "c"]; // a 最旧
    expect(pickEvictionVictim(open, recency, "d", "c", { a: meta(false), b: meta(false) })).toBe(
      "a",
    );
    // a 变 active → 逐出退到 b
    expect(pickEvictionVictim(open, recency, "d", "a", { a: meta(false), b: meta(false) })).toBe(
      "b",
    );
  });

  it("dirty tab 永不成为 victim；全 dirty → null（宁超限不丢未保存编辑）", () => {
    const open = ["a", "b"];
    const recency = ["a", "b"];
    expect(
      pickEvictionVictim(open, recency, "c", "c", { a: meta(true), b: meta(false) }),
    ).toBe("b");
    expect(pickEvictionVictim(open, recency, "c", "c", { a: meta(true), b: meta(true) })).toBeNull();
  });

  it("recency 里已不在 open 列表的 key（刚关掉的）不会被选中", () => {
    expect(
      pickEvictionVictim(["a"], ["gone", "a"], "b", null, { a: meta(false) }),
    ).toBe("a");
  });
});

describe("useCodeTabs：关 tab 后的焦点回落", () => {
  const T = (p: string) => ({ rootId: "session", path: p });

  it("关中间 tab → 同索引邻位接管（原 idx 的右侧邻居）", () => {
    const { nextTabs, fallback } = nextActiveAfterClose([T("a"), T("b"), T("c")], codeKey(T("b")));
    expect(nextTabs.map((x) => x.path)).toEqual(["a", "c"]);
    expect(fallback?.path).toBe("c");
  });

  it("关最后一个 tab → 前一个接管；关唯一 tab → null", () => {
    expect(nextActiveAfterClose([T("a"), T("b")], codeKey(T("b"))).fallback?.path).toBe("a");
    expect(nextActiveAfterClose([T("a")], codeKey(T("a"))).fallback).toBeNull();
  });
});

describe("useGitStatus：agent 标记映射（S3）", () => {
  it("绝对路径 change map → 活动根的 root-relative 标记", () => {
    const touched = {
      "/ws/s1/out.md": { op: "write" as const, version: "1", at: 1 },
      "/Users/x/proj/a.ts": { op: "edit" as const, version: "2", at: 2 },
    };
    const split = (abs: string) => splitRootPathIn(ROOTS, abs);
    expect(agentTouchedMap(touched, "session", split)).toEqual({ "out.md": "write" });
    expect(agentTouchedMap(touched, "mount", split)).toEqual({ "a.ts": "edit" });
    expect(agentTouchedMap(touched, null, split)).toBeNull();
  });
});

/** 与 EditorTabs 的 codeTabKey 同构（rootId + \0 + path），测试内自建避免
 *  拉进 .tsx 渲染模块。 */
function codeKey(tab: { rootId: string; path: string }): string {
  return `${tab.rootId}\u0000${tab.path}`;
}

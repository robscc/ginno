// 远端会话模型选择器禁用（remote-session-design.md 延伸：误导性死控件修复）。
//
// 已核实的 bug：远端会话（session.remote_peer）里模型 chip 仍可点击切换，但
// api/stream/remote_turn.py 完全不读 model/provider——模型由远端节点自己的
// 配置决定，本地切换是看起来能调、实际无效的死控件。修复 = 双层：
//   ① ModelChipMenu：整 chip disabled + tooltip 告知归属 + label 换 peer 名
//   ② useModelPick：pickModel / pickModelAndThinking fail-closed 守卫
// node 环境无 DOM，照 AgentChips.test.tsx 范式读源码字符串钉住契约；渲染
// 真实性由 packaged UI e2e（test_packaged_ui_llm_events.py）覆盖。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enChat from "../../../../messages/en/chat.json";
import zhChat from "../../../../messages/zh-CN/chat.json";

const CHIP = readFileSync(
  fileURLToPath(new URL("./ModelChipMenu.tsx", import.meta.url)),
  "utf8",
);
const PICK = readFileSync(
  fileURLToPath(new URL("../useModelPick.ts", import.meta.url)),
  "utf8",
);

describe("ModelChipMenu：远端会话整 chip 禁用", () => {
  it("禁用判据只来自 session.remote_peer（本地会话零影响）", () => {
    expect(CHIP).toMatch(/const remotePeer = session\?\.remote_peer \|\| null/);
    expect(CHIP).toContain("disabled={!!remotePeer}");
  });

  it("tooltip 明确告知「模型由远端节点决定」（model-remote-title，带 peer 名）", () => {
    expect(CHIP).toMatch(/remotePeer\s*\n?\s*\? tc\("model-remote-title", \{ name: remoteName \}\)/);
  });

  it("菜单渲染加 !remotePeer 守卫（即使 open 被置真也不展开）", () => {
    expect(CHIP).toContain("{open && !remotePeer && (");
  });

  it("label 换 peer 名——session 载荷真源优先，三级回退（i18n {name} 错位修复）", () => {
    expect(CHIP).toContain('import { useA2aPeers } from "@/lib/useA2aPeers"');
    expect(CHIP).toMatch(/const peers = useA2aPeers\(!!remotePeer\)/);
    // 三级回退：session.remote_peer_name（后端真源，无竞态）→ peers 查找
    // （旧 sidecar 兼容）→ slug。此前只有后两级，useA2aPeers 落定前
    // {name} 占位符闪 slug（e2e flaky 根因，2026-11-18）。
    expect(CHIP).toMatch(
      /session\?\.remote_peer_name \|\|\s*\n\s*peers\.find\(\(p\) => p\.slug === remotePeer\)\?\.name \|\|\s*\n\s*remotePeer/,
    );
    expect(CHIP).toContain("{remotePeer ? remoteName : label}");
  });

  it("远端态换机器图标、隐藏思考角标与下拉箭头（都是本地语义的控件）", () => {
    expect(CHIP).toMatch(/\{remotePeer \? \(\s*\n?\s*<Server/);
    expect(CHIP).toContain("{!remotePeer && EFFORT_BADGE[thinkingEffort] && (");
    expect(CHIP).toContain("{!remotePeer && <ChevronDown");
  });

  it("e2e 定位点：data-testid=model-chip-remote（仅远端态）", () => {
    expect(CHIP).toContain('data-testid={remotePeer ? "model-chip-remote" : undefined}');
  });

  it("useA2aPeers 在组件顶层无条件调用（React hooks 顺序不可条件化）", () => {
    const hookIdx = CHIP.indexOf("useA2aPeers(!!remotePeer)");
    const returnIdx = CHIP.indexOf("return (");
    expect(hookIdx).toBeGreaterThan(-1);
    expect(hookIdx).toBeLessThan(returnIdx);
  });
});

describe("useModelPick：fail-closed 守卫（纵深防御，chip 之外再守一道）", () => {
  it("pickModel 在发 PATCH 前被 remote_peer 守卫拦下", () => {
    const fnStart = PICK.indexOf("async function pickModel(");
    const guardIdx = PICK.indexOf("if (session?.remote_peer) return;", fnStart);
    const patchIdx = PICK.indexOf("api.patchSession", fnStart);
    expect(fnStart).toBeGreaterThan(-1);
    expect(guardIdx).toBeGreaterThan(fnStart);
    expect(guardIdx).toBeLessThan(patchIdx);
  });

  it("pickModelAndThinking 同样被守卫拦下（思考档位也由远端决定）", () => {
    const fnStart = PICK.indexOf("async function pickModelAndThinking(");
    const guardIdx = PICK.indexOf("if (session?.remote_peer) return;", fnStart);
    const patchIdx = PICK.indexOf("api.patchSession", fnStart);
    expect(fnStart).toBeGreaterThan(-1);
    expect(guardIdx).toBeGreaterThan(fnStart);
    expect(guardIdx).toBeLessThan(patchIdx);
  });

  it("守卫是条件式（session?.remote_peer），本地会话路径不被误伤", () => {
    // 两处守卫都必须是条件 return，不是无条件 return
    const guards = PICK.match(/if \(session\?\.remote_peer\) return;/g) ?? [];
    expect(guards.length).toBe(2);
  });
});

describe("i18n：model-remote-title 双语齐全且扁平连字符（无点）", () => {
  it("en 与 zh-CN 都存在、非空、已翻译、带 {name} 占位", () => {
    const k = "model-remote-title";
    expect(k).not.toContain(".");
    const en = (enChat.chat as Record<string, unknown>)[k];
    const zh = (zhChat.chat as Record<string, unknown>)[k];
    expect(typeof en).toBe("string");
    expect(typeof zh).toBe("string");
    expect(en).not.toBe(zh);
    expect(en as string).toContain("{name}");
    expect(zh as string).toContain("{name}");
  });

  it("zh-CN 文案明确「模型由远端节点…决定」语义", () => {
    const zh = (zhChat.chat as Record<string, unknown>)["model-remote-title"] as string;
    expect(zh).toContain("模型由远端节点");
    expect(zh).toContain("决定");
  });
});

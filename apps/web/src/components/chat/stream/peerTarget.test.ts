/**
 * peerTarget.ts 纯函数单测（remote-session-design.md R3）。
 *
 * target 编码契约：`peer:<slug>` 前缀串区分远端 peer 与本地 agent id，
 * 本地 agent 选择行为零改动——这里钉住「本地 id 绝不被误判成 peer」与
 * 「peer 串绝不被当本地 id 放行」两个方向。
 */
import { describe, expect, it } from "vitest";
import {
  PEER_TARGET_PREFIX,
  parsePeerTarget,
  peerEndpointHint,
  peerTargetValue,
} from "./peerTarget";

describe("peerTargetValue / parsePeerTarget", () => {
  it("roundtrip：slug → target 串 → slug", () => {
    expect(peerTargetValue("expense-bot")).toBe("peer:expense-bot");
    expect(parsePeerTarget(peerTargetValue("expense-bot"))).toBe("expense-bot");
  });

  it("前缀常量钉死（发送/预览路径都按它分流）", () => {
    expect(PEER_TARGET_PREFIX).toBe("peer:");
  });

  it("本地 agent id 一律返回 null（不破坏既有选择行为）", () => {
    for (const id of ["dev", "main", "dev-agent", "a2a-expense-bot", "peerless"]) {
      expect(parsePeerTarget(id)).toBeNull();
    }
  });

  it("null / undefined / 空串 / 裸前缀 → null", () => {
    expect(parsePeerTarget(null)).toBeNull();
    expect(parsePeerTarget(undefined)).toBeNull();
    expect(parsePeerTarget("")).toBeNull();
    expect(parsePeerTarget("peer:")).toBeNull();
  });

  it("前缀必须在串首（中间出现 peer: 不算）", () => {
    expect(parsePeerTarget("xpeer:bot")).toBeNull();
    expect(parsePeerTarget(" peer:bot")).toBeNull();
  });

  it("slug 含连字符/数字正常解析", () => {
    expect(parsePeerTarget("peer:node-2")).toBe("node-2");
  });
});

describe("peerEndpointHint", () => {
  it("剥协议头与 #fp 指纹片段，只留 host:port/path", () => {
    expect(peerEndpointHint("https://192.168.1.9:8790/a2a#fp=ABC")).toBe(
      "192.168.1.9:8790/a2a",
    );
    expect(peerEndpointHint("http://mini.local:8790/a2a")).toBe("mini.local:8790/a2a");
  });

  it("无协议头/无片段原样返回；空串安全", () => {
    expect(peerEndpointHint("host:8790/a2a")).toBe("host:8790/a2a");
    expect(peerEndpointHint("")).toBe("");
  });
});
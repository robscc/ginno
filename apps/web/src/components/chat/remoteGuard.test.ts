// 远端会话守卫纯函数（remoteGuard.ts）——审计 C2 的判定单一源。
// vitest node 环境：真 import 断言行为（范式同 waitState.ts）。

import { describe, expect, it } from "vitest";

import { isRemoteSession, remotePeerDisplayName } from "./remoteGuard";

describe("isRemoteSession", () => {
  it("remote_peer 非空字符串 → true", () => {
    expect(isRemoteSession({ remote_peer: "ginno-node-vps" })).toBe(true);
  });

  it("null / undefined / 缺字段 → false（本地会话不受影响）", () => {
    expect(isRemoteSession(null)).toBe(false);
    expect(isRemoteSession(undefined)).toBe(false);
    expect(isRemoteSession({})).toBe(false);
  });

  it("空串 / null 载荷 → false（truthy 检查兼容旧 sidecar 不下发）", () => {
    expect(isRemoteSession({ remote_peer: "" })).toBe(false);
    expect(isRemoteSession({ remote_peer: null })).toBe(false);
  });

  it("type=channel 无 remote_peer → false：渠道只读 ≠ 远端执行，不互推", () => {
    expect(isRemoteSession({ type: "channel" })).toBe(false);
  });

  it("type=channel 且带 remote_peer → true：判定只看 remote_peer 字段", () => {
    expect(isRemoteSession({ type: "channel", remote_peer: "vps" })).toBe(true);
  });
});

describe("remotePeerDisplayName", () => {
  it("载荷真源 remote_peer_name 优先", () => {
    expect(
      remotePeerDisplayName({ remote_peer: "ginno-node-vps", remote_peer_name: "VPS 节点" }),
    ).toBe("VPS 节点");
  });

  it("无显示名回退 slug；非远端会话返回空串", () => {
    expect(remotePeerDisplayName({ remote_peer: "ginno-node-vps" })).toBe("ginno-node-vps");
    expect(remotePeerDisplayName({ remote_peer: null })).toBe("");
    expect(remotePeerDisplayName(null)).toBe("");
  });
});

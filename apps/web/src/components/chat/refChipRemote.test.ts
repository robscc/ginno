/* 远端会话 attach_ref chip 降级（2026-11-18 修复）的源码断言测试——
 * node 无 DOM，照 RemotePeerBadge.test.tsx 范式；渲染真实性见 PR body
 * 对 packaged UI e2e 取舍的说明。
 *
 * 背景：远端 turn 转发回来的 file/doc ref 的 refId 是对端机器上的路径，
 * 本机 ensure-registered 必然 "File not found"，旧版 chip 点击静默返回
 * （用户反馈「点了没反应」）。修复契约：
 *  1) 远端判定双保险——转发帧的 remote 标记（live）+ 会话自身 remote_peer
 *     （历史投影的 ref 块无标记，靠会话归属兜底：远端会话本地工具不注入，
 *     file/doc 引用必来自对端）；
 *  2) 远端 chip 点击 → pushNotice 远端路径提示（绝不调 ensureFileRegistered）；
 *  3) link 型远端 ref 仍交系统浏览器（URL 与机器无关）；
 *  4) streamCore 把 ref.emit 的 remote/peer 透传进 ref block；
 *  5) Artifacts 面板 remote 条目（origin="remote"）点击同样降级提示。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import enChat from "../../../messages/en/chat.json";
import zhChat from "../../../messages/zh-CN/chat.json";
import enRight from "../../../messages/en/right.json";
import zhRight from "../../../messages/zh-CN/right.json";

const BLOCKS = readFileSync(
  fileURLToPath(new URL("./blocks.tsx", import.meta.url)),
  "utf8",
);
const STREAM_CORE = readFileSync(
  fileURLToPath(new URL("./streamCore.ts", import.meta.url)),
  "utf8",
);
const ARTIFACTS_PANEL = readFileSync(
  fileURLToPath(new URL("../right/ArtifactsPanel.tsx", import.meta.url)),
  "utf8",
);

describe("RefChip 远端降级", () => {
  it("远端判定双保险：帧级 remote 标记 + 会话 remote_peer 兜底", () => {
    expect(BLOCKS).toMatch(
      /const isRemote = remote \|\| !!sess\?\.remote_peer;/,
    );
    expect(BLOCKS).toMatch(
      /const peerName = peer \|\| sess\?\.remote_peer_name \|\| sess\?\.remote_peer \|\| "";/,
    );
  });

  it("远端 chip 点击 → pushNotice 提示，绝不走 ensureFileRegistered", () => {
    // isRemote 分支先 return，ensureFileRegistered 只服务本会话路径
    const onOpen = BLOCKS.slice(
      BLOCKS.indexOf("const onOpen = async () => {"),
      BLOCKS.indexOf("return (", BLOCKS.indexOf("const onOpen = async () => {")),
    );
    const ir = onOpen.indexOf("if (isRemote) {");
    const push = onOpen.indexOf('pushNotice("info"');
    const ensure = onOpen.indexOf("await ensureFileRegistered");
    expect(ir).toBeGreaterThan(-1);
    expect(push).toBeGreaterThan(ir);
    expect(onOpen.indexOf("return;", push)).toBeLessThan(ensure);
    expect(onOpen).toContain('tc("refChip.remoteNotice", { name, path: refId, peer: peerName })');
    expect(onOpen).toMatch(/dedupeKey: `remote-ref:\$\{refId\}`/);
  });

  it("link 型远端 ref 仍交系统浏览器（URL 与机器无关）", () => {
    const onOpen = BLOCKS.slice(
      BLOCKS.indexOf("const onOpen = async () => {"),
      BLOCKS.indexOf("return (", BLOCKS.indexOf("const onOpen = async () => {")),
    );
    const link = onOpen.indexOf('if (refKind === "link") {');
    expect(link).toBeGreaterThan(-1);
    expect(link).toBeLessThan(onOpen.indexOf("if (isRemote) {"));
  });

  it("远端 chip 视觉标识：Server 图标 + 远端徽标 + data-testid", () => {
    expect(BLOCKS).toContain('data-testid={isRemote ? "remote-ref-chip" : undefined}');
    expect(BLOCKS).toMatch(/\{isRemote && refKind !== "link" && \(/);
    expect(BLOCKS).toContain('<Server className="h-3 w-3" />');
    expect(BLOCKS).toContain('tc("refChip.remoteBadge")');
  });

  it("RefsBlock 调用点透传 remote/peer", () => {
    expect(BLOCKS).toMatch(/remote=\{r\.remote\}/);
    expect(BLOCKS).toMatch(/peer=\{r\.peer\}/);
  });

  it("streamCore：ref.emit 帧的 remote/peer 透传进 ref block", () => {
    expect(STREAM_CORE).toContain("remote: (ev.remote as boolean) || undefined,");
    expect(STREAM_CORE).toContain("peer: (ev.peer as string) || undefined,");
  });
});

describe("ArtifactsPanel 远端条目降级", () => {
  it("registry 查不到 + origin=remote → pushNotice 远端提示（不静默）", () => {
    expect(ARTIFACTS_PANEL).toContain('if (a.origin === "remote") {');
    expect(ARTIFACTS_PANEL).toContain(
      'pushNotice("info", t("remoteNotice", { name: a.name, path: a.ref })',
    );
  });

  it("行内远端徽标", () => {
    expect(ARTIFACTS_PANEL).toMatch(/\{a\.origin === "remote" && \(/);
    expect(ARTIFACTS_PANEL).toContain('{t("remoteBadge")}');
  });
});

describe("i18n：新键双语齐全", () => {
  it("chat.refChip.remote* 三键在 en/zh-CN 齐且带占位", () => {
    for (const msgs of [enChat, zhChat]) {
      const rc = (msgs.chat as Record<string, unknown>).refChip as Record<
        string,
        string
      >;
      expect(rc.remoteBadge?.length).toBeGreaterThan(0);
      expect(rc.remoteNotice).toContain("{name}");
      expect(rc.remoteNotice).toContain("{path}");
      expect(rc.remoteNotice).toContain("{peer}");
      expect(rc.remoteTitle).toContain("{path}");
    }
  });

  it("right.artifacts.remote* 两键在 en/zh-CN 齐且带占位", () => {
    for (const msgs of [enRight, zhRight]) {
      const arts = ((msgs.right as Record<string, unknown>).artifacts ?? {}) as Record<
        string,
        string
      >;
      expect(arts.remoteBadge?.length).toBeGreaterThan(0);
      expect(arts.remoteNotice).toContain("{name}");
      expect(arts.remoteNotice).toContain("{path}");
    }
  });
});

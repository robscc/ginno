// streamCore 的失败恢复横幅「过时即隐藏」判定（failedTurnBannerVisible）。
//
// 2026-11-18 修复：turn 失败后用户已开新一轮对话（发了新消息并得到回复），
// 旧失败横幅（⚠ 请求失败 + 从头重试/从 checkpoint 续跑）仍悬在新对话上方
// ——过时的恢复入口点了会重放旧 turn。判据（goal 设计）：横幅只在失败
// turn 仍是**会话最后一个 turn**（其后没有更新的用户/助手消息）时渲染。
//
// 「守卫范围/布尔条件画错」这类错只有把真实形状喂进纯函数才能钉住——
// 下列用例按 ChatMsg 真实字段形状构造（最小 Pick<ChatMsg,"role"> 投影），
// 覆盖 goal 列的三条边界 + 回归（末位失败横幅仍在）。

import { describe, expect, it } from "vitest";
import { failedTurnBannerVisible } from "@/components/chat/streamCore";
import type { ChatMsg } from "@/components/chat/streamCore";

/** 最小消息形状：判定只读 role，其余字段按真实类型给足可辨识形状。 */
const msg = (role: ChatMsg["role"], over: Partial<ChatMsg> = {}): ChatMsg =>
  ({ id: "m", role, blocks: [], ...over }) as ChatMsg;

const user = (over: Partial<ChatMsg> = {}) => msg("user", over);
const assistant = (over: Partial<ChatMsg> = {}) => msg("assistant", over);
const system = () => msg("system");
/** 失败 turn 的 error 卡消息（role=assistant + error，实时流与历史重放同形） */
const errorCard = (over: Partial<ChatMsg> = {}) =>
  assistant({ error: true, sendPayload: { text: "x", images: [], files: [], mentions: [], agentId: null }, ...over });

describe("failedTurnBannerVisible：末位失败 → 横幅在（回归）", () => {
  it("error 是最后一条消息", () => {
    const ms = [user(), errorCard()];
    expect(failedTurnBannerVisible(ms, 1)).toBe(true);
  });

  it("其后只跟 system 行（context chips 等系统块不算 turn 内容）", () => {
    const ms = [user(), errorCard(), system()];
    expect(failedTurnBannerVisible(ms, 1)).toBe(true);
  });

  it("index 是最后一位 / 越界防御", () => {
    const ms = [user(), errorCard()];
    expect(failedTurnBannerVisible(ms, ms.length - 1)).toBe(true);
    expect(failedTurnBannerVisible(ms, ms.length)).toBe(true);
  });
});

describe("failedTurnBannerVisible：新一轮对话开始 → 旧横幅消失", () => {
  it("边界①镜像：其后出现新 user 消息（即使投递失败 status=failed）", () => {
    // 用户已表达新意图——恢复重心应转到那条消息自己的重发入口（红 ❗）
    const ms = [user(), errorCard(), user({ status: "failed" })];
    expect(failedTurnBannerVisible(ms, 1)).toBe(false);
  });

  it("用户截图场景：新 user 消息 + 成功的 assistant 回复", () => {
    const ms = [user(), errorCard(), user(), assistant({ blocks: [] })];
    expect(failedTurnBannerVisible(ms, 1)).toBe(false);
  });

  it("新 turn 只有一半（user 在、assistant 未到）也已过时", () => {
    const ms = [user(), errorCard(), user({ status: "sending" })];
    expect(failedTurnBannerVisible(ms, 1)).toBe(false);
  });
});

describe("failedTurnBannerVisible：边界②新 turn 又失败 → 新横幅出现、旧的消失", () => {
  it("两个失败 turn 并存时，只末位 error 可见", () => {
    const ms = [user(), errorCard(), user(), errorCard()];
    expect(failedTurnBannerVisible(ms, 1)).toBe(false); // 旧横幅消失
    expect(failedTurnBannerVisible(ms, 3)).toBe(true); // 新横幅出现
  });

  it("历史重放形状：多个失败 turn 埋在中段全部隐藏", () => {
    const ms = [user(), errorCard(), user(), errorCard(), user(), assistant()];
    expect(failedTurnBannerVisible(ms, 1)).toBe(false);
    expect(failedTurnBannerVisible(ms, 3)).toBe(false);
  });

  it("流中断气泡（failed assistant，非 error 卡）后跟新对话也不 resurrect", () => {
    // failed 气泡走 AssistantBubble 分支不受此判定影响，但若它后面的
    // error 卡仍在末位（同 turn 两条消息），横幅保留
    const ms = [user(), assistant({ failed: true }), errorCard()];
    expect(failedTurnBannerVisible(ms, 2)).toBe(true);
  });
});

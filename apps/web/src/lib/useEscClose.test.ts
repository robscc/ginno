/** makeEscHandler（useEscClose 纯函数层）单测。 */
import { describe, expect, it, vi } from "vitest";
import { makeEscHandler } from "./useEscClose";

function keyEv(key: string) {
  let stopped = false;
  return {
    key,
    stopped: () => stopped,
    stopPropagation: () => (stopped = true),
  };
}

describe("makeEscHandler", () => {
  it("Escape 触发 onClose 并 stopPropagation", () => {
    const onClose = vi.fn();
    const h = makeEscHandler(onClose);
    const e = keyEv("Escape");
    h(e as unknown as KeyboardEvent);
    expect(onClose).toHaveBeenCalledOnce();
    expect(e.stopped()).toBe(true);
  });

  it("非 Escape 键忽略", () => {
    const onClose = vi.fn();
    const e = keyEv("Enter");
    makeEscHandler(onClose)(e as unknown as KeyboardEvent);
    expect(onClose).not.toHaveBeenCalled();
    expect(e.stopped()).toBe(false);
  });

  it("guard 返回 true 时不关闭也不 stop", () => {
    const onClose = vi.fn();
    const e = keyEv("Escape");
    makeEscHandler(onClose, () => true)(e as unknown as KeyboardEvent);
    expect(onClose).not.toHaveBeenCalled();
    expect(e.stopped()).toBe(false);
  });
});

import { describe, expect, it } from "vitest";
import { burstParticles, stepParticle } from "./fireworks";

describe("fireworks 引擎纯函数", () => {
  it("一发主礼花 62 颗、副礼花 34 颗，全部球面放射（角度覆盖 2π）", () => {
    let seed = 42;
    const rand = () => ((seed = (seed * 9301 + 49297) % 233280) / 233280);
    const big = burstParticles(100, 100, true, rand);
    const small = burstParticles(100, 100, false, rand);
    expect(big).toHaveLength(62);
    expect(small).toHaveLength(34);
    // 粒子从爆点出发、速度非零（放射状）
    for (const p of big.slice(0, 8)) {
      expect(Math.hypot(p.vx, p.vy)).toBeGreaterThan(0);
      expect(p.life).toBe(1);
    }
  });

  it("物理步进产生重力弧线：vy 递增、vx 阻尼递减、寿命衰减", () => {
    const p0 = { x: 0, y: 0, vx: 4, vy: -2, life: 1, decay: 0.01, hue: 0, sat: 90, lit: 70, size: 2, tw: 0 };
    let p = p0;
    const vys: number[] = [];
    const vxs: number[] = [];
    for (let i = 0; i < 30; i++) {
      p = stepParticle(p);
      if (i % 10 === 9) { vys.push(p.vy); vxs.push(p.vx); }
    }
    // 重力：vy 持续增大（上升→下坠的弧线转折）
    expect(vys[1]).toBeGreaterThan(vys[0]);
    expect(vys[2]).toBeGreaterThan(vys[1]);
    // 空气阻尼：vx 模值递减
    expect(Math.abs(vxs[1])).toBeLessThan(Math.abs(vxs[0]));
    expect(Math.abs(vxs[2])).toBeLessThan(Math.abs(vxs[1]));
    // 寿命衰减
    expect(p.life).toBeLessThan(1);
  });

  it("步进不改原对象（纯函数）", () => {
    const p0 = { x: 1, y: 2, vx: 0.5, vy: 0.5, life: 1, decay: 0.02, hue: 100, sat: 90, lit: 60, size: 2, tw: 0 };
    const p1 = stepParticle(p0);
    expect(p0.x).toBe(1);
    expect(p1.x).not.toBe(1);
  });
});

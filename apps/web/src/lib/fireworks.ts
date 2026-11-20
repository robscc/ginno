/**
 * TODO 完成大礼花引擎（2026-11-21，候选⑦六发版）。
 *
 * 勾选（undone→done）瞬间在任务卡片上方放六发礼花：两发主礼花居中、
 * 四发副礼花左右扇形展开（0–540ms 六级错峰），每发 62/34 颗彩虹粒子
 * 球面放射，真物理（重力 + 空气阻尼 → 弧线垂落）、拖尾光线（lighter
 * 混合在暗色主题下发光）、亮度闪烁、寿命衰减淡出。
 *
 * canvas 挂 body（fixed 全屏、pointer-events:none）——礼花在卡片上方的
 * 整个面板空间绽放，不受卡片 overflow 裁剪；单例 rAF 循环多卡共享，
 * 粒子全灭后自动停表清屏。纯新增视觉反馈，不写任何业务状态。
 */

export interface FwParticle {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  decay: number;
  hue: number;
  sat: number;
  lit: number;
  size: number;
  tw: number;
  tailOnly?: boolean;
}

/** 一发礼花的球面放射粒子（纯函数，可测）。 */
export function burstParticles(
  x: number,
  y: number,
  big: boolean,
  rand: () => number = Math.random
): FwParticle[] {
  const hues = [0, 25, 45, 160, 210, 280, 330];
  const n = big ? 62 : 34;
  const speed = big ? 5.6 : 4.2;
  const baseHue = hues[Math.floor(rand() * hues.length)];
  const out: FwParticle[] = [];
  for (let i = 0; i < n; i++) {
    const ang = (Math.PI * 2 * i) / n + rand() * 0.25;
    const v = speed * (0.55 + rand() * 0.45);
    out.push({
      x,
      y,
      vx: Math.cos(ang) * v,
      vy: Math.sin(ang) * v * 0.92,
      life: 1,
      decay: 0.0072 + rand() * 0.0072,
      hue: baseHue + (rand() - 0.5) * 55,
      sat: 95,
      lit: 62 + rand() * 12,
      size: 1.6 + rand() * 1.7,
      tw: rand() * Math.PI * 2,
    });
  }
  return out;
}

/** 粒子一步物理（重力 + 阻尼 + 衰减；纯函数，可测弧线）。 */
export function stepParticle(p: FwParticle): FwParticle {
  return {
    ...p,
    x: p.x + p.vx,
    y: p.y + p.vy,
    vy: p.vy + 0.085,
    vx: p.vx * 0.985,
    life: p.life - p.decay,
    tw: p.tw + 0.35,
  };
}

/* ── DOM/canvas 引擎（懒初始化，SSR 安全） ── */

let canvas: HTMLCanvasElement | null = null;
let ctx: CanvasRenderingContext2D | null = null;
let parts: FwParticle[] = [];
let raf = 0;

function ensureCanvas(): boolean {
  if (typeof document === "undefined") return false;
  if (canvas && ctx) return true;
  canvas = document.createElement("canvas");
  canvas.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:9999";
  document.body.appendChild(canvas);
  ctx = canvas.getContext("2d");
  if (!ctx) return false;
  const fit = () => {
    if (!canvas || !ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = window.innerWidth * dpr;
    canvas.height = window.innerHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  };
  fit();
  window.addEventListener("resize", fit);
  return true;
}

function loop(): void {
  if (!ctx || !canvas) return;
  ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  ctx.globalCompositeOperation = "lighter";
  parts = parts.filter((p) => p.life > 0);
  for (const p of parts) {
    const q = stepParticle(p);
    p.x = q.x; p.y = q.y; p.vx = q.vx; p.vy = q.vy; p.life = q.life; p.tw = q.tw;
    const flicker = p.tailOnly ? 1 : 0.72 + 0.28 * Math.sin(p.tw);
    const a = Math.max(p.life, 0) * flicker;
    const tl = 3.2;
    ctx.strokeStyle = `hsla(${p.hue},${p.sat}%,${p.lit}%,${a * 0.5})`;
    ctx.lineWidth = p.size * 0.62;
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(p.x - p.vx * tl, p.y - p.vy * tl);
    ctx.stroke();
    ctx.fillStyle = `hsla(${p.hue},${p.sat}%,${Math.min(p.lit + 18, 92)}%,${a})`;
    ctx.beginPath();
    ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalCompositeOperation = "source-over";
  exposeTestHook();
  if (parts.length) {
    raf = requestAnimationFrame(loop);
  } else {
    cancelAnimationFrame(raf);
    raf = 0;
    ctx.clearRect(0, 0, window.innerWidth, window.innerHeight);
  }
}

function burstAt(x: number, y: number, big: boolean): void {
  parts.push(...burstParticles(x, y, big));
}

/** 升空弹：从 (x,y) 带尾迹上升到上方 70–120px 处炸开。 */
function rocket(fromX: number, fromY: number, delayMs: number, big: boolean): void {
  window.setTimeout(() => {
    const drift = (Math.random() - 0.5) * 0.8;
    const rise = 70 + Math.random() * 50;
    const t0 = performance.now();
    const tick = () => {
      const t = (performance.now() - t0) / 420;
      const nx = fromX + drift * t * 60;
      const ny = fromY - rise * Math.min(t, 1);
      parts.push({
        x: nx, y: ny, vx: 0, vy: 0.2, life: 0.55, decay: 0.05,
        hue: 45, sat: 90, lit: 70, size: 1.2, tw: 0, tailOnly: true,
      });
      if (t < 1) requestAnimationFrame(tick);
      else burstAt(nx, ny, big);
    };
    requestAnimationFrame(tick);
  }, delayMs);
}

/** 在元素上方放六发礼花（两主四副扇形错落）。 */
export function fireCelebrationFrom(el: HTMLElement | null): void {
  if (!el || !ensureCanvas()) return;
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2;
  rocket(cx - 18, r.top, 0, true);
  rocket(cx + 22, r.top - 4, 220, true);
  rocket(cx - 120, r.top + 14, 130, false);
  rocket(cx + 118, r.top + 10, 330, false);
  rocket(cx - 210, r.top + 26, 430, false);
  rocket(cx + 205, r.top + 22, 540, false);
  if (!raf) raf = requestAnimationFrame(loop);
}

/** 测试钩子：当前粒子数（不触发渲染）。 */
export function __particleCount(): number {
  return parts.length;
}

declare global {
  interface Window {
    /** e2e 只读观测钩子（渲染循环节拍同步）。 */
    __fwParts?: number;
  }
}

function exposeTestHook(): void {
  if (typeof window !== "undefined") {
    window.__fwParts = parts.length;
  }
}

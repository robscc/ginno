#!/usr/bin/env python3
"""预烘托盘彩虹描边动画帧（方案 2「描边流光」，2026-11-21）。

视觉契约：鸭身保持原色；轮廓带（alpha 梯度边缘）上色为 conic 彩虹——
每个轮廓像素的色相由它相对图像中心的极角决定，相位随帧号推进 →
光带绕轮廓一圈 = 一个循环。空闲态用回原始 icon（第 0 相位 ≠ 原图，
所以静态恢复必须用原 icon 而非 frame_00，见 tray_anim::restore）。

用法（需 Pillow+numpy，可用 runtime venv）：
    packages/runtime/.venv/bin/python apps/desktop/scripts/gen_tray_rainbow.py

输出：apps/desktop/icons/tray_anim/frame_{00..11}.png（64×64 RGBA，
对应菜单栏 22pt@3x；12 帧 × 170ms ≈ 2.04s/圈，与方案参数一致）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "apps/desktop/icons/icon.png"
OUT_DIR = REPO / "apps/desktop/icons/tray_anim"
FRAMES = 12
OUT_SIZE = 64
EDGE_GAIN = 8.0  # 轮廓带强度增益（alpha 梯度 × 增益，clip 到 1）
SAT, LIG = 0.92, 0.60  # 彩虹色饱和度/亮度


def main() -> int:
    src = Image.open(SRC).convert("RGBA")
    w = src.width
    arr = np.asarray(src).astype(float) / 255.0
    alpha = arr[..., 3]

    # 轮廓带：alpha 高斯模糊后的梯度幅值（双向 ~3px 的边缘带）
    blurred = (
        np.asarray(
            Image.fromarray((alpha * 255).astype("uint8")).filter(
                ImageFilter.GaussianBlur(3)
            )
        ).astype(float)
        / 255.0
    )
    gy, gx = np.gradient(blurred)
    edge = np.clip(np.sqrt(gx**2 + gy**2) * EDGE_GAIN, 0, 1)

    # 每像素极角（以图像中心为极点）→ conic 彩虹的相位基础
    yy, xx = np.mgrid[0:w, 0:w]
    theta = (np.degrees(np.arctan2(yy - w / 2, xx - w / 2)) + 360) % 360

    def conic_frame(deg_shift: float) -> Image.Image:
        hue = ((theta + deg_shift) % 360) / 360.0
        h6 = hue * 6
        sector = h6.astype(int) % 6
        frac_abs = np.abs((h6 % 2) - 1)
        c = (1 - abs(2 * LIG - 1)) * SAT
        x = c * (1 - frac_abs)
        zero = np.zeros_like(c)
        rgb = np.stack(
            [
                np.choose(sector, [c, x, zero, zero, x, c]),
                np.choose(sector, [x, c, c, x, zero, zero]),
                np.choose(sector, [zero, zero, x, c, c, x]),
            ]
        ) + (LIG - c / 2)
        out = arr.copy()
        e3 = edge[..., None]
        out[..., :3] = arr[..., :3] * (1 - e3) + rgb.transpose(1, 2, 0) * e3
        return Image.fromarray((np.clip(out, 0, 1) * 255).astype("uint8"), "RGBA")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(FRAMES):
        shift = 360.0 * i / FRAMES
        frame = conic_frame(shift).resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)
        out = OUT_DIR / f"frame_{i:02d}.png"
        frame.save(out, "PNG", optimize=True)
        print(f"{out.name} phase={shift:.0f}°")
    print(f"done: {FRAMES} frames -> {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

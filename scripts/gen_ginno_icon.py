"""Generate 3 candidate star icons for Ginno (previews only, no repo write)."""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 1024
SS = 4
C = S * SS
INDIGO = (121, 112, 235)
INDIGO_HI = (141, 133, 246)
INDIGO_LO = (99, 86, 228)
RADIUS = round(0.2237 * C)  # macOS squircle-ish corner


def star_points(cx, cy, R, r):
    pts = []
    for i in range(10):
        rad = R if i % 2 == 0 else r
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def rounded_bg(gradient):
    mask = Image.new("L", (C, C), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, C - 1, C - 1], radius=RADIUS, fill=255)
    if gradient:
        col = Image.new("RGB", (1, C))
        for y in range(C):
            t = y / C
            col.putpixel((0, y), tuple(round(hi + (lo - hi) * t) for hi, lo in zip(INDIGO_HI, INDIGO_LO)))
        bg = col.resize((C, C))
        bg.putalpha(mask)
        return bg
    im = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, C - 1, C - 1], radius=RADIUS, fill=INDIGO + (255,))
    return im


def make(variant):
    if variant == "B":
        # transparent bg, star only, fills the canvas
        R = 0.47 * C
        cy = C / 2 + 0.0955 * R  # optical centering (star bbox sits high)
        im = Image.new("RGBA", (C, C), (0, 0, 0, 0))
        ImageDraw.Draw(im).polygon(star_points(C / 2, cy, R, 0.40 * R), fill=INDIGO + (255,))
    else:
        R = 0.335 * C
        cy = C / 2 + 0.0955 * R
        im = rounded_bg(gradient=(variant == "C"))
        if variant == "C":
            sh = Image.new("RGBA", (C, C), (0, 0, 0, 0))
            ImageDraw.Draw(sh).polygon(star_points(C / 2, cy + round(0.018 * C), R, 0.40 * R),
                                       fill=(20, 10, 80, 90))
            sh = sh.filter(ImageFilter.GaussianBlur(round(0.015 * C)))
            im = Image.alpha_composite(im, sh)
        ImageDraw.Draw(im).polygon(star_points(C / 2, cy, R, 0.40 * R), fill=(255, 255, 255, 255))
    return im.resize((S, S), Image.Resampling.LANCZOS)


for v in "ABC":
    make(v).save(f"/tmp/icon-options/icon-{v}.png")
    print("saved", v)

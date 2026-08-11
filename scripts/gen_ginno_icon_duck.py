"""Candidate D: cute cartoon duck on the indigo gradient rounded-square bg."""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 1024
SS = 4
C = S * SS
INDIGO_HI = (141, 133, 246)
INDIGO_LO = (99, 86, 228)
RADIUS = round(0.2237 * C)

YELLOW = (255, 211, 78)
YELLOW_DK = (244, 181, 60)
ORANGE = (255, 145, 60)
BLACK = (43, 43, 51)
PINK = (255, 138, 138)


def bg():
    mask = Image.new("L", (C, C), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, C - 1, C - 1], radius=RADIUS, fill=255)
    col = Image.new("RGB", (1, C))
    for y in range(C):
        t = y / C
        col.putpixel((0, y), tuple(round(hi + (lo - hi) * t) for hi, lo in zip(INDIGO_HI, INDIGO_LO)))
    g = col.resize((C, C))
    g.putalpha(mask)
    return g


def circle(d, cx, cy, r, fill):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def star_points(cx, cy, R, r, rot=0.0):
    pts = []
    for i in range(10):
        rad = R if i % 2 == 0 else r
        a = -math.pi / 2 + i * math.pi / 5 + rot
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def star_layer(cx, cy, R, rot):
    lay = Image.new("RGBA", (C, C), (0, 0, 0, 0))
    ImageDraw.Draw(lay).polygon(star_points(cx, cy, R, 0.42 * R, rot),
                                fill=(255, 255, 255, 255))
    return lay


im = bg()
d = ImageDraw.Draw(im, "RGBA")

# tail nub (left)
circle(d, 1030, 2480, 260, YELLOW)
# body
d.ellipse([900, 1970, 3200, 3530], fill=YELLOW)
# wing (rotated darker ellipse)
wing = Image.new("RGBA", (C, C), (0, 0, 0, 0))
wd = ImageDraw.Draw(wing)
wd.ellipse([1300, 2430, 2260, 3070], fill=YELLOW_DK)
wing = wing.rotate(-18, resample=Image.Resampling.BICUBIC)
im = Image.alpha_composite(im, wing)
d = ImageDraw.Draw(im, "RGBA")
# head (big = cute)
circle(d, 2050, 1550, 780, YELLOW)
# hair tuft
circle(d, 1880, 790, 130, YELLOW)
circle(d, 2110, 760, 120, YELLOW)
# beak
d.ellipse([2620, 1500, 3260, 1880], fill=ORANGE)
# eye + highlight
circle(d, 2330, 1330, 95, BLACK)
circle(d, 2365, 1295, 32, (255, 255, 255))
# blush (separate layer: ImageDraw overwrites alpha instead of blending)
bl = Image.new("RGBA", (C, C), (0, 0, 0, 0))
ImageDraw.Draw(bl).ellipse([2230, 1780, 2590, 2000], fill=PINK + (110,))
im = Image.alpha_composite(im, bl)
# soft shadow under duck
sh = Image.new("RGBA", (C, C), (0, 0, 0, 0))
sd = ImageDraw.Draw(sh)
sd.ellipse([1000, 3300, 3100, 3660], fill=(20, 10, 80, 80))
sh = sh.filter(ImageFilter.GaussianBlur(round(0.012 * C)))

out = Image.alpha_composite(bg(), sh)
out = Image.alpha_composite(out, im)
# white star sparkles in the free background corners
for cx, cy, R, rot in [(3230, 830, 260, 0.26), (770, 1120, 170, -0.21), (3540, 2500, 120, 0.1)]:
    out = Image.alpha_composite(out, star_layer(cx, cy, R, rot))
out.resize((S, S), Image.Resampling.LANCZOS).save("/tmp/icon-options/icon-D-duck.png")
print("saved D")

"""Draw the bot's custom emoji set into assets/emojis/.

Every emoji is drawn from scratch here with Pillow (no third-party art), at
512px with thick outlines, then scaled to 128x128 PNG, Discord's recommended
size. File names are the emoji names: 2+ characters, letters, digits and
underscores only, so the folder can be bulk-uploaded as application emojis
(Developer Portal > your app > Emojis).

    .venv/bin/python scripts/make_emojis.py
    .venv/bin/python scripts/make_emojis.py --only zombie,kills

It also writes assets/emojis/README.md (the list, by category) and
assets/emojis/_preview.png (a contact sheet on Discord's dark and light
backgrounds). The preview isn't an emoji; don't upload it.

Text badges use Avenir Next Condensed Heavy, which ships with macOS.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFont

S = 512  # drawing size
OUT = 128  # emoji size
OW = 20  # outline width at drawing size
INK = (28, 24, 32, 255)

OUT_DIR = Path(__file__).resolve().parent.parent / "assets" / "emojis"
FONT = "/System/Library/Fonts/Avenir Next Condensed.ttc"
FONT_INDEX = 8  # Heavy

# Palette
WHITE = (248, 246, 240)
BONE = (238, 228, 200)
RED = (222, 52, 52)
ORANGE = (246, 132, 32)
YELLOW = (255, 204, 40)
GOLD = (255, 196, 46)
SILVER = (196, 206, 220)
BRONZE = (206, 128, 62)
GREEN = (92, 190, 72)
TOXIC = (150, 236, 40)
ZOMBIE = (128, 176, 92)
OLIVE = (112, 128, 62)
BLUE = (54, 132, 232)
CYAN = (60, 200, 230)
PURPLE = (146, 84, 222)
PINK = (240, 140, 170)
STEEL = (150, 160, 176)
DSTEEL = (82, 90, 106)
CHAR = (52, 54, 64)
WOOD = (182, 122, 66)
DWOOD = (120, 76, 40)
TAN = (214, 186, 132)
BROWN = (120, 82, 52)


def light(c, k=0.35):
    return tuple(int(v + (255 - v) * k) for v in c[:3])


def dark(c, k=0.3):
    return tuple(int(v * (1 - k)) for v in c[:3])


def sh(c, up=0.3, down=0.22):
    """A top-light, bottom-dark gradient fill for colour ``c``."""
    return (light(c, up), dark(c, down))


# --------------------------------------------------------------------------
# Masks
# --------------------------------------------------------------------------


def M():
    return Image.new("L", (S, S), 0)


def circle(cx, cy, r):
    m = M()
    ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    return m


def ellipse(box):
    m = M()
    ImageDraw.Draw(m).ellipse(box, fill=255)
    return m


def rect(box):
    m = M()
    ImageDraw.Draw(m).rectangle(box, fill=255)
    return m


def rrect(box, rad):
    m = M()
    ImageDraw.Draw(m).rounded_rectangle(box, rad, fill=255)
    return m


def poly(pts):
    m = M()
    ImageDraw.Draw(m).polygon([tuple(p) for p in pts], fill=255)
    return m


def stroke(pts, w):
    """A polyline of width ``w`` with round joins and caps."""
    m = M()
    d = ImageDraw.Draw(m)
    pts = [tuple(p) for p in pts]
    d.line(pts, fill=255, width=int(w), joint="curve")
    r = w / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    return m


def arc(box, a0, a1, w):
    m = M()
    ImageDraw.Draw(m).arc(box, a0, a1, fill=255, width=int(w))
    return m


def union(*ms):
    out = ms[0]
    for m in ms[1:]:
        out = ImageChops.lighter(out, m)
    return out


def minus(a, *bs):
    for b in bs:
        a = ImageChops.subtract(a, b)
    return a


def inter(a, b):
    return ImageChops.darker(a, b)


def rot(m, deg, center=(S / 2, S / 2)):
    """Rotate a mask ``deg`` degrees clockwise about ``center``."""
    return m.rotate(-deg, resample=Image.BICUBIC, center=center)


def shift(m, dx, dy):
    out = M()
    out.paste(m, (int(dx), int(dy)))
    return out


def ngon(cx, cy, r, n, start=-90):
    return [
        (cx + r * math.cos(math.radians(start + 360 * i / n)),
         cy + r * math.sin(math.radians(start + 360 * i / n)))
        for i in range(n)
    ]


def star_pts(cx, cy, R, r, n=5, start=-90):
    pts = []
    for i in range(2 * n):
        rad = R if i % 2 == 0 else r
        a = math.radians(start + 180 * i / n)
        pts.append((cx + rad * math.cos(a), cy + rad * math.sin(a)))
    return pts


def bezier(p0, p1, p2, n=24):
    return [
        ((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
         (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1])
        for t in (i / n for i in range(n + 1))
    ]


def shield_pts(cx, top, w, h):
    x0, x1 = cx - w / 2, cx + w / 2
    right = bezier((x1, top + h * 0.42), (x1, top + h * 0.82), (cx, top + h))
    left = bezier((cx, top + h), (x0, top + h * 0.82), (x0, top + h * 0.42))
    return [(x0, top + h * 0.06), (cx, top), (x1, top + h * 0.06)] + right + left


def flame_pts(cx, base, w, h, m=1.4):
    """Teardrop flame: rounded bottom at ``base``, tip ``h`` above it."""
    pts = []
    for i in range(72):
        t = 2 * math.pi * i / 72
        x = math.sin(t) * math.sin(t / 2) ** m
        pts.append((cx + x * w / 2 / 0.77, base - h * (1 + math.cos(t)) / 2))
    return pts


_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def font(size):
    size = max(8, int(size))
    if size not in _font_cache:
        try:
            _font_cache[size] = ImageFont.truetype(FONT, size, index=FONT_INDEX)
        except OSError:
            _font_cache[size] = ImageFont.load_default(size)
    return _font_cache[size]


def text(txt, cx, cy, h=None, w=None):
    """Mask of ``txt`` with its ink box centred on (cx, cy), at most h x w."""
    probe = font(200)
    b = probe.getbbox(txt)
    k = min(h / (b[3] - b[1]) if h else 1e9, w / (b[2] - b[0]) if w else 1e9)
    f = font(200 * k)
    b = f.getbbox(txt)
    m = M()
    ImageDraw.Draw(m).text(
        (cx - (b[0] + b[2]) / 2, cy - (b[1] + b[3]) / 2), txt, font=f, fill=255)
    return m


def dilate(m, r):
    """Grow a mask by ``r`` pixels (near-circular: square and cross steps)."""
    a = np.asarray(m, dtype=np.uint8)
    for i in range(int(r)):
        p = np.pad(a, 1)
        h, w = a.shape
        out = np.maximum.reduce([
            p[1:h + 1, 1:w + 1], p[0:h, 1:w + 1], p[2:h + 2, 1:w + 1],
            p[1:h + 1, 0:w], p[1:h + 1, 2:w + 2],
        ])
        if i % 2 == 0:
            out = np.maximum.reduce([
                out, p[0:h, 0:w], p[0:h, 2:w + 2], p[2:h + 2, 0:w],
                p[2:h + 2, 2:w + 2],
            ])
        a = out
    return Image.fromarray(a, "L")


# --------------------------------------------------------------------------
# Canvas
# --------------------------------------------------------------------------


class C:
    def __init__(self):
        self.img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    def put(self, m, fill, ow=OW, oc=INK):
        """Paint mask ``m``: an ``ow`` outline, then a colour or gradient."""
        if ow:
            self.img.paste(oc, (0, 0, S, S), dilate(m, ow))
        if isinstance(fill[0], (tuple, list)):
            box = m.getbbox() or (0, 0, S, S)
            top, bot = fill
            g = Image.new("RGBA", (1, 256))
            for y in range(256):
                t = y / 255
                g.putpixel((0, y), tuple(
                    int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
            g = g.resize((S, max(1, box[3] - box[1])), Image.BILINEAR)
            layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            layer.paste(g, (0, box[1]))
            self.img.paste(layer, (0, 0), m)
        else:
            col = tuple(fill) + ((255,) if len(fill) == 3 else ())
            self.img.paste(col, (0, 0, S, S), m)
        return self

    def ink(self, m, color=INK):
        return self.put(m, color, ow=0)

    def gloss(self, m, a=70):
        """A soft white sheen over the top-left of mask ``m``."""
        box = m.getbbox()
        if not box:
            return self
        x0, y0, x1, y1 = box
        w, h = x1 - x0, y1 - y0
        hl = inter(m, ellipse((x0 - w * 0.3, y0 - h * 0.5, x0 + w * 0.85, y0 + h * 0.48)))
        hl = hl.point(lambda v: v * a // 255)
        self.img.paste((255, 255, 255, 255), (0, 0, S, S), hl)
        return self

    def stamp(self, other, scale, cx, cy, ring=0):
        """Draw another canvas scaled by ``scale`` and centred on (cx, cy)."""
        size = int(S * scale)
        im = other.img.resize((size, size), Image.LANCZOS)
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        layer.paste(im, (int(cx - size / 2), int(cy - size / 2)))
        if ring:
            self.img.paste(INK, (0, 0, S, S), dilate(layer.getchannel("A"), ring))
        self.img.alpha_composite(layer)
        return self

    def label(self, txt, cx, cy, h, w, fill=WHITE, ow=12):
        return self.put(text(txt, cx, cy, h, w), fill, ow=ow)


# --------------------------------------------------------------------------
# Shared glyphs
# --------------------------------------------------------------------------


def disc(c, color, r=226):
    m = circle(256, 256, r)
    c.put(m, sh(color))
    return m


def check_mask(cx, cy, s, w):
    return stroke([(cx - 0.42 * s, cy), (cx - 0.12 * s, cy + 0.3 * s),
                   (cx + 0.45 * s, cy - 0.35 * s)], w)


def x_mask(cx, cy, s, w):
    return union(stroke([(cx - s / 2, cy - s / 2), (cx + s / 2, cy + s / 2)], w),
                 stroke([(cx - s / 2, cy + s / 2), (cx + s / 2, cy - s / 2)], w))


def arrow_mask(cx, cy, length, w, angle=0):
    """Thick arrow pointing up (angle 0), rotated clockwise by ``angle``."""
    top = cy - length / 2
    head = w * 1.9
    m = union(
        rect((cx - w / 2, top + head * 0.8, cx + w / 2, cy + length / 2)),
        poly([(cx, top), (cx + head, top + head * 1.05), (cx - head, top + head * 1.05)]),
    )
    return rot(m, angle, (cx, cy))


def skull(c, color=BONE):
    cranium = union(circle(256, 220, 158), rrect((176, 270, 336, 400), 34))
    c.put(cranium, sh(color, 0.2, 0.18)).gloss(cranium, 50)
    c.ink(ellipse((170, 186, 240, 262)))
    c.ink(ellipse((272, 186, 342, 262)))
    c.ink(poly([(256, 276), (236, 316), (276, 316)]))
    for x in (218, 256, 294):
        c.ink(stroke([(x, 352), (x, 400)], 10))
    return c


def zombie_head(c, mood="normal"):
    head = ellipse((112, 88, 400, 432))
    c.put(head, sh(ZOMBIE))
    brain = inter(head, ellipse((136, 56, 316, 206)))
    c.put(brain, sh(PINK, 0.2, 0.15), ow=12)
    c.ink(stroke([(170, 150), (196, 124), (222, 146), (250, 120), (278, 140)], 9))
    # Stitched cheek
    c.ink(stroke([(316, 330), (372, 300)], 9))
    for i in range(3):
        x, y = 326 + i * 18, 324 - i * 10
        c.ink(stroke([(x - 6, y - 14), (x + 6, y + 14)], 7))
    if mood == "dead":
        c.ink(x_mask(204, 250, 60, 18))
        c.ink(x_mask(316, 244, 52, 18))
    else:
        c.put(circle(204, 252, 46), WHITE, ow=10)
        c.put(circle(318, 244, 36), WHITE, ow=10)
        pupil = RED if mood == "angry" else INK[:3]
        c.put(circle(214, 262, 16), pupil, ow=0)
        c.put(circle(312, 238, 12), pupil, ow=0)
        if mood == "angry":
            c.ink(stroke([(150, 196), (244, 226)], 18))
            c.ink(stroke([(276, 220), (360, 194)], 18))
    mouth = poly([(178, 330), (344, 316), (334, 380), (256, 392), (192, 376)])
    c.ink(mouth)
    for x0, y0 in ((196, 330), (236, 326), (280, 322)):
        c.put(poly([(x0, y0 + 2), (x0 + 30, y0 - 1), (x0 + 18, y0 + 26)]), BONE, ow=0)
    c.put(poly([(232, 392), (262, 390), (250, 366)]), BONE, ow=0)
    return c


def crosshair(c, color=RED, r=150, cx=256, cy=256, w=26):
    c.put(union(
        arc((cx - r, cy - r, cx + r, cy + r), 0, 360, w),
        rect((cx - w / 2, cy - r - 46, cx + w / 2, cy - r + 50)),
        rect((cx - w / 2, cy + r - 50, cx + w / 2, cy + r + 46)),
        rect((cx - r - 46, cy - w / 2, cx - r + 50, cy + w / 2)),
        rect((cx + r - 50, cy - w / 2, cx + r + 46, cy + w / 2)),
        circle(cx, cy, w * 0.7),
    ), color, ow=12)
    return c


def gear_mask(cx, cy, R, teeth=8):
    body = circle(cx, cy, R * 0.76)
    tw = R * 0.36
    for i in range(teeth):
        tooth = rrect((cx - tw / 2, cy - R, cx + tw / 2, cy - R * 0.6), 8)
        body = union(body, rot(tooth, 360 * i / teeth, (cx, cy)))
    return minus(body, circle(cx, cy, R * 0.3))


def shield(c, color, w=320, h=380, top=66):
    m = poly(shield_pts(256, top, w, h))
    c.put(m, sh(color)).gloss(m, 55)
    return m


def flag_mask(x, top, w, h, wave=26):
    """Waving flag attached at x, from y=top, size w x h."""
    upper = bezier((x, top), (x + w / 2, top - wave * 1.6), (x + w, top + wave * 0.3), 16)
    lower = bezier((x + w, top + h + wave * 0.3), (x + w / 2, top + h - wave * 1.6), (x, top + h), 16)
    return poly(upper + lower)


def flag(c, color, x=150, top=86, w=240, h=170, pole=STEEL):
    c.put(rrect((x - 22, top - 30, x + 6, 452), 12), sh(pole))
    c.put(circle(x - 8, top - 34, 22), sh(GOLD))
    m = flag_mask(x + 6, top, w, h)
    c.put(m, sh(color)).gloss(m, 40)
    return m


def sword_mask():
    blade = poly([(240, 330), (240, 104), (256, 64), (272, 104), (272, 330)])
    guard = rrect((196, 326, 316, 352), 12)
    grip = rect((244, 352, 268, 428))
    pommel = circle(256, 438, 20)
    return blade, union(guard, grip, pommel)


def crossed_swords(c, blade=SILVER, hilt=GOLD):
    for ang in (-42, 42):
        b, h = sword_mask()
        c.put(rot(b, ang), sh(blade))
        c.put(rot(h, ang), sh(hilt))
    return c


def eye(c, cx=256, cy=256, s=1.0, iris=CYAN):
    w, h = 190 * s, 110 * s
    lens = union(poly(bezier((cx - w, cy), (cx, cy - h * 1.9), (cx + w, cy)) +
                      bezier((cx + w, cy), (cx, cy + h * 1.9), (cx - w, cy))))
    c.put(lens, WHITE)
    c.put(inter(lens, circle(cx, cy, 82 * s)), sh(iris), ow=10)
    c.ink(circle(cx, cy, 36 * s))
    c.put(circle(cx - 24 * s, cy - 26 * s, 14 * s), WHITE, ow=0)
    return c


def doc(c, color=WHITE, box=(110, 64, 402, 448)):
    x0, y0, x1, y1 = box
    fold = 70
    m = poly([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)])
    c.put(rrect(box, 2), color, ow=OW, oc=INK)
    c.img.paste((0, 0, 0, 0), (0, 0, S, S), minus(rrect(box, 2), m))  # cut corner
    c.put(m, sh(color, 0.1, 0.12))
    c.put(poly([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)]), light(dark(color, 0.18), 0.1), ow=10)
    return m


def database(c, color=GREEN, box=(124, 70, 388, 440)):
    x0, y0, x1, y1 = box
    eh = 70
    body = union(rect((x0, y0 + eh / 2, x1, y1 - eh / 2)), ellipse((x0, y1 - eh, x1, y1)),
                 ellipse((x0, y0, x1, y0 + eh)))
    c.put(body, sh(color))
    for k in (1, 2):
        y = y0 + (y1 - y0 - eh) * k / 3
        c.ink(arc((x0, y, x1, y + eh), 0, 180, 12))
    c.put(ellipse((x0 + 4, y0, x1 - 4, y0 + eh)), light(color, 0.35), ow=10)
    return body


def coin(c, color=GOLD, cx=256, cy=256, r=200):
    m = circle(cx, cy, r)
    c.put(m, sh(color, 0.25, 0.25))
    c.put(arc((cx - r * 0.8, cy - r * 0.8, cx + r * 0.8, cy + r * 0.8), 0, 360, 12),
          dark(color, 0.25), ow=0)
    c.gloss(m, 60)
    return m


def stopwatch(c, ring, center_txt, txt_fill=INK[:3]):
    c.put(rrect((226, 30, 286, 90), 12), sh(STEEL))
    c.put(rot(rrect((358, 82, 398, 132), 10), 40, (378, 107)), sh(STEEL))
    face = circle(256, 284, 194)
    c.put(face, sh(ring))
    c.put(circle(256, 284, 152), WHITE, ow=12)
    c.put(text(center_txt, 256, 290, 170, 210), txt_fill, ow=0)
    return c


def flame(c, cx=256, base=440, w=300, h=400, inner=True):
    outer = poly(flame_pts(cx, base, w, h))
    side_l = rot(poly(flame_pts(cx - w * 0.28, base - 10, w * 0.45, h * 0.55)), -24, (cx, base))
    side_r = rot(poly(flame_pts(cx + w * 0.28, base - 10, w * 0.45, h * 0.55)), 24, (cx, base))
    m = union(outer, side_l, side_r)
    c.put(m, (YELLOW, (232, 64, 30)))
    if inner:
        c.put(poly(flame_pts(cx, base - 16, w * 0.5, h * 0.58)), (WHITE, YELLOW), ow=0)
    return m


def hexbadge(c, color, r=236):
    m = poly(ngon(256, 256, r, 6, start=-90))
    c.put(m, sh(color)).gloss(m, 45)
    return m


def pill(c, color, txt, wide=True):
    box = (14, 112, 498, 400) if wide else (40, 40, 472, 472)
    m = rrect(box, 90)
    c.put(m, sh(color)).gloss(m, 55)
    y0, y1 = box[1], box[3]
    c.label(txt, 256, (y0 + y1) / 2, (y1 - y0) * 0.62, (box[2] - box[0]) * 0.8)
    return c


def bullet(c, cx=256, top=60, w=120, h=392):
    tip = union(ellipse((cx - w / 2, top, cx + w / 2, top + w * 1.6)),)
    tip = inter(tip, rect((0, 0, S, top + w * 0.95)))
    tip = union(tip, rect((cx - w / 2, top + w * 0.78, cx + w / 2, top + w * 1.05)))
    case = rect((cx - w / 2, top + w * 1.05, cx + w / 2, top + h - 30))
    rim = rrect((cx - w / 2 - 8, top + h - 40, cx + w / 2 + 8, top + h), 6)
    c.put(union(case, rim), sh((222, 170, 60)))
    c.put(tip, sh((206, 112, 60)))
    c.gloss(case, 60)
    c.ink(rect((cx - w / 2, top + h - 56, cx + w / 2, top + h - 46)))
    return c


def crate(c, color=OLIVE, box=(96, 140, 416, 440)):
    x0, y0, x1, y1 = box
    m = rrect(box, 14)
    c.put(m, sh(color, 0.2, 0.2))
    inset = 34
    c.put(minus(m, rect((x0 + inset, y0 + inset, x1 - inset, y1 - inset))), dark(color, 0.18), ow=0)
    c.put(stroke([(x0 + inset, y1 - inset), (x1 - inset, y0 + inset)], 34), dark(color, 0.18), ow=8)
    for x, y in ((x0 + 17, y0 + 17), (x1 - 17, y0 + 17), (x0 + 17, y1 - 17), (x1 - 17, y1 - 17)):
        c.put(circle(x, y, 8), STEEL, ow=4)
    return m


def gift_box(c, color=RED, ribbon=GOLD):
    c.put(union(ellipse((150, 70, 262, 166)), ellipse((250, 70, 362, 166))), sh(ribbon))
    c.ink(ellipse((186, 100, 236, 140)))
    c.ink(ellipse((276, 100, 326, 140)))
    c.put(rect((110, 230, 402, 440)), sh(color))
    c.put(rrect((90, 150, 422, 236), 12), sh(light(color, 0.1)))
    c.put(union(rect((230, 150, 282, 440))), sh(ribbon), ow=8)
    return c


def calendar(c, header, body_txt, header_txt=""):
    c.put(rrect((70, 86, 442, 450), 40), sh(WHITE, 0.0, 0.12))
    top = inter(rrect((70, 86, 442, 450), 40), rect((0, 0, S, 190)))
    c.put(top, sh(header), ow=0)
    c.ink(rect((70, 186, 442, 196)))
    for x in (170, 342):
        c.put(rrect((x - 16, 50, x + 16, 128), 14), sh(STEEL), ow=10)
    if header_txt:
        c.label(header_txt, 256, 142, 64, 160, ow=8)
    c.put(text(body_txt, 256, 322, 190, 280), INK[:3], ow=0)
    return c


def medal(c, color, txt, ribbon=(RED, BLUE)):
    c.put(poly([(150, 20), (230, 20), (300, 250), (220, 250)]), sh(ribbon[0]))
    c.put(poly([(362, 20), (282, 20), (212, 250), (292, 250)]), sh(ribbon[1]))
    m = circle(256, 320, 168)
    c.put(m, sh(color, 0.3, 0.25))
    c.put(arc((126, 190, 386, 450), 0, 360, 14), dark(color, 0.28), ow=0)
    c.gloss(m, 50)
    c.label(txt, 256, 324, 170, 210 if len(txt) < 2 else 230)
    return c


def trophy(c, color=GOLD):
    cup = poly([(126, 70), (386, 70)] + bezier((386, 70), (392, 300), (256, 316))[1:]
               + bezier((256, 316), (120, 300), (126, 70))[1:])
    handles = union(arc((56, 96, 196, 250), 90, 300, 30), arc((316, 96, 456, 250), 240, 450, 30))
    c.put(handles, sh(color))
    c.put(cup, sh(color, 0.35, 0.25)).gloss(cup, 60)
    c.put(rect((226, 300, 286, 380)), sh(color))
    c.put(rrect((150, 370, 362, 450), 16), sh(BROWN))
    return cup


def bar_chart(c, values, colors, box=(96, 120, 416, 420)):
    x0, y0, x1, y1 = box
    n = len(values)
    bw = (x1 - x0) / n
    for i, (v, col) in enumerate(zip(values, colors)):
        bx = x0 + i * bw + 12
        c.put(rrect((bx, y1 - (y1 - y0) * v, bx + bw - 24, y1), 10), sh(col))
    return c


# --------------------------------------------------------------------------
# Emoji registry
# --------------------------------------------------------------------------

EMOJIS: list[tuple[str, str, str, object]] = []  # (name, category, caption, fn)


def emoji(name, category, caption):
    def deco(fn):
        EMOJIS.append((name, category, caption, fn))
        return fn
    return deco


def add(name, category, caption, fn):
    EMOJIS.append((name, category, caption, fn))


# ---- Alliance metrics -----------------------------------------------------


@emoji("vs_points", "Alliance metrics", "Versus Points: red vs blue split badge")
def _(c):
    m = circle(256, 256, 226)
    c.put(m, sh(BLUE))
    split = poly([(0, 0), (300, 0), (226, 230), (290, 262), (212, 512), (0, 512)])
    c.put(inter(m, split), sh(RED), ow=0)
    c.ink(stroke([(300, 20), (226, 230), (290, 262), (212, 492)], 16))
    c.gloss(m, 50)
    c.label("VS", 256, 262, 190, 330, ow=16)


@emoji("tech_contribution", "Alliance metrics", "Tech Contribution: gear and research flask")
def _(c):
    g = gear_mask(206, 214, 180, 9)
    c.put(g, sh(CYAN, 0.2, 0.3))
    fl = C()
    neck = rect((222, 60, 290, 210))
    body = poly([(222, 200), (290, 200), (420, 430), (92, 430)])
    shape = union(neck, rrect((92, 300, 420, 450), 40), body)
    fl.put(shape, (light(WHITE, 0), (210, 226, 236)))
    liquid = inter(dilate(shape, 0), poly([(160, 300), (352, 300), (420, 450), (92, 450)]))
    fl.put(liquid, sh(TOXIC), ow=0)
    fl.put(rrect((204, 46, 308, 84), 14), sh(STEEL), ow=14)
    for x, y, r in ((220, 360, 18), (290, 390, 12), (260, 332, 10)):
        fl.put(circle(x, y, r), WHITE, ow=0)
    c.stamp(fl, 0.62, 340, 330, ring=14)


@emoji("hq_level", "Alliance metrics", "HQ Level: fortified headquarters with an up-arrow")
def _(c):
    c.put(rrect((60, 220, 452, 452), 10), sh(DSTEEL))
    for x in range(60, 452, 72):
        c.put(rect((x, 186, x + 44, 230)), sh(DSTEEL), ow=12)
    c.put(rrect((150, 120, 362, 452), 8), sh(STEEL))
    c.put(poly([(130, 132), (256, 54), (382, 132)]), sh(RED))
    c.ink(rrect((216, 330, 296, 452), 30))
    for x in (176, 296):
        c.put(rect((x, 190, x + 40, 240)), YELLOW, ow=10)
    c.put(arrow_mask(400, 330, 200, 44), sh(GREEN), ow=14)


@emoji("power", "Alliance metrics", "Power: gold shield with a lightning bolt")
def _(c):
    shield(c, GOLD)
    bolt = poly([(286, 100), (178, 280), (248, 280), (210, 420), (338, 220), (264, 220), (304, 100)])
    c.put(bolt, (WHITE, YELLOW), ow=14)


@emoji("arena_power", "Alliance metrics", "Arena Power: crossed swords on a purple shield")
def _(c):
    shield(c, PURPLE)
    sw = C()
    crossed_swords(sw)
    c.stamp(sw, 0.78, 256, 248)


@emoji("kills", "Alliance metrics", "Kills: skull in red crosshairs")
def _(c):
    skull(c)
    crosshair(c, RED, r=196, w=24)


@emoji("growth_up", "Alliance metrics", "Week-on-week growth: rising bars")
def _(c):
    c.put(rrect((40, 40, 472, 472), 70), sh(CHAR))
    bar_chart(c, (0.25, 0.45, 0.65, 0.9), (GREEN,) * 4, (80, 150, 432, 430))
    c.put(stroke([(100, 300), (210, 220), (290, 250), (400, 130)], 26), WHITE, ow=12)
    c.put(rot(arrow_mask(400, 130, 120, 0.1), 0), WHITE, ow=0)
    c.put(poly([(430, 96), (424, 196), (338, 124)]), WHITE, ow=12)


@emoji("growth_down", "Alliance metrics", "Week-on-week drop: falling bars")
def _(c):
    c.put(rrect((40, 40, 472, 472), 70), sh(CHAR))
    bar_chart(c, (0.9, 0.65, 0.45, 0.25), (RED,) * 4, (80, 150, 432, 430))
    c.put(stroke([(100, 150), (210, 260), (290, 230), (390, 340)], 26), WHITE, ow=12)
    c.put(poly([(426, 380), (322, 372), (404, 284)]), WHITE, ow=12)


@emoji("personal_best", "Alliance metrics", "Personal best: gold star with PB")
def _(c):
    m = poly(star_pts(256, 268, 240, 112))
    c.put(m, sh(GOLD, 0.35, 0.25)).gloss(m, 60)
    c.label("PB", 256, 284, 110, 150, ow=12)


# ---- Ranks and placings --------------------------------------------------

for _n, _col in ((1, STEEL), (2, GREEN), (3, BLUE), (4, PURPLE), (5, GOLD)):
    def _rank(c, n=_n, col=_col):
        shield(c, col, w=340, h=400, top=50)
        c.label(f"R{n}", 256, 220, 150, 230, ow=14)
        for i in range(min(n, 3) if n < 5 else 0):
            y = 330 + i * 30 - (min(n, 3) - 1) * 15
            c.put(stroke([(206, y - 10), (256, y + 14), (306, y - 10)], 20), WHITE, ow=8)
        if n >= 4:
            c.put(poly(star_pts(256, 342, 52, 22)), WHITE if n == 4 else (255, 250, 220), ow=10)
    add(f"rank_r{_n}", "Ranks and placings", f"Alliance rank R{_n} badge", _rank)

for _n in range(1, 11):
    _col = {1: GOLD, 2: SILVER, 3: BRONZE}.get(_n, (92, 150, 210))
    _rib = {1: (RED, BLUE), 2: (BLUE, (40, 80, 160)), 3: (GREEN, (40, 120, 60))}.get(
        _n, (DSTEEL, CHAR))
    add(f"place_{_n}", "Ranks and placings", f"Leaderboard place {_n} medal",
        lambda c, n=_n, col=_col, rib=_rib: medal(c, col, str(n), rib))


@emoji("leader", "Ranks and placings", "Alliance leader: crown over a shield")
def _(c):
    shield(c, BLUE, w=300, h=300, top=176)
    crown = poly([(126, 236), (110, 92), (186, 156), (256, 60), (326, 156), (402, 92), (386, 236)])
    c.put(crown, sh(GOLD, 0.35, 0.2)).gloss(crown, 50)
    for x, y in ((110, 92), (256, 60), (402, 92)):
        c.put(circle(x, y, 22), sh(GOLD), ow=12)
    c.put(circle(256, 186, 22), sh(RED), ow=10)
    c.label("Z", 256, 350, 110, 120, ow=12)


@emoji("leaderboard", "Ranks and placings", "Leaderboard podium 1-2-3")
def _(c):
    c.put(rect((48, 250, 186, 452)), sh(SILVER))
    c.put(rect((326, 300, 464, 452)), sh(BRONZE))
    c.put(rect((176, 170, 336, 452)), sh(GOLD))
    c.label("1", 256, 300, 150, 120, ow=12)
    c.label("2", 117, 350, 120, 100, ow=12)
    c.label("3", 395, 376, 100, 100, ow=12)
    c.put(poly(star_pts(256, 90, 70, 30)), sh(GOLD), ow=14)


# ---- Alliance and VS duel ------------------------------------------------


@emoji("alliance", "Alliance and VS duel", "Alliance shield with a Z crest")
def _(c):
    shield(c, BLUE, w=360, h=420, top=46)
    c.put(poly(shield_pts(256, 100, 250, 300)), sh(dark(BLUE, 0.35)), ow=12)
    c.label("Z", 256, 236, 170, 180, fill=TOXIC, ow=14)


@emoji("attack", "Alliance and VS duel", "Attack: crossed swords on red")
def _(c):
    disc(c, RED)
    sw = C()
    crossed_swords(sw)
    c.stamp(sw, 0.84, 256, 256)


@emoji("defend", "Alliance and VS duel", "Defend: steel shield with a blue band")
def _(c):
    m = shield(c, STEEL, w=360, h=420, top=46)
    c.put(inter(m, rect((206, 0, 306, S))), sh(BLUE), ow=0)
    c.put(inter(m, rect((0, 180, S, 260))), sh(BLUE), ow=0)
    c.put(circle(256, 220, 46), sh(GOLD), ow=12)


@emoji("reinforce", "Alliance and VS duel", "Reinforce: green shield with a plus")
def _(c):
    shield(c, GREEN, w=360, h=420, top=46)
    c.put(union(rrect((222, 120, 290, 360), 14), rrect((136, 206, 376, 274), 14)), WHITE, ow=14)


@emoji("rally", "Alliance and VS duel", "Rally: red war flag")
def _(c):
    flag(c, RED)
    c.label("!", 270, 170, 130, 80, ow=12)


@emoji("rally_point", "Alliance and VS duel", "Rally point: map pin with crossed swords")
def _(c):
    pin = union(circle(256, 200, 160), poly([(130, 290), (382, 290), (256, 476)]))
    c.put(pin, sh(RED)).gloss(pin, 50)
    c.put(circle(256, 200, 110), WHITE, ow=12)
    sw = C()
    crossed_swords(sw, blade=CHAR, hilt=CHAR)
    c.stamp(sw, 0.42, 256, 200)


@emoji("scout", "Alliance and VS duel", "Scout: watchful eye")
def _(c):
    disc(c, CHAR)
    eye(c, s=1.0, iris=TOXIC)


@emoji("teleport", "Alliance and VS duel", "Teleport: purple vortex")
def _(c):
    disc(c, PURPLE)
    pts = [(256 + (12 + 9.5 * t) * math.cos(t * 0.9 - 1), 256 + (12 + 9.5 * t) * math.sin(t * 0.9 - 1))
           for t in np.linspace(0, 20, 240)]
    c.put(stroke(pts, 30), (WHITE, light(PURPLE, 0.6)), ow=10)


@emoji("territory", "Alliance and VS duel", "Territory: hex tile with a planted flag")
def _(c):
    tile = poly([(p[0], p[1] * 0.62 + 200) for p in ngon(256, 256, 230, 6, start=0)])
    c.put(shift(tile, 0, 46), sh(BROWN), ow=OW)
    c.put(tile, sh(GREEN))
    c.put(rrect((236, 70, 256, 370), 8), sh(STEEL), ow=12)
    m = flag_mask(256, 80, 150, 110, wave=18)
    c.put(m, sh(BLUE), ow=14)


@emoji("territory_lost", "Alliance and VS duel", "Territory lost: scorched tile, broken flag")
def _(c):
    tile = poly([(p[0], p[1] * 0.62 + 200) for p in ngon(256, 256, 230, 6, start=0)])
    c.put(shift(tile, 0, 46), sh(DWOOD), ow=OW)
    c.put(tile, sh((126, 112, 100)))
    c.ink(stroke([(170, 330), (220, 360), (200, 400), (260, 420)], 10))
    c.put(rot(rrect((236, 150, 256, 370), 8), 28, (246, 370)), sh(STEEL), ow=12)
    c.put(rot(flag_mask(256, 160, 120, 90, wave=14), 28, (246, 370)), sh(RED), ow=14)


@emoji("planner", "Alliance and VS duel", "Territory planner: folded map with a route")
def _(c):
    panels = [[(60, 110), (190, 70), (190, 410), (60, 450)],
              [(190, 70), (322, 110), (322, 450), (190, 410)],
              [(322, 110), (452, 70), (452, 410), (322, 450)]]
    for i, p in enumerate(panels):
        c.put(poly(p), sh((228, 214, 170) if i != 1 else (206, 192, 150)))
    dots = [(100, 380), (150, 320), (210, 300), (260, 240), (300, 190), (350, 170)]
    for x, y in dots:
        c.ink(circle(x, y, 17), RED)
    c.put(x_mask(396, 150, 66, 24), RED, ow=8)


@emoji("vs_win", "Alliance and VS duel", "VS duel win banner")
def _(c):
    pill(c, GREEN, "WIN")


@emoji("vs_loss", "Alliance and VS duel", "VS duel loss banner")
def _(c):
    pill(c, RED, "LOSS")


@emoji("vs_draw", "Alliance and VS duel", "VS duel draw banner")
def _(c):
    pill(c, STEEL, "DRAW")


for _d in range(1, 7):
    add(f"vs_day{_d}", "Alliance and VS duel", f"VS duel day {_d} calendar tile",
        lambda c, d=_d: calendar(c, RED, str(d), "VS"))


# ---- Zombies and survival ------------------------------------------------

add("zombie", "Zombies and survival", "Zombie head", lambda c: zombie_head(c))
add("zombie_angry", "Zombies and survival", "Angry red-eyed zombie", lambda c: zombie_head(c, "angry"))
add("zombie_dead", "Zombies and survival", "Downed zombie with X eyes", lambda c: zombie_head(c, "dead"))


@emoji("zombie_horde", "Zombies and survival", "A horde of zombies")
def _(c):
    for x, y, mood in ((140, 190, "normal"), (372, 190, "dead"), (256, 300, "angry")):
        z = C()
        zombie_head(z, mood)
        c.stamp(z, 0.6, x, y, ring=10)


@emoji("zombie_hand", "Zombies and survival", "Zombie hand bursting from the ground")
def _(c):
    c.put(rect((198, 330, 318, 430)), sh((90, 80, 110)))
    fingers = [(196, 110, 222), (226, 70, 252), (258, 66, 284), (290, 96, 316)]
    hand = union(rrect((190, 200, 322, 360), 40),
                 *[rrect((x0, y0, x1, 260), 13) for x0, y0, x1 in fingers],
                 stroke([(204, 300), (140, 226)], 36))
    c.put(hand, sh(ZOMBIE))
    for x0, y0, x1 in fingers:
        c.put(rrect((x0 + 4, y0 + 4, x1 - 4, y0 + 22), 6), BONE, ow=0)
    c.ink(stroke([(230, 300), (256, 280), (270, 310)], 8))
    dirt = union(ellipse((50, 380, 462, 480)), circle(150, 390, 40), circle(360, 386, 46))
    c.put(dirt, sh(BROWN))
    for x, y in ((120, 330), (390, 320), (420, 360)):
        c.put(circle(x, y, 16), sh(BROWN), ow=10)


@emoji("headshot", "Zombies and survival", "Headshot: zombie in the crosshairs")
def _(c):
    zombie_head(c)
    crosshair(c, RED, r=196, w=24)


@emoji("infected", "Zombies and survival", "Infection: toxic biohazard mark")
def _(c):
    disc(c, CHAR)
    R = 170
    sym = M()
    for k in range(3):
        a = math.radians(-90 + 120 * k)
        lobe = circle(256 + 0.42 * R * math.cos(a), 266 + 0.42 * R * math.sin(a), 0.52 * R)
        cut = circle(256 + 0.56 * R * math.cos(a), 266 + 0.56 * R * math.sin(a), 0.36 * R)
        sym = union(sym, minus(lobe, cut))
    sym = union(sym, minus(circle(256, 266, 0.36 * R), circle(256, 266, 0.24 * R)))
    for k in range(3):
        a = math.radians(-90 + 120 * k)
        sym = minus(sym, stroke([(256, 266), (256 + R * math.cos(a), 266 + R * math.sin(a))], 14))
    sym = minus(sym, circle(256, 266, 0.12 * R))
    c.put(sym, (light(TOXIC, 0.3), dark(TOXIC, 0.25)), ow=12)


@emoji("hazard_zone", "Zombies and survival", "Danger zone sign with a skull")
def _(c):
    tri = poly([(256, 40), (476, 440), (36, 440)])
    c.put(dilate(tri, 18), sh(YELLOW))
    sk = C()
    skull(sk, BONE)
    c.stamp(sk, 0.56, 256, 312, ring=10)


@emoji("blood_moon", "Zombies and survival", "Blood moon night event")
def _(c):
    disc(c, (34, 30, 52))
    for x, y, r in ((130, 120, 8), (380, 400, 6), (400, 150, 7), (170, 400, 5)):
        c.put(poly(star_pts(x, y, r * 2.4, r)), WHITE, ow=0)
    m = minus(circle(236, 256, 168), circle(320, 206, 150))
    c.put(m, ((250, 96, 70), (140, 16, 24)))
    for x, y, r in ((120, 270, 22), (170, 360, 30), (220, 400, 14)):
        c.put(inter(m, circle(x, y, r)), dark(RED, 0.4), ow=0)


@emoji("bullet", "Zombies and survival", "Rifle round")
def _(c):
    b = C()
    bullet(b)
    c.stamp(b, 1.0, 256, 256)
    c.img = c.img.rotate(-30, resample=Image.BICUBIC)


@emoji("ammo", "Zombies and survival", "Ammo: three rounds")
def _(c):
    for x in (146, 256, 366):
        bullet(c, cx=x, top=70, w=92, h=380)


@emoji("grenade", "Zombies and survival", "Frag grenade")
def _(c):
    body = ellipse((126, 150, 366, 460))
    c.put(body, sh(OLIVE, 0.25, 0.3))
    for y in (250, 330, 400):
        c.ink(inter(body, rect((0, y - 6, S, y + 6))))
    for x in (206, 286):
        c.ink(inter(body, rect((x - 6, 0, x + 6, S))))
    c.gloss(body, 40)
    c.put(rrect((196, 100, 296, 170), 14), sh(STEEL))
    c.put(stroke([(296, 118), (366, 150), (390, 300)], 30), sh(STEEL))
    c.put(arc((96, 40, 206, 150), 0, 360, 18), sh(GOLD), ow=10)


@emoji("molotov", "Zombies and survival", "Molotov cocktail")
def _(c):
    flame(c, 256, 170, 220, 170)
    body = union(rrect((170, 230, 342, 462), 56), rect((226, 140, 286, 260)))
    c.put(body, ((150, 190, 130), (60, 110, 80)))
    c.put(inter(body, rect((0, 330, S, S))), sh(ORANGE), ow=0)
    c.ink(rect((170, 324, 342, 334)))
    c.put(poly([(214, 124), (298, 120), (306, 160), (210, 168)]), sh(TAN), ow=12)
    c.gloss(body, 60)


@emoji("nail_bat", "Zombies and survival", "Baseball bat studded with nails")
def _(c):
    bat = union(poly([(230, 450), (282, 450), (310, 110), (256, 40), (202, 110)]),
                circle(256, 450, 30))
    nails = M()
    for y, side in ((110, 1), (160, -1), (210, 1), (250, -1)):
        x = 256 + side * 30
        nails = union(nails, stroke([(x, y), (x + side * 90, y - 24)], 18))
    nails, bat = rot(nails, 35), rot(bat, 35)
    c.put(nails, sh(SILVER), ow=8)
    c.put(bat, sh(WOOD))
    c.put(inter(bat, rot(rect((0, 380, S, 420)), 35)), DWOOD, ow=0)


@emoji("barricade", "Zombies and survival", "Hazard-striped barricade")
def _(c):
    for x0, x1 in ((110, 150), (362, 402)):
        c.put(poly([(x0, 160), (x1, 160), (x1 + 50, 450), (x0 - 50, 450)]), sh(WOOD))
    board = rrect((40, 170, 472, 290), 16)
    c.put(board, sh(YELLOW))
    stripes = M()
    for x in range(-100, 600, 110):
        stripes = union(stripes, poly([(x, 170), (x + 55, 170), (x - 65, 290), (x - 120, 290)]))
    c.ink(inter(board, stripes))
    c.put(rrect((40, 330, 472, 390), 12), sh(WOOD))
    for x in (80, 432):
        c.put(circle(x, 360, 9), STEEL, ow=4)


@emoji("barbed_wire", "Zombies and survival", "Barbed wire")
def _(c):
    c.put(rrect((40, 40, 472, 472), 70), sh(CHAR))
    a = [(x, 256 + 40 * math.sin(x / 52)) for x in range(40, 480, 8)]
    b = [(x, 256 - 40 * math.sin(x / 52)) for x in range(40, 480, 8)]
    c.put(stroke(a, 18), sh(SILVER), ow=8)
    c.put(stroke(b, 18), sh(SILVER), ow=8)
    for x in (104, 202, 300, 398):
        c.put(x_mask(x, 256, 76, 14), SILVER, ow=8)


@emoji("sandbags", "Zombies and survival", "Sandbag wall")
def _(c):
    rows = [(430, (60, 210, 360)), (338, (134, 284)), (246, (210,))]
    for y, xs in rows:
        for x in xs:
            bag = rrect((x - 10, y - 50, x + 150, y + 40), 44)
            c.put(bag, sh(TAN)).gloss(bag, 40)
            c.ink(stroke([(x + 40, y - 34), (x + 40, y + 24)], 8))


@emoji("watchtower", "Zombies and survival", "Watchtower lookout")
def _(c):
    c.put(union(stroke([(180, 250), (130, 470)], 26), stroke([(332, 250), (382, 470)], 26)), sh(WOOD))
    c.put(union(stroke([(160, 320), (352, 420)], 14), stroke([(352, 320), (160, 420)], 14)), sh(WOOD), ow=10)
    c.put(rect((140, 230, 372, 270)), sh(DWOOD))
    c.put(rect((160, 130, 352, 236)), sh(WOOD))
    c.put(rect((190, 150, 322, 210)), (40, 46, 60), ow=10)
    c.put(poly([(256, 40), (390, 140), (122, 140)]), sh(RED))
    c.put(circle(286, 180, 16), YELLOW, ow=0)


@emoji("walkie_talkie", "Zombies and survival", "Walkie-talkie")
def _(c):
    c.put(rrect((300, 36, 336, 190), 14), sh(CHAR))
    body = rrect((160, 150, 352, 470), 38)
    c.put(body, sh(DSTEEL)).gloss(body, 40)
    c.put(rrect((192, 186, 320, 270), 14), sh(TOXIC), ow=12)
    for y in (310, 346, 382, 418):
        c.ink(rrect((196, y - 8, 316, y + 8), 8))
    c.put(rrect((136, 210, 166, 290), 8), sh(ORANGE), ow=10)


@emoji("radio_tower", "Zombies and survival", "Radio tower broadcasting")
def _(c):
    legs = union(stroke([(256, 150), (186, 460)], 24), stroke([(256, 150), (326, 460)], 24),
                 stroke([(226, 290), (286, 290)], 16), stroke([(206, 380), (306, 380)], 16),
                 stroke([(226, 290), (306, 380)], 12), stroke([(286, 290), (206, 380)], 12))
    c.put(legs, sh(STEEL), ow=12)
    c.put(circle(256, 130, 32), sh(RED), ow=14)
    for r in (90, 150):
        c.put(arc((256 - r, 130 - r, 256 + r, 130 + r), 200, 250, 24), CYAN, ow=10)
        c.put(arc((256 - r, 130 - r, 256 + r, 130 + r), 290, 340, 24), CYAN, ow=10)


@emoji("supply_crate", "Zombies and survival", "Military supply crate")
def _(c):
    crate(c, OLIVE)
    c.put(poly(star_pts(256, 292, 70, 30)), BONE, ow=10)


@emoji("airdrop", "Zombies and survival", "Supply airdrop under a parachute")
def _(c):
    dome = inter(ellipse((40, 30, 472, 330)), rect((0, 0, S, 180)))
    c.put(dome, sh(RED))
    for x0, x1 in ((112, 184), (256 - 36, 256 + 36), (328, 400)):
        c.put(inter(dome, rect((x0, 0, x1, S))), sh(WHITE), ow=0)
    c.put(union(*[stroke([(x, 176), (256 + (x - 256) * 0.25, 330)], 7) for x in (48, 160, 352, 464)]),
          INK[:3], ow=0)
    cr = C()
    crate(cr, WOOD)
    c.stamp(cr, 0.46, 256, 390)


@emoji("fuel_can", "Zombies and survival", "Jerry can of fuel")
def _(c):
    c.put(rot(rrect((326, 40, 372, 140), 10), 30, (349, 120)), sh(STEEL))
    handle = minus(rrect((150, 80, 330, 170), 20),
                   rrect((172, 102, 214, 150), 10), rrect((226, 102, 268, 150), 10))
    c.put(handle, sh(RED))
    body = rrect((126, 140, 386, 466), 36)
    c.put(body, sh(RED)).gloss(body, 45)
    c.put(union(stroke([(170, 200), (342, 410)], 18), stroke([(342, 200), (170, 410)], 18)),
          dark(RED, 0.3), ow=0)


@emoji("medkit", "Zombies and survival", "Field medkit")
def _(c):
    c.put(arc((176, 70, 336, 230), 180, 360, 30), sh(DSTEEL))
    body = rrect((70, 150, 442, 440), 40)
    c.put(body, sh(WHITE, 0, 0.14)).gloss(body, 40)
    c.put(union(rrect((222, 196, 290, 394), 10), rrect((156, 262, 356, 330), 10)), sh(RED), ow=12)


@emoji("gas_mask", "Zombies and survival", "Gas mask")
def _(c):
    c.put(union(stroke([(110, 200), (40, 170)], 24), stroke([(402, 200), (472, 170)], 24)), CHAR, ow=8)
    face = rrect((116, 96, 396, 420), 120)
    c.put(face, sh(DSTEEL)).gloss(face, 35)
    for x in (190, 322):
        c.put(circle(x, 214, 56), ((170, 240, 240), (40, 140, 160)), ow=16)
        c.put(circle(x - 16, 196, 14), WHITE, ow=0)
    c.put(circle(256, 380, 76), sh(STEEL))
    for a in range(0, 360, 45):
        c.ink(rot(rect((252, 316, 260, 344)), a, (256, 380)))
    c.put(circle(256, 380, 30), sh(OLIVE), ow=10)


@emoji("campfire", "Zombies and survival", "Survivor campfire")
def _(c):
    c.put(rot(rrect((70, 380, 442, 440), 28), 14), sh(WOOD))
    c.put(rot(rrect((70, 380, 442, 440), 28), -14), sh(DWOOD))
    flame(c, 256, 400, 270, 360)


@emoji("survivor_camp", "Zombies and survival", "Survivor shelter behind a fence")
def _(c):
    c.put(poly([(256, 70), (450, 330), (62, 330)]), sh(OLIVE))
    c.ink(poly([(256, 160), (316, 330), (196, 330)]))
    for x in range(52, 470, 62):
        c.put(poly([(x, 300), (x + 22, 270), (x + 44, 300), (x + 44, 460), (x, 460)]), sh(WOOD), ow=12)
    c.put(rect((40, 360, 472, 386)), sh(DWOOD), ow=10)


# ---- Resources -----------------------------------------------------------


@emoji("res_food", "Resources", "Food resource token")
def _(c):
    hexbadge(c, (232, 120, 60))
    c.put(union(rect((170, 160, 342, 380)), ellipse((170, 340, 342, 410))), sh(SILVER))
    c.put(rect((170, 220, 342, 330)), sh(RED), ow=0)
    c.put(ellipse((170, 126, 342, 196)), light(SILVER, 0.3), ow=12)
    c.label("FOOD", 256, 276, 70, 140, ow=8)


@emoji("res_wood", "Resources", "Wood resource token")
def _(c):
    hexbadge(c, GREEN)
    for x, y in ((180, 330), (332, 330), (256, 200)):
        c.put(circle(x, y, 76), sh(WOOD))
        c.put(circle(x, y, 50), (236, 200, 140), ow=8)
        c.put(arc((x - 26, y - 26, x + 26, y + 26), 0, 360, 8), DWOOD, ow=0)


@emoji("res_steel", "Resources", "Steel resource token")
def _(c):
    hexbadge(c, BLUE)
    def ingot(cx, cy):
        top = poly([(cx - 70, cy - 30), (cx + 70, cy - 30), (cx + 96, cy + 34), (cx - 96, cy + 34)])
        c.put(top, sh(SILVER, 0.3, 0.3), ow=14)
    ingot(178, 360)
    ingot(334, 360)
    ingot(256, 268)


@emoji("res_oil", "Resources", "Oil resource token")
def _(c):
    hexbadge(c, (220, 160, 40))
    drop = poly(flame_pts(256, 410, 220, 320, m=1.2))
    c.put(drop, ((80, 80, 96), (16, 16, 22)))
    c.put(ellipse((196, 280, 236, 350)), (255, 255, 255, 200), ow=0)


# ---- Trivia --------------------------------------------------------------


@emoji("trivia", "Trivia", "Trivia question bubble")
def _(c):
    bubble = union(rrect((40, 50, 472, 380), 110), poly([(110, 340), (220, 340), (90, 470)]))
    c.put(bubble, sh(PURPLE)).gloss(bubble, 50)
    c.label("?", 256, 216, 250, 200, ow=16)


for _l, _col in (("a", RED), ("b", BLUE), ("c", YELLOW), ("d", GREEN)):
    add(f"answer_{_l}", "Trivia", f"Answer button {_l.upper()}",
        lambda c, l=_l, col=_col: pill(c, col, l.upper(), wide=False))


@emoji("answer_correct", "Trivia", "Correct answer")
def _(c):
    m = rrect((40, 40, 472, 472), 90)
    c.put(m, sh(GREEN)).gloss(m, 50)
    c.put(check_mask(256, 266, 300, 66), WHITE, ow=14)


@emoji("answer_wrong", "Trivia", "Wrong answer")
def _(c):
    m = rrect((40, 40, 472, 472), 90)
    c.put(m, sh(RED)).gloss(m, 50)
    c.put(x_mask(256, 256, 210, 66), WHITE, ow=14)


for _t, _col in (("3", RED), ("2", ORANGE), ("1", YELLOW)):
    add(f"count_{_t}", "Trivia", f"Countdown {_t}", lambda c, t=_t, col=_col: stopwatch(c, col, t))
add("count_go", "Trivia", "Countdown GO", lambda c: stopwatch(c, GREEN, "GO", dark(GREEN, 0.2)))

for _n in (3, 5, 10):
    def _streak(c, n=_n):
        flame(c, 256, 470, 330, 460, inner=False)
        c.label(str(n), 256, 340, 160, 200 if n < 10 else 220, ow=14)
    add(f"streak_{_n}", "Trivia", f"{_n}-answer streak flame", _streak)

for _n in (1, 5, 10):
    def _pts(c, n=_n):
        coin(c, GOLD)
        c.label(f"+{n}", 256, 262, 170, 300, ow=14)
    add(f"pts_{_n}", "Trivia", f"+{_n} points coin", _pts)


@emoji("trivia_champ", "Trivia", "Trivia champion trophy")
def _(c):
    trophy(c)
    c.label("?", 256, 180, 160, 120, fill=PURPLE, ow=12)


# ---- Reports and bot -----------------------------------------------------


@emoji("lz_assistant", "Reports and bot", "LastZ Assistant logo mark")
def _(c):
    m = hexbadge(c, CHAR)
    c.put(poly(ngon(256, 256, 186, 6)), sh((40, 44, 54)), ow=10)
    c.label("Z", 256, 256, 260, 220, fill=TOXIC, ow=16)
    c.put(stroke([(140, 380), (380, 130)], 14), RED, ow=8)


@emoji("lz_bot", "Reports and bot", "The bot's zombie-robot mascot")
def _(c):
    c.put(stroke([(256, 120), (256, 40)], 18), sh(STEEL), ow=12)
    c.put(circle(256, 40, 28), sh(RED), ow=12)
    for x in (64, 448):
        c.put(rrect((x - 34, 220, x + 34, 340), 20), sh(DSTEEL))
    head = rrect((90, 110, 422, 450), 70)
    c.put(head, sh(ZOMBIE)).gloss(head, 40)
    c.put(rrect((130, 180, 382, 300), 50), (40, 44, 54), ow=12)
    c.put(circle(198, 240, 30), TOXIC, ow=0)
    c.put(circle(314, 240, 24), TOXIC, ow=0)
    c.put(rrect((170, 350, 342, 400), 16), BONE, ow=12)
    for x in (213, 256, 299):
        c.ink(rect((x - 4, 350, x + 4, 400)))
    c.ink(stroke([(370, 120), (410, 170)], 8))
    c.ink(stroke([(380, 158), (404, 136)], 6))


@emoji("report", "Reports and bot", "Weekly report document")
def _(c):
    doc(c)
    bar_chart(c, (0.4, 0.7, 0.55, 0.9), (BLUE, GREEN, ORANGE, PURPLE), (140, 200, 372, 410))
    c.ink(rrect((140, 120, 280, 140), 10))


@emoji("csv_file", "Reports and bot", "CSV export file")
def _(c):
    doc(c, (220, 240, 220))
    for y in (150, 200):
        c.ink(rrect((150, y, 320, y + 16), 8))
    c.put(rrect((60, 260, 452, 410), 30), sh(GREEN))
    c.label("CSV", 256, 336, 100, 300, ow=10)


@emoji("ocr_scan", "Reports and bot", "OCR screenshot scan")
def _(c):
    c.put(rrect((140, 40, 372, 472), 40), sh(CHAR))
    c.put(rrect((164, 84, 348, 428), 12), sh((60, 90, 140)), ow=10)
    for y in (130, 180, 230, 330, 380):
        c.put(rrect((188, y, 324 - (y % 3) * 20, y + 18), 8), WHITE, ow=0)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        cx, cy = 256 + sx * 200, 256 + sy * 210
        c.put(stroke([(cx, cy - sy * 70), (cx, cy), (cx - sx * 70, cy)], 26), TOXIC, ow=10)
    c.put(rect((40, 270, 472, 290)), TOXIC, ow=10)


@emoji("ingest", "Reports and bot", "Ingest data into the database")
def _(c):
    database(c, GREEN, (124, 200, 388, 470))
    c.put(arrow_mask(256, 130, 220, 56, angle=180), sh(BLUE), ow=16)


@emoji("database", "Reports and bot", "Alliance database")
def _(c):
    database(c, BLUE)


@emoji("backup", "Reports and bot", "Database backup")
def _(c):
    database(c, BLUE, (60, 70, 324, 440))
    c.put(circle(346, 344, 130), sh(GREEN))
    c.put(arc((256, 254, 436, 434), 200, 470, 30), WHITE, ow=10)
    c.put(poly([(232, 300), (296, 316), (250, 360)]), WHITE, ow=10)


@emoji("export", "Reports and bot", "Export a file")
def _(c):
    doc(c, WHITE, (70, 90, 330, 450))
    for y in (190, 240, 290):
        c.ink(rrect((110, y, 280, y + 16), 8))
    c.put(arrow_mask(380, 180, 230, 56, angle=45), sh(ORANGE), ow=16)


@emoji("gift_code", "Reports and bot", "Gift code ticket")
def _(c):
    t = minus(rrect((30, 130, 482, 382), 26), circle(30, 256, 46), circle(482, 256, 46))
    t = rot(t, -12)
    c.put(t, sh(ORANGE)).gloss(t, 50)
    bars = M()
    x = 150
    for wbar in (8, 16, 8, 24, 8, 8, 16, 24, 8, 16, 8):
        bars = union(bars, rect((x, 200, x + wbar, 312)))
        x += wbar + 10
    c.ink(rot(bars, -12))
    c.ink(rot(union(*[rect((100, y, 110, y + 18)) for y in range(160, 360, 34)]), -12))


@emoji("gift_claimed", "Reports and bot", "Gift code redeemed")
def _(c):
    t = minus(rrect((30, 130, 482, 382), 26), circle(30, 256, 46), circle(482, 256, 46))
    t = rot(t, -12)
    c.put(t, sh(STEEL))
    c.put(circle(330, 300, 150), sh(GREEN))
    c.put(check_mask(330, 304, 160, 42), WHITE, ow=12)


@emoji("gift_reminder", "Reports and bot", "Gift code reminder")
def _(c):
    gift_box(c, PURPLE, GOLD)
    sw = C()
    stopwatch(sw, RED, "!", RED)
    c.stamp(sw, 0.5, 370, 370, ring=12)


@emoji("screenshot", "Reports and bot", "Game screenshot")
def _(c):
    c.put(rot(rrect((40, 120, 472, 392), 50), -8), sh(CHAR))
    scr = rot(rrect((90, 150, 422, 362), 16), -8)
    c.put(scr, ((120, 190, 240), (60, 110, 170)), ow=10)
    c.put(inter(scr, rot(poly([(90, 362), (200, 240), (280, 320), (340, 260), (422, 362)]), -8)), sh(GREEN), ow=0)
    c.put(circle(350, 210, 26), YELLOW, ow=0)


# ---- Tiers ---------------------------------------------------------------


@emoji("tier_free", "Plans", "Free plan badge")
def _(c):
    hexbadge(c, STEEL)
    c.put(stroke([(166, 290), (256, 200), (346, 290)], 50), WHITE, ow=14)


@emoji("tier_alliance", "Plans", "Alliance plan badge")
def _(c):
    hexbadge(c, BLUE)
    for y in (230, 320):
        c.put(stroke([(166, y + 40), (256, y - 50), (346, y + 40)], 46), WHITE, ow=14)


@emoji("tier_command", "Plans", "Command plan badge")
def _(c):
    hexbadge(c, GOLD)
    c.put(poly(star_pts(256, 270, 170, 74)), WHITE, ow=14)


# ---- Text badges ---------------------------------------------------------

for _name, _txt, _col in (
    ("new", "NEW", RED), ("beta", "BETA", PURPLE), ("live", "LIVE", RED),
    ("mvp", "MVP", GOLD), ("gg", "GG", GREEN), ("afk", "AFK", STEEL),
    ("lfg", "LFG", BLUE), ("buff", "BUFF", GREEN), ("nerf", "NERF", RED),
    ("soon", "SOON", ORANGE), ("op", "OP", PURPLE), ("rip", "RIP", CHAR),
    ("ty", "TY", (220, 90, 150)), ("gl", "GL", CYAN), ("wip", "WIP", YELLOW),
    ("ok", "OK", GREEN), ("warn", "WARN", ORANGE), ("err", "ERR", RED),
):
    add(f"tag_{_name}", "Text tags", f"{_txt} tag", lambda c, t=_txt, col=_col: pill(c, col, t))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,32}$")


def render(fn) -> Image.Image:
    c = C()
    fn(c)
    return c.img.resize((OUT, OUT), Image.LANCZOS)


def preview(images, cols=12):
    cell = 72
    names = list(images)
    rows = math.ceil(len(names) / cols)
    sheet = Image.new("RGBA", (cols * cell * 2 + 24, rows * cell + 16), (255, 255, 255, 255))
    dark_bg = Image.new("RGBA", (cols * cell + 8, rows * cell + 16), (49, 51, 56, 255))
    sheet.paste(dark_bg, (0, 0))
    for half, x_off in ((0, 4), (1, cols * cell + 20)):
        for i, n in enumerate(names):
            im = images[n].resize((56, 56), Image.LANCZOS)
            x = x_off + (i % cols) * cell + 8
            y = 8 + (i // cols) * cell + 8
            sheet.alpha_composite(im, (x, y))
    return sheet


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated names to (re)draw")
    args = ap.parse_args()

    names = [e[0] for e in EMOJIS]
    dupes = {n for n in names if names.count(n) > 1}
    bad = [n for n in names if not NAME_RE.match(n)]
    if dupes or bad:
        print(f"bad names: dupes={sorted(dupes)} invalid={bad}", file=sys.stderr)
        return 1

    only = set(args.only.split(",")) if args.only else None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    images = {}
    for name, _cat, _cap, fn in EMOJIS:
        path = OUT_DIR / f"{name}.png"
        if only is None or name in only:
            img = render(fn)
            img.save(path, optimize=True)
            if path.stat().st_size > 256 * 1024:
                print(f"{name}: over 256 KB", file=sys.stderr)
                return 1
        images[name] = Image.open(path).convert("RGBA")

    if only is None:
        for stale in OUT_DIR.glob("*.png"):
            if stale.stem not in images and stale.name != "_preview.png":
                stale.unlink()
    preview(images).save(OUT_DIR / "_preview.png", optimize=True)

    lines = [
        "# Custom emojis",
        "",
        f"{len(EMOJIS)} original 128x128 PNGs for LastZ Assistant, drawn by "
        "`scripts/make_emojis.py` (edit that and rerun; don't hand-edit the PNGs). "
        "Each file name is the emoji name. `_preview.png` is a contact sheet, not an emoji.",
        "",
        "Upload them as **application emojis** (Developer Portal > the app > Emojis; "
        "up to 2000 per app), then use them in messages as `<:name:id>`.",
        "",
    ]
    cats: dict[str, list] = {}
    for name, cat, cap, _fn in EMOJIS:
        cats.setdefault(cat, []).append((name, cap))
    for cat, items in cats.items():
        lines += [f"## {cat} ({len(items)})", "", "| Emoji | Name | What it is |", "|---|---|---|"]
        lines += [f"| ![{n}]({n}.png) | `{n}` | {cap} |" for n, cap in items]
        lines.append("")
    (OUT_DIR / "README.md").write_text("\n".join(lines))
    print(f"wrote {len(images)} emojis to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

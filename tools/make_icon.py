#!/usr/bin/env python3
"""Draw the app icon in the APPLaunch house style.

The built-in launcher icons are bold flat glyphs on a TRANSPARENT background,
with a thick tinted outline, filling roughly 11..89 px of a 100 px square.
APPLaunch shows the centre card at 100x100 and side cards at 80x80 and
resamples with plain bilinear (no pre-filter), so we draw each size natively
on a 100-unit grid at 8x and box-filter down: edges land on whole pixels.

  python tools/make_icon.py [style]      style: classic | blue | letter | neon
  python tools/make_icon.py --sheet OUT  comparison sheet of all styles

  packaging/icon.png   100x100  -> /usr/share/APPLaunch/share/images/czmail.png
  store/icon.png       256x256  (App Store listing)
"""

import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_STYLE = "classic"
SS = 8


class Pen:
    """Draw in 0..100 design units on a supersampled RGBA canvas."""

    def __init__(self, size):
        self.n = size * SS
        self.u = self.n / 100.0
        self.img = Image.new("RGBA", (self.n, self.n), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.img)

    def p(self, v):
        return int(round(v * self.u))

    def pts(self, pts):
        return [(self.p(x), self.p(y)) for x, y in pts]

    def rrect(self, box, r, fill=None, outline=None, width=0):
        self.d.rounded_rectangle([self.p(v) for v in box], radius=self.p(r), fill=fill,
                                 outline=outline, width=self.p(width) if width else 0)

    def poly(self, pts, fill):
        self.d.polygon(self.pts(pts), fill=fill)

    def line(self, pts, fill, width):
        self.d.line(self.pts(pts), fill=fill, width=self.p(width), joint="curve")
        r = width / 2.0  # round caps
        for x, y in (pts[0], pts[-1]):
            self.circle(x, y, r, fill)

    def circle(self, cx, cy, r, fill, outline=None, width=0):
        self.d.ellipse([self.p(cx - r), self.p(cy - r), self.p(cx + r), self.p(cy + r)],
                       fill=fill, outline=outline, width=self.p(width) if width else 0)

    def done(self, size):
        return self.img.resize((size, size), Image.BOX)


def clipped(pen, draw_fn, mask_fn):
    """Draw draw_fn onto a layer and keep only what mask_fn covers."""
    layer = Pen(pen.n // SS)
    draw_fn(layer)
    mask = Pen(pen.n // SS)
    mask_fn(mask)
    pen.img.alpha_composite(Image.composite(layer.img, Image.new("RGBA", layer.img.size),
                                            mask.img.split()[3]))


# ------------------------------------------------------------- styles ----

def style_classic(pen):
    """White envelope, thick blue outline, red unread dot (camera-icon feel)."""
    OUT, BODY, FOLD, DOT, DOT_OUT = (47, 110, 235, 255), (255, 255, 255, 255), \
        (214, 226, 248, 255), (255, 84, 64, 255), (150, 28, 20, 255)
    box = (12, 24, 88, 80)
    pen.rrect(box, 9, fill=OUT)
    pen.rrect((17, 29, 83, 75), 5, fill=BODY)
    clipped(pen, lambda q: (q.poly([(17, 75), (50, 50), (83, 75)], FOLD)),
            lambda q: q.rrect((17, 29, 83, 75), 5, fill=(0, 0, 0, 255)))
    pen.line([(20, 32), (50, 55), (80, 32)], OUT, 6)
    pen.circle(80, 24, 12, DOT_OUT)
    pen.circle(80, 24, 8.5, DOT)


def style_blue(pen):
    """Filled blue envelope like the Files folder, light flap, cyan highlight."""
    DARK, BODY, FLAP, HI, DOT = (22, 72, 190, 255), (45, 112, 235, 255), \
        (96, 160, 255, 255), (150, 235, 255, 255), (255, 200, 40, 255)
    pen.rrect((12, 24, 88, 80), 9, fill=BODY)
    clipped(pen, lambda q: (q.poly([(12, 80), (50, 52), (88, 80)], DARK)),
            lambda q: q.rrect((12, 24, 88, 80), 9, fill=(0, 0, 0, 255)))
    clipped(pen, lambda q: q.poly([(12, 24), (88, 24), (50, 58)], FLAP),
            lambda q: q.rrect((12, 24, 88, 80), 9, fill=(0, 0, 0, 255)))
    pen.line([(20, 30), (50, 54), (80, 30)], HI, 3.5)
    pen.circle(81, 24, 11, (24, 24, 30, 255))
    pen.circle(81, 24, 7.5, DOT)


def style_letter(pen):
    """Open envelope with a letter sliding out - colourful like the Store icon."""
    BACK, LETTER, LINE, POCKET, POCKET_D, OUTL, DOT = (
        (234, 92, 56, 255), (255, 255, 255, 255), (150, 160, 178, 255),
        (255, 132, 74, 255), (214, 74, 40, 255), (60, 26, 18, 255), (40, 200, 120, 255))
    # back of the envelope with the open flap pointing up
    pen.poly([(13, 46), (50, 20), (87, 46), (87, 82), (13, 82)], OUTL)
    pen.poly([(17.5, 47.5), (50, 25), (82.5, 47.5), (82.5, 78), (17.5, 78)], BACK)
    # the letter
    pen.rrect((24, 22, 76, 66), 3, fill=OUTL)
    pen.rrect((27.5, 25.5, 72.5, 66), 2, fill=LETTER)
    for y, x1 in ((33, 66), (40, 66), (47, 58)):
        pen.line([(34, y), (x1, y)], LINE, 3.4)
    # front pocket: two side triangles + bottom V
    pen.poly([(13, 46), (50, 68), (87, 46), (87, 86), (13, 86)], OUTL)
    pen.poly([(17.5, 53), (50, 72.5), (82.5, 53), (82.5, 81.5), (17.5, 81.5)], POCKET)
    pen.poly([(17.5, 81.5), (50, 60), (82.5, 81.5)], POCKET_D)
    pen.poly([(22, 81.5), (50, 64.5), (78, 81.5)], POCKET)
    pen.circle(80, 22, 10.5, OUTL)
    pen.circle(80, 22, 7, DOT)


def style_neon(pen):
    """Dark envelope with a bright outline, matching the Terminal/SSH icons."""
    GLOW, FILL, DOT = (60, 230, 120, 255), (34, 36, 44, 255), (255, 210, 50, 255)
    pen.rrect((12, 24, 88, 80), 8, fill=GLOW)
    pen.rrect((17, 29, 83, 75), 4, fill=FILL)
    pen.line([(21, 33), (50, 56), (79, 33)], GLOW, 5)
    pen.line([(21, 72), (40, 56)], GLOW, 3.5)
    pen.line([(79, 72), (60, 56)], GLOW, 3.5)
    pen.circle(81, 24, 11, FILL)
    pen.circle(81, 24, 7.5, DOT)


STYLES = {"classic": style_classic, "blue": style_blue, "letter": style_letter,
          "neon": style_neon}


def draw(style, size):
    pen = Pen(size)
    STYLES[style](pen)
    return pen.done(size)


def sheet(out_path):
    """Variants at real size next to built-in icons, plus a 3x zoom row."""
    bg = (24, 24, 28, 255)
    builtin_dir = os.environ.get("CZ_BUILTIN_ICONS", "")
    builtins = []
    for name in ("file_100.png", "camera_100.png", "store_100.png", "ssh_100.png"):
        p = os.path.join(builtin_dir, name)
        if builtin_dir and os.path.exists(p):
            builtins.append(Image.open(p).convert("RGBA"))
    names = list(STYLES)
    w = max(len(names) * 3 * 100 + 40, 900)
    out = Image.new("RGBA", (w, 140 + 310 + 130), bg)
    d = ImageDraw.Draw(out)
    x = 20
    for name in names:  # real size: 100 and 80
        out.alpha_composite(draw(name, 100), (x, 30))
        out.alpha_composite(draw(name, 80), (x + 110, 40))
        d.text((x, 8), name.upper(), fill=(220, 220, 230, 255))
        x += 230 if len(names) <= 4 else 200
    y = 150
    for i, name in enumerate(names):  # 3x zoom of the 100 px icon
        out.alpha_composite(draw(name, 100).resize((300, 300), Image.NEAREST),
                            (20 + i * (w - 40) // len(names), y))
    y = 150 + 310
    d.text((20, y), "built-in launcher icons for comparison:", fill=(160, 160, 170, 255))
    for i, im in enumerate(builtins):
        out.alpha_composite(im, (20 + i * 110, y + 20))
    out.save(out_path)
    return out_path


def main(argv):
    if len(argv) >= 2 and argv[0] == "--sheet":
        print(sheet(argv[1]))
        return 0
    style = argv[0] if argv else DEFAULT_STYLE
    if style not in STYLES:
        print("unknown style %r; choose from %s" % (style, ", ".join(STYLES)))
        return 2
    p1 = os.path.join(ROOT, "packaging", "icon.png")
    draw(style, 100).save(p1, optimize=True)
    os.makedirs(os.path.join(ROOT, "store"), exist_ok=True)
    p2 = os.path.join(ROOT, "store", "icon.png")
    draw(style, 256).save(p2, optimize=True)
    print(p1)
    print(p2)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

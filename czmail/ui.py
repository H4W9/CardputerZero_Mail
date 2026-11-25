"""Drawing helpers: screen size, theme, fonts, text measuring and wrapping."""

import os

from PIL import ImageFont

W, H = 320, 170   # built-in screen; set_size() switches to the EXT display (320x240)


def set_size(w, h):
    """Pick the screen size. Call before importing app/display."""
    global W, H
    W, H = w, h

# Theme (RGB). Dark UI: the ST7789V panel looks best with dark backgrounds.
C = {
    "bg": (14, 16, 22),
    "panel": (26, 30, 40),
    "bar": (30, 84, 160),
    "bar_text": (240, 244, 255),
    "text": (228, 232, 240),
    "dim": (132, 140, 158),
    "accent": (90, 176, 255),
    "sel": (44, 60, 92),
    "unread": (255, 196, 64),
    "warn": (255, 108, 88),
    "ok": (104, 208, 128),
    "cursor": (255, 255, 255),
    "field": (36, 40, 52),
    "field_focus": (52, 64, 90),
}

_CANDIDATES = {
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "DejaVuSans.ttf",
    ],
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ],
}

_cache = {}


def font(kind="regular", size=11):
    key = (kind, size)
    f = _cache.get(key)
    if f is not None:
        return f
    paths = list(_CANDIDATES.get(kind, _CANDIDATES["regular"]))
    font_dir = os.environ.get("CZMAIL_FONT_DIR")
    if font_dir:
        name = "DejaVuSans-Bold.ttf" if kind == "bold" else "DejaVuSans.ttf"
        paths.insert(0, os.path.join(font_dir, name))
    for p in paths:
        try:
            f = ImageFont.truetype(p, size)
            break
        except (OSError, IOError):
            continue
    else:
        try:
            f = ImageFont.load_default(size)
        except TypeError:  # Pillow < 10.1
            f = ImageFont.load_default()
    _cache[key] = f
    return f


def text_w(fnt, s):
    return fnt.getlength(s)


def ellipsize(fnt, s, maxw):
    """Cut s so it fits in maxw pixels, adding an ellipsis when shortened."""
    s = s.replace("\n", " ").replace("\r", "").replace("\t", " ")
    if fnt.getlength(s) <= maxw:
        return s
    ell = "\u2026"
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fnt.getlength(s[:mid] + ell) <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo].rstrip() + ell


def wrap_offsets(text, fnt, maxw):
    """Word-wrap text to maxw pixels.

    Returns a list of (start, end) offsets into text, one per visual line.
    Newlines end a line; the newline itself is not part of any line.
    """
    out = []
    n = len(text)
    s = 0
    while True:
        e = text.find("\n", s)
        if e == -1:
            e = n
        _wrap_para(text, s, e, fnt, maxw, out)
        if e >= n:
            break
        s = e + 1
    return out


def _wrap_para(text, s, e, fnt, maxw, out):
    if s == e:
        out.append((s, e))
        return
    while s < e:
        # Fast path: the rest fits.
        if fnt.getlength(text[s:e]) <= maxw:
            out.append((s, e))
            return
        # Binary search the longest prefix that fits.
        lo, hi = s + 1, e
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fnt.getlength(text[s:mid]) <= maxw:
                lo = mid
            else:
                hi = mid - 1
        k = lo
        brk = text.rfind(" ", s, k + 1)
        if brk > s:
            out.append((s, brk))
            s = brk + 1
        else:
            out.append((s, k))
            s = k


def wrap_lines(text, fnt, maxw):
    return [text[a:b] for a, b in wrap_offsets(text, fnt, maxw)]


def draw_bar(d, title, right=""):
    """Top title bar, 16 px tall."""
    d.rectangle([0, 0, W, 15], fill=C["bar"])
    fb = font("bold", 11)
    rw = text_w(font("regular", 10), right) if right else 0
    # The rightmost 16 px are reserved for the busy spinner.
    d.text((4, 1), ellipsize(fb, title, W - rw - 26), font=fb, fill=C["bar_text"])
    if right:
        d.text((W - 16 - rw, 2), right, font=font("regular", 10), fill=C["bar_text"])


def draw_footer(d, hint):
    """Bottom key-hint bar, 13 px tall."""
    d.rectangle([0, H - 13, W, H], fill=C["panel"])
    f = font("regular", 9)
    d.text((4, H - 12), ellipsize(f, hint, W - 8), font=f, fill=C["dim"])

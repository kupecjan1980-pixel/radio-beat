#!/usr/bin/env python3
"""App icon (launcher / taskbar): the BEAT + RADIO part of the station logo, BEAT in white,
the red RADIO band as is, on a dark rounded square. "Classic Rock" is left out: unreadable
at launcher sizes. Writes appicon/local.hanis.RadioBeat-<size>.png (committed; install.sh copies them).
Usage: tools/make_app_icon.py   (needs cairo + ImageMagick; run from the repo root)"""
import math, os, subprocess, tempfile, cairo

APP_ID = "local.hanis.RadioBeat"
SIZES = [16, 22, 24, 32, 48, 64, 96, 128, 256, 512]
M = 1024

tmp = tempfile.mkdtemp()
top = os.path.join(tmp, "top.png")
# logo-hd.png is 1600x1600 RGB on white: BEAT + RADIO are the top 1180 px
subprocess.run(["magick", "logo-hd.png", "-crop", "1600x1180+0+0", "+repage", "-fuzz", "8%", "-trim", "+repage",
                "-alpha", "on", "PNG32:" + top], check=True)
src = cairo.ImageSurface.create_from_png(top)
W, H, st = src.get_width(), src.get_height(), src.get_stride()
px = bytes(src.get_data())

def rgb(x, y):
    i = y * st + x * 4
    return px[i + 2], px[i + 1], px[i]          # BGRA in memory
def red(r, g, b): return r > 150 and g < 110 and b < 110

# the red band: first row (from the middle down) that is mostly red; its x extent
band = next(y for y in range(H // 2, H) if sum(red(*rgb(x, y)) for x in range(0, W, 4)) > W / 4 * 0.5)
cols = [x for x in range(W) if red(*rgb(x, band + 20))]
bx0, bx1 = cols[0], cols[-1]

mark = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
ms = mark.get_stride()
out = bytearray(ms * H)
for y in range(H):
    for x in range(W):
        r, g, b = rgb(x, y)
        o = y * ms + x * 4
        if y >= band - 2 and bx0 - 2 <= x <= bx1 + 2:
            inside = band + 4 <= y < H - 4 and bx0 + 4 <= x <= bx1 - 4
            if red(r, g, b) or (inside and r > 120):   # red band + its white letters (not the white rim)
                out[o:o + 4] = bytes((b, g, r, 255))
        else:
            a = max(0, 255 - (r + g + b) // 3)   # dark = BEAT -> white, anti-aliased by darkness
            a = 0 if a < 40 else min(255, int((a - 40) * 255 / 175))
            out[o:o + 4] = bytes((a, a, a, a))   # premultiplied white
mark.get_data()[:] = out
mark.mark_dirty()

def squircle(cr, x, y, s, n=5.0):
    c, r = x + s / 2, s / 2
    for i in range(361):
        t = math.radians(i)
        ct, sn = math.cos(t), math.sin(t)
        px_ = c + r * math.copysign(abs(ct) ** (2 / n), ct)
        py_ = y + r + r * math.copysign(abs(sn) ** (2 / n), sn)
        (cr.move_to if i == 0 else cr.line_to)(px_, py_)
    cr.close_path()

big = cairo.ImageSurface(cairo.FORMAT_ARGB32, M, M)
cr = cairo.Context(big)
pad = M * 0.06
squircle(cr, pad, pad, M - 2 * pad)
g = cairo.LinearGradient(0, pad, 0, M - pad)
g.add_color_stop_rgb(0, 0.17, 0.17, 0.18); g.add_color_stop_rgb(1, 0.05, 0.05, 0.055)
cr.set_source(g); cr.fill_preserve()
cr.set_source_rgba(1, 1, 1, 0.10); cr.set_line_width(M * 0.006); cr.stroke()
inner = (M - 2 * pad) * 0.74
sc = inner / max(W, H)
cr.save()
cr.translate((M - W * sc) / 2, (M - H * sc) / 2)
cr.scale(sc, sc)
cr.set_source_surface(mark, 0, 0); cr.get_source().set_filter(cairo.FILTER_BEST); cr.paint()
cr.restore()

os.makedirs("appicon", exist_ok=True)
for s in SIZES:
    cur, size = big, M
    while size / 2 >= s:                          # halve step by step: clean downscale
        n = cairo.ImageSurface(cairo.FORMAT_ARGB32, size // 2, size // 2)
        c = cairo.Context(n); c.scale(0.5, 0.5); c.set_source_surface(cur, 0, 0)
        c.get_source().set_filter(cairo.FILTER_GOOD); c.paint()
        cur, size = n, size // 2
    if size != s:
        n = cairo.ImageSurface(cairo.FORMAT_ARGB32, s, s)
        c = cairo.Context(n); c.scale(s / size, s / size); c.set_source_surface(cur, 0, 0)
        c.get_source().set_filter(cairo.FILTER_BEST); c.paint()
        cur = n
    cur.write_to_png(f"appicon/{APP_ID}-{s}.png")
print("band at", band, "x", bx0, bx1, "->", len(SIZES), "icons")

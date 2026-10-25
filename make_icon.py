#!/usr/bin/env python3
"""App icon: the station logo on a rounded white square (readable on dark bars)."""

import math
import sys

import cairo


def rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def make(logo_path: str, out_path: str, size: int) -> None:
    logo = cairo.ImageSurface.create_from_png(logo_path)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    pad = size * 0.04
    rounded(cr, pad, pad, size - 2 * pad, size - 2 * pad, size * 0.2)
    cr.set_source_rgb(1, 1, 1)
    cr.fill_preserve()
    cr.clip()
    inner = size * 0.84
    scale = inner / max(logo.get_width(), logo.get_height())
    cr.translate((size - logo.get_width() * scale) / 2, (size - logo.get_height() * scale) / 2)
    cr.scale(scale, scale)
    cr.set_source_surface(logo, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_BEST)
    cr.paint()
    surf.write_to_png(out_path)


if __name__ == "__main__":
    make(sys.argv[1], sys.argv[2], int(sys.argv[3]))

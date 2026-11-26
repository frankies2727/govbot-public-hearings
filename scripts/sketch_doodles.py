#!/usr/bin/env python3
"""Draw the page's two pencil doodles as still SVG line drawings (site/assets/):

* sketch-ribbon.svg: a wireframe ribbon that twists and pinches in the middle,
  drawn as a mesh of rails and rungs, like a pencil study of a twisted sheet.
* sketch-pencil.svg: a sharpened pencil, lying at an angle.

The page uses them as CSS masks filled with its graphite colour, so they follow
the theme. Re-run after changing a shape: python3 scripts/sketch_doodles.py
"""
import math
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "site" / "assets"
rnd = random.Random(7)  # fixed: the same drawing every run


def poly(pts):
    return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)


def ribbon(w=320, h=130, rails=12, rungs=38):
    """A ribbon along x that turns 1.5 times and pinches to a twist in the middle."""
    def point(u, v):
        theta = u * math.pi * 1.5
        width = 52 * (0.12 + 0.88 * abs(math.cos(math.pi * u)))
        x = 14 + u * (w - 28)
        y0 = h / 2 + math.sin(u * math.pi * 2) * 10
        across, depth = v * width * math.cos(theta), v * width * math.sin(theta)
        return x + 0.30 * depth, y0 + across - 0.18 * depth

    paths = []
    for i in range(rails):                       # lines along the ribbon
        v = -1 + 2 * i / (rails - 1)
        paths.append((poly(point(k / 64, v) for k in range(65)), 1.6 if i in (0, rails - 1) else 0.7))
    for j in range(rungs):                       # lines across it
        u = j / (rungs - 1)
        paths.append((poly(point(u, -1 + 2 * k / 10) for k in range(11)), 0.6))
    # A second, slightly shifted pass along both edges: a pencil goes over its outline twice.
    for v in (-1, 1):
        paths.append((poly((x + rnd.uniform(-.6, .6), y + 1.2) for x, y in (point(k / 40, v) for k in range(41))), 0.6))
    return w, h, paths


def pencil(w=260, h=70):
    """A hexagonal pencil, tip to the left: body facets, the sharpened cone and lead, ferrule and eraser."""
    top, mid, bot = 18, 33, 48
    x0, x1 = 64, 214                  # where the cone meets the body; body ends at the ferrule
    paths = [
        (f"M{x0} {top} L{x1} {top} M{x0} {bot} L{x1} {bot}", 1.6),
        (f"M{x0} {mid - 5} L{x1} {mid - 5} M{x0} {mid + 5} L{x1} {mid + 5}", 0.7),     # facet edges
        (f"M{x0} {top} L22 {mid} L{x0} {bot}", 1.4),                                  # the cone
        (f"M{x0} {top} Q{x0 - 6} {mid - 8} {x0 - 2} {mid - 4} Q{x0 - 8} {mid + 4} {x0} {bot}", 0.8),  # shaved wood edge
        ("M22 33 L36 27.5 L36 38.5 Z", 1.2),                                          # lead
        (f"M{x1} {top - 1} L{x1} {bot + 1} M{x1 + 6} {top - 1} L{x1 + 6} {bot + 1} M{x1 + 12} {top - 1} L{x1 + 12} {bot + 1}", 0.9),
        (f"M{x1 + 12} {top + 1} Q{x1 + 32} {top + 1} {x1 + 32} {mid} Q{x1 + 32} {bot - 1} {x1 + 12} {bot - 1}", 1.5),  # eraser
    ]
    # Shading strokes along the lower facet.
    for k in range(18):
        x = x0 + 8 + k * 8
        paths.append((f"M{x} {bot - 1} L{x + 6} {mid + 6}", 0.5))
    return w, h, paths


def svg(w, h, paths):
    body = "".join(f'<path d="{d}" stroke-width="{sw}"/>' for d, sw in paths)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" fill="none" stroke="#000" '
            f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>\n')


if __name__ == "__main__":
    for name, shape in (("sketch-ribbon.svg", ribbon()), ("sketch-pencil.svg", pencil())):
        (OUT / name).write_text(svg(*shape))
        print(name, (OUT / name).stat().st_size, "bytes")

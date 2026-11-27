#!/usr/bin/env python3
"""Draw the page's pencil art (site/assets/):

* sketch-pencil.svg: a sharpened pencil, lying at an angle: a still line drawing
  the page uses as a CSS mask filled with its graphite colour, so it follows the theme.
* hearing-room-lines.webp: the pencil lines of the coloured hearing-room drawing
  (hearing-room.webp), for the "drawn in real time" animation by 01 Pick a place:
  the lines are drawn first, then the colour fills in. Made with the classic
  pencil-sketch filter: grey, inverted, blurred, then colour-dodged back over the grey.

Re-run after changing a shape or the drawing: python3 scripts/sketch_doodles.py
(needs Pillow and numpy).
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "site" / "assets"


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


def pencil_lines(src, dst):
    """The drawing's pencil lines: dodge the grey image with its blurred negative."""
    import numpy as np
    from PIL import Image, ImageFilter, ImageOps
    grey = ImageOps.grayscale(Image.open(src))
    blur = ImageOps.invert(grey).filter(ImageFilter.GaussianBlur(6))
    g = np.asarray(grey, dtype=np.float32)
    b = np.asarray(blur, dtype=np.float32)
    dodge = np.clip(g * 255.0 / np.maximum(255.0 - b, 1.0), 0, 255)
    # Darken the strokes a touch so they read at a small size, keep the paper white.
    out = 255 - np.clip((255 - dodge) * 1.6, 0, 255)
    Image.fromarray(out.astype(np.uint8)).save(dst, "WEBP", quality=50, method=6)


if __name__ == "__main__":
    (OUT / "sketch-pencil.svg").write_text(svg(*pencil()))
    pencil_lines(OUT / "hearing-room.webp", OUT / "hearing-room-lines.webp")
    for name in ("sketch-pencil.svg", "hearing-room.webp", "hearing-room-lines.webp"):
        print(name, (OUT / name).stat().st_size, "bytes")

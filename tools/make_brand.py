"""Generate the LMR Edit brand images (neon logo, icon and .ico) into static/branding/.

Run from the project folder:  python tools/make_brand.py
Uses the Orbitron font in static/fonts (SIL Open Font License, see OFL-Orbitron.txt).
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "static" / "fonts" / "Orbitron-variable.ttf"
OUT = ROOT / "static" / "branding"

NEON = (0, 180, 255)
CYAN = (0, 229, 255)
WHITE = (240, 250, 255)


def _font(size: int, weight: int = 900) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT), size)
    try:
        font.set_variation_by_axes([weight])
    except (OSError, AttributeError):
        pass  # static font or FreeType without variation support: default weight
    return font


def _glow(layer: Image.Image, radius: int, strength: int = 2) -> Image.Image:
    """Layer + blurred copies of itself underneath = neon glow."""
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    for step in range(strength, 0, -1):
        out.alpha_composite(layer.filter(ImageFilter.GaussianBlur(radius * step)))
    out.alpha_composite(layer)
    return out


def _cut_frame(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], cut: int, color, width: int) -> None:
    x0, y0, x1, y1 = box
    points = [(x0 + cut, y0), (x1, y0), (x1, y1 - cut), (x1 - cut, y1), (x0, y1), (x0, y0 + cut), (x0 + cut, y0)]
    draw.line(points, fill=color, width=width, joint="curve")


def make_logo(scale: int = 4) -> Image.Image:
    """Wide wordmark for the sidebar / header: LMR (neon) + EDIT (white)."""
    w, h = 300 * scale, 72 * scale
    pad = 12 * scale
    lines = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(lines)
    big = _font(44 * scale, 900)
    small = _font(20 * scale, 700)
    draw.text((pad, h // 2), "LMR", font=big, fill=NEON + (255,), anchor="lm")
    lmr_w = draw.textlength("LMR", font=big)
    x = pad + lmr_w + 10 * scale
    draw.line([(x, 16 * scale), (x, h - 16 * scale)], fill=CYAN + (255,), width=2 * scale)
    draw.text((x + 10 * scale, h // 2), "EDIT", font=small, fill=WHITE + (255,), anchor="lm")
    logo = _glow(lines, 4 * scale)
    bbox = logo.getbbox()
    return logo.crop((0, 0, min(w, bbox[2] + pad), h)) if bbox else logo


def make_icon(size: int = 512) -> Image.Image:
    """Square icon: an L inside a cut-corner neon frame, on near-black."""
    icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    base = ImageDraw.Draw(icon)
    margin = size // 16
    base.rounded_rectangle((margin, margin, size - margin, size - margin), radius=size // 8, fill=(13, 13, 15, 255))
    lines = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(lines)
    inner = size // 5
    _cut_frame(draw, (inner, inner, size - inner, size - inner), size // 8, NEON + (255,), size // 40)
    draw.text((size // 2, size // 2 + size // 40), "L", font=_font(int(size * 0.42), 900),
              fill=CYAN + (255,), anchor="mm")
    icon.alpha_composite(_glow(lines, size // 64))
    return icon


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_logo().save(OUT / "lmr_logo.png")
    icon = make_icon()
    icon.save(OUT / "lmr_icon.png")
    icon.save(OUT / "lmr_icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Saved to", OUT)


if __name__ == "__main__":
    main()

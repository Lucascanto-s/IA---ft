"""Caixa de texto 'glacial' (vidro fosco) para as imagens 2 a 4.

Texto sempre em CAIXA ALTA, dentro de uma forma arredondada translúcida com três
tonalidades: cinza no topo, cinza mais escuro no meio e preto embaixo. A imagem é
devolvida em RGBA e depois vira uma camada que o usuário move e redimensiona livremente.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).resolve().parent / "static" / "fonts" / "GlacialIndifference-Bold.ttf"

DEFAULT_TEXT_COLOR = "#000000"  # preto: branco sumiria no fundo branco das imagens 2 a 4


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = str(value).lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (0, 0, 0)

_SUPERSAMPLE = 2  # desenha grande e reduz, para texto nítido
DEFAULT_FONT_PX = 104
MAX_CHARS = 42


@lru_cache(maxsize=16)
def _font(size_px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size_px)


def _text_width(text: str, font: ImageFont.FreeTypeFont, tracking: int) -> int:
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    return round(sum(draw.textlength(ch, font=font) for ch in text)) + tracking * max(0, len(text) - 1)


@lru_cache(maxsize=64)
def render_text_badge(text: str, color: str = DEFAULT_TEXT_COLOR, font_px: int = DEFAULT_FONT_PX) -> Image.Image | None:
    """Só o texto, em CAIXA ALTA, sem fundo (Glacial Indifference). None se ficar vazio."""
    text = " ".join(str(text).upper().split())[:MAX_CHARS].strip()
    if not text:
        return None
    text_color = _hex_to_rgb(color)

    scale = _SUPERSAMPLE
    size = font_px * scale
    font = _font(size)
    tracking = round(size * 0.06)
    ascent, descent = font.getmetrics()

    margin = max(2, round(size * 0.08))  # folga para não cortar a letra na borda
    text_w = _text_width(text, font, tracking)
    canvas_w = text_w + 2 * margin
    canvas_h = ascent + descent + 2 * margin

    badge = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)
    x = margin
    for ch in text:
        draw.text((x, margin), ch, font=font, fill=text_color + (255,))
        x += round(draw.textlength(ch, font=font)) + tracking

    return badge.resize((canvas_w // scale, canvas_h // scale), Image.Resampling.LANCZOS)

from __future__ import annotations

import html
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import streamlit as st

# Colours and fonts live in .streamlit/config.toml (native theming). ui/style.css adds the
# cyberpunk layer (glow, HUD corners, grid background, loader), as requested for LMR Edit.

UI_DIR = Path(__file__).resolve().parent
STATIC_DIR = UI_DIR.parent / "static"
BRAND_DIR = STATIC_DIR / "branding"
LOGO = BRAND_DIR / "lmr_logo.png"
ICON = BRAND_DIR / "lmr_icon.png"

STEPS = (
    ("Buscar SKU", ":material/search:"),
    ("Veículos", ":material/directions_car:"),
    ("Ajustar imagens", ":material/tune:"),
    ("Publicar", ":material/cloud_upload:"),
)


def _css() -> str:  # read every run (tiny file) so edits to style.css show up without a restart
    return (UI_DIR / "style.css").read_text(encoding="utf-8")


def tabler_icon(name: str) -> str:
    """<img> of a Tabler icon served from static/icons (MIT licence).

    st.html strips inline <svg> and data: URIs, so the icon is a static file. The SVGs in
    static/icons are already tinted neon cyan (currentColor replaced), because an SVG inside
    <img> does not inherit the page text colour.
    """
    if not (STATIC_DIR / "icons" / f"{name}.svg").exists():
        return ""
    return f'<img src="app/static/icons/{name}.svg" alt="">'


def apply_theme() -> None:
    """Inject the cyberpunk CSS layer and the brand logo. Call once per run, before any page."""
    st.html(f"<style>{_css()}</style>")
    st.logo(str(LOGO), size="large", icon_image=str(ICON))


def page_header(title: str, subtitle: str = "", icon: str = "scan") -> None:
    """Neon page header: Tabler icon in a cut-corner frame, brand line, title and subtitle."""
    sub = f'<p class="lmr-hero__sub">{html.escape(subtitle)}</p>' if subtitle else ""
    st.html(
        f'<div class="lmr-hero" role="banner">'
        f'<div class="lmr-hero__icon" aria-hidden="true">{tabler_icon(icon)}</div>'
        f'<div><p class="lmr-hero__brand">LMR // EDIT</p>'
        f'<h1 class="lmr-hero__title">{html.escape(title)}</h1>{sub}</div></div>'
    )


@contextmanager
def loading(text: str) -> Iterator[None]:
    """Neon loader shown while the block runs (replaces st.spinner)."""
    slot = st.empty()
    slot.html(
        f'<div class="lmr-loader" role="status" aria-live="polite">'
        f'<div class="lmr-loader__ring"></div>'
        f'<span class="lmr-loader__text">{html.escape(text)}</span></div>'
    )
    try:
        yield
    finally:
        slot.empty()


def flow_steps(active: int = 1) -> None:
    """Progress through the four steps as badges: done (green), current (blue), next (grey)."""
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        for index, (label, icon) in enumerate(STEPS, start=1):
            if index < active:
                st.badge(f"{index}. {label}", icon=":material/check_circle:", color="green")
            elif index == active:
                st.badge(f"{index}. {label}", icon=icon, color="blue")
            else:
                st.badge(f"{index}. {label}", icon=icon, color="gray")
            if index < len(STEPS):
                st.markdown(":gray[:material/chevron_right:]", width="content")

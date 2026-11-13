from __future__ import annotations

import streamlit as st

# Colours, borders and spacing live in .streamlit/config.toml (native theming, no CSS).

STEPS = (
    ("Buscar SKU", ":material/search:"),
    ("Veículos", ":material/directions_car:"),
    ("Ajustar imagens", ":material/tune:"),
    ("Publicar", ":material/cloud_upload:"),
)


def apply_theme() -> None:
    """Kept for compatibility: the theme is configured in .streamlit/config.toml."""


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

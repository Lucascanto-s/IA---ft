from __future__ import annotations

import streamlit as st


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        .block-container { max-width: 1500px; padding-top: 2rem; }
        [data-testid="stMetricValue"] { color: #155e75; }
        .flow-step { border: 1px solid #dbeafe; border-radius: 12px; padding: .8rem; background: #f8fafc; }
        .flow-step strong { display: block; color: #0f172a; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def flow_steps(active: int = 1) -> None:
    columns = st.columns(4)
    labels = ("1. Buscar", "2. Ajustar", "3. Revisar", "4. Publicar")
    for index, (column, label) in enumerate(zip(columns, labels), start=1):
        with column:
            state = "🟢" if index <= active else "⚪"
            st.markdown(f'<div class="flow-step">{state} <strong>{label}</strong></div>', unsafe_allow_html=True)

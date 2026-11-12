from pathlib import Path
import streamlit.components.v1 as components


_component = components.declare_component(
    "layer_editor",
    path=str(Path(__file__).parent),
)


def layer_editor(
    layers: list[dict],
    canvas_size: tuple[int, int] = (1200, 1200),
    guides_default: dict | None = None,
    key: str | None = None,
) -> dict | None:
    return _component(
        layers=layers,
        canvas_width=canvas_size[0],
        canvas_height=canvas_size[1],
        guides=guides_default or {"vertical": [], "horizontal": []},
        default={"layers": layers, "guides": guides_default or {"vertical": [], "horizontal": []}},
        key=key,
    )

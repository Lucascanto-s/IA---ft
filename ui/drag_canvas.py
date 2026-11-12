"""Canvas where the part (and the cars) can be grabbed with the mouse and moved.

Drag a layer to move it, use the mouse wheel over it to zoom. Arrow keys move the
selected layer (Shift = 20px) and +/- zoom, for keyboard users.
"""
import base64
import io
from collections.abc import Callable

import streamlit as st
from PIL import Image

CANVAS = 1200

_HTML = """
<div class="stage" role="application" aria-label="Arraste a peça ou o carro com o mouse">
  <div class="guide guide-v"></div>
  <div class="guide guide-h"></div>
</div>
<p class="hint">Clique e arraste para mover · roda do mouse para zoom · setas para ajuste fino</p>
"""

_CSS = """
.stage {
  position: relative;
  width: 100%;
  aspect-ratio: 1 / 1;
  background: #ffffff;
  border: 4px solid #000000;
  box-sizing: border-box;
  overflow: hidden;
  overflow: clip; /* clip cannot be scrolled when a layer sticks out and gets focus */
  user-select: none;
  touch-action: none;
}
.layer {
  position: absolute;
  cursor: grab;
  outline: 2px dashed transparent;
  outline-offset: 2px;
  -webkit-user-drag: none;
}
.layer:hover { outline-color: rgba(0, 120, 212, 0.45); }
.layer.active, .layer:focus-visible { outline-color: #0078d4; }
.layer.dragging { cursor: grabbing; }
.layer.shadow { filter: drop-shadow(0 16px 10px rgba(40, 40, 40, 0.22)); }
.guide {
  position: absolute;
  background: rgba(220, 0, 0, 0.55);
  display: none;
  pointer-events: none;
  z-index: 50;
}
.stage.dragging .guide { display: block; }
.guide-v { left: 50%; top: 0; bottom: 0; width: 1px; }
.guide-h { top: 50%; left: 0; right: 0; height: 1px; }
.hint {
  margin: 6px 0 0;
  font-size: 0.8rem;
  color: var(--st-text-color, #333);
  opacity: 0.7;
  text-align: center;
}
"""

_JS = """
const INSTANCES = new WeakMap()

export default function (component) {
  const { data, parentElement, setStateValue } = component
  const stage = parentElement.querySelector(".stage")
  if (!stage || !data) return
  const size = data.canvas

  let inst = INSTANCES.get(parentElement)
  if (!inst) {
    inst = { imgs: {}, values: {}, specs: {}, dragging: null, timer: null }
    INSTANCES.set(parentElement, inst)
  }

  const clamp = (value, range) => Math.max(range[0], Math.min(range[1], value))

  const place = (id) => {
    const spec = inst.specs[id]
    const v = inst.values[id]
    const img = inst.imgs[id]
    const w = Math.round(spec.base_w * v.scale / 100)
    const h = Math.round(spec.base_h * v.scale / 100)
    const left = Math.floor((size - w) / 2) + v.x
    const top = spec.top === null ? Math.floor((size - h) / 2) + v.y : spec.top + v.y
    img.style.left = (left / size * 100) + "%"
    img.style.top = (top / size * 100) + "%"
    img.style.width = (w / size * 100) + "%"
    img.style.height = (h / size * 100) + "%"
    stage.scrollLeft = 0
    stage.scrollTop = 0
  }

  const emit = () => {
    clearTimeout(inst.timer)
    inst.timer = null
    setStateValue("layout", { ...inst.values, _at: Date.now() })
  }
  const emitSoon = () => {
    clearTimeout(inst.timer)
    inst.timer = setTimeout(emit, 350)
  }

  const select = (id) => {
    for (const [otherId, img] of Object.entries(inst.imgs)) {
      img.classList.toggle("active", otherId === id)
    }
  }

  const seen = new Set()
  for (const spec of data.layers) {
    seen.add(spec.id)
    inst.specs[spec.id] = spec
    let img = inst.imgs[spec.id]
    if (!img) {
      img = document.createElement("img")
      img.className = "layer"
      img.draggable = false
      img.tabIndex = 0
      stage.appendChild(img)
      inst.imgs[spec.id] = img
    }
    if (img.dataset.src !== spec.src) {
      img.src = spec.src
      img.dataset.src = spec.src
    }
    img.alt = spec.label
    img.title = spec.label + " · arraste para mover, roda do mouse para zoom"
    img.style.zIndex = String(spec.z)
    img.classList.toggle("shadow", !!spec.shadow)
    // Python is the source of truth, except for the layer being dragged right now.
    if (inst.dragging !== spec.id && !inst.timer) {
      inst.values[spec.id] = { x: spec.x, y: spec.y, scale: spec.scale }
    }
    place(spec.id)

    const id = spec.id
    img.onpointerdown = (event) => {
      event.preventDefault()
      img.setPointerCapture(event.pointerId)
      img.focus({ preventScroll: true })
      select(id)
      inst.dragging = id
      img.classList.add("dragging")
      stage.classList.add("dragging")
      const start = { ...inst.values[id] }
      const rect = stage.getBoundingClientRect()
      const ratio = size / rect.width
      const startX = event.clientX
      const startY = event.clientY
      img.onpointermove = (move) => {
        const s = inst.specs[id]
        inst.values[id].x = clamp(Math.round(start.x + (move.clientX - startX) * ratio), s.x_range)
        inst.values[id].y = clamp(Math.round(start.y + (move.clientY - startY) * ratio), s.y_range)
        place(id)
      }
      img.onpointerup = img.onpointercancel = () => {
        img.onpointermove = null
        img.classList.remove("dragging")
        stage.classList.remove("dragging")
        inst.dragging = null
        const moved = inst.values[id].x !== start.x || inst.values[id].y !== start.y
        if (moved) emit()
      }
    }
    img.onwheel = (event) => {
      event.preventDefault()
      select(id)
      const step = event.deltaY < 0 ? 5 : -5
      inst.values[id].scale = clamp(inst.values[id].scale + step, inst.specs[id].scale_range)
      place(id)
      emitSoon()
    }
    img.onkeydown = (event) => {
      const s = inst.specs[id]
      const v = inst.values[id]
      const delta = event.shiftKey ? 20 : 2
      if (event.key === "ArrowLeft") v.x = clamp(v.x - delta, s.x_range)
      else if (event.key === "ArrowRight") v.x = clamp(v.x + delta, s.x_range)
      else if (event.key === "ArrowUp") v.y = clamp(v.y - delta, s.y_range)
      else if (event.key === "ArrowDown") v.y = clamp(v.y + delta, s.y_range)
      else if (event.key === "+" || event.key === "=") v.scale = clamp(v.scale + 5, s.scale_range)
      else if (event.key === "-") v.scale = clamp(v.scale - 5, s.scale_range)
      else return
      event.preventDefault()
      place(id)
      emitSoon()
    }
  }
  for (const id of Object.keys(inst.imgs)) {
    if (!seen.has(id)) {
      inst.imgs[id].remove()
      delete inst.imgs[id]
      delete inst.specs[id]
      delete inst.values[id]
    }
  }

  return () => {
    clearTimeout(inst.timer)
    INSTANCES.delete(parentElement)
  }
}
"""

_DRAG_CANVAS = st.components.v2.component(
    "autopecas_drag_canvas",
    html=_HTML,
    css=_CSS,
    js=_JS,
)


def image_data_url(image: Image.Image, max_side: int = 900) -> str:
    """Light WebP copy of a layer for the browser (the final file is rendered in Python)."""
    preview = image.convert("RGBA")
    preview.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    preview.save(buffer, format="WEBP", quality=88)
    return "data:image/webp;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _clamp(value, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return low


def drag_canvas(layers: list[dict], prefixes: dict[str, str], *, key: str) -> None:
    """Mount the canvas.

    Each layer: id, label, src, base_w, base_h (size at zoom 100), top (None = centred
    vertically), z, shadow, x_range, y_range, scale_range. Positions are read from and
    written to the slider keys f"{prefix}_x", f"{prefix}_y", f"{prefix}_scale".
    """
    def _apply_layout() -> None:
        state = st.session_state.get(key)
        layout = getattr(state, "layout", None)
        if layout is None and isinstance(state, dict):
            layout = state.get("layout")
        if not isinstance(layout, dict):
            return
        for layer in layers:
            values = layout.get(layer["id"])
            prefix = prefixes.get(layer["id"])
            if not isinstance(values, dict) or not prefix:
                continue
            st.session_state[f"{prefix}_x"] = _clamp(values.get("x"), *layer["x_range"])
            st.session_state[f"{prefix}_y"] = _clamp(values.get("y"), *layer["y_range"])
            st.session_state[f"{prefix}_scale"] = _clamp(values.get("scale"), *layer["scale_range"])

    mounted = []
    for layer in layers:
        prefix = prefixes[layer["id"]]
        mounted.append({
            **layer,
            "x": int(st.session_state.get(f"{prefix}_x", 0)),
            "y": int(st.session_state.get(f"{prefix}_y", 0)),
            "scale": int(st.session_state.get(f"{prefix}_scale", 100)),
        })
    _DRAG_CANVAS(
        key=key,
        data={"canvas": CANVAS, "layers": mounted},
        on_layout_change=_apply_layout,
        width="stretch",
    )

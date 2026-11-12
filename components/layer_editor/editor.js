const Streamlit = {
  RENDER_EVENT: "streamlit:render",
  events: new EventTarget(),
  setComponentReady() {
    window.addEventListener("message", (event) => {
      if (!event.data || !event.data.isStreamlitMessage) return;
      if (event.data.type === this.RENDER_EVENT) {
        this.events.dispatchEvent(new CustomEvent(this.RENDER_EVENT, {
          detail: { args: event.data.args || {} },
        }));
      }
    });
    window.parent.postMessage({
      isStreamlitMessage: true,
      type: "streamlit:componentReady",
      apiVersion: 1,
    }, "*");
  },
  setComponentValue(value) {
    window.parent.postMessage({
      isStreamlitMessage: true,
      type: "streamlit:setComponentValue",
      value,
      dataType: "json",
    }, "*");
  },
  setFrameHeight(height) {
    window.parent.postMessage({
      isStreamlitMessage: true,
      type: "streamlit:setFrameHeight",
      height,
    }, "*");
  },
};

const state = {
  width: 1200,
  height: 1200,
  layers: [],
  guides: { vertical: [], horizontal: [] },
  selected: null,
  scale: 0.55,
  snap: true,
};

const stageElement = document.getElementById("stage");
const stageWrap = document.getElementById("stage-wrap");
const topRuler = document.getElementById("top-ruler");
const leftRuler = document.getElementById("left-ruler");
let stage;
let layerGroup;
let transformer;
let sendTimer;

function coordinates() {
  return state.layers.map((layer) => ({
    id: layer.id,
    x: Math.round(layer.x),
    y: Math.round(layer.y),
    width: Math.round(layer.width),
    height: Math.round(layer.height),
  }));
}

function send() {
  clearTimeout(sendTimer);
  sendTimer = setTimeout(() => Streamlit.setComponentValue({
    layers: coordinates(),
    guides: state.guides,
    selected: state.selected,
  }), 200);
}

function drawRulers() {
  const width = Math.max(1, Math.round(state.width * state.scale));
  const height = Math.max(1, Math.round(state.height * state.scale));
  topRuler.width = width;
  topRuler.height = 42;
  leftRuler.width = 42;
  leftRuler.height = height;
  const top = topRuler.getContext("2d");
  const left = leftRuler.getContext("2d");
  top.fillStyle = left.fillStyle = "#333";
  top.fillRect(0, 0, width, 42);
  left.fillRect(0, 0, 42, height);
  top.fillStyle = left.fillStyle = "#fff";
  top.font = left.font = "11px sans-serif";
  for (let value = 0; value <= 1200; value += 50) {
    const position = value * state.scale;
    const major = value % 100 === 0;
    top.fillRect(position, 42 - (major ? 16 : 9), 1, major ? 16 : 9);
    left.fillRect(42 - (major ? 16 : 9), position, major ? 16 : 9, 1);
    if (major) {
      top.fillText(String(value), position + 2, 12);
      left.fillText(String(value), 2, position + 12);
    }
  }
}

function applySnap(value, candidates) {
  if (!state.snap) return value;
  const threshold = 8 / state.scale;
  const match = candidates.find((candidate) => Math.abs(candidate - value) <= threshold);
  return match === undefined ? value : match;
}

function redraw() {
  stageElement.replaceChildren();
  stageElement.style.position = "relative";
  state.layers.forEach((layer) => {
    const node = document.createElement("img");
    node.src = layer.image;
    node.alt = layer.id;
    node.draggable = false;
    node.style.position = "absolute";
    node.style.left = `${layer.x * state.scale}px`;
    node.style.top = `${layer.y * state.scale}px`;
    node.style.width = `${layer.width * state.scale}px`;
    node.style.height = `${layer.height * state.scale}px`;
    node.style.maxWidth = "none";
    node.style.cursor = layer.locked ? "default" : "move";
    node.style.userSelect = "none";
    node.style.outline = state.selected === layer.id ? "2px solid #d63384" : "none";
    let start = null;
    node.addEventListener("pointerdown", (event) => {
      if (layer.locked) return;
      event.preventDefault();
      select(layer.id);
      start = { x: event.clientX, y: event.clientY, layerX: layer.x, layerY: layer.y };
      node.setPointerCapture(event.pointerId);
    });
    node.addEventListener("pointermove", (event) => {
      if (!start) return;
      layer.x = Math.round(start.layerX + (event.clientX - start.x) / state.scale);
      layer.y = Math.round(start.layerY + (event.clientY - start.y) / state.scale);
      node.style.left = `${layer.x * state.scale}px`;
      node.style.top = `${layer.y * state.scale}px`;
    });
    node.addEventListener("pointerup", (event) => {
      if (!start) return;
      start = null;
      node.releasePointerCapture(event.pointerId);
      layer.x = applySnap(layer.x, [0, (state.width - layer.width) / 2, state.width - layer.width]);
      layer.y = applySnap(layer.y, [0, (state.height - layer.height) / 2, state.height - layer.height]);
      node.style.left = `${layer.x * state.scale}px`;
      node.style.top = `${layer.y * state.scale}px`;
      send();
    });
    stageElement.appendChild(node);
  });
}

function select(id) {
  state.selected = id;
  stageElement.querySelectorAll("img").forEach((node) => {
    node.style.outline = node.alt === id ? "2px solid #d63384" : "none";
  });
}

function render(event) {
  const args = event.detail.args || {};
  state.width = args.canvas_width || 1200;
  state.height = args.canvas_height || 1200;
  const incomingLayers = args.layers || (args.default && args.default.layers) || [];
  state.layers = incomingLayers.map((layer) => ({...layer}));
  state.guides = args.guides || { vertical: [], horizontal: [] };
  stageElement.style.width = `${state.width * state.scale}px`;
  stageElement.style.height = `${state.height * state.scale}px`;
  redraw();
  drawRulers();
  Streamlit.setFrameHeight(Math.min(760, state.height * state.scale + 58));
}

Streamlit.events.addEventListener(Streamlit.RENDER_EVENT, render);
Streamlit.setComponentReady();
Streamlit.setFrameHeight(680);

// web/static/annotate.js
// Draw rotated quadrilaterals on the frame the camera actually uploaded.
//
// Coordinates are kept in the uploaded image's own pixels. Existing polygons
// are scaled onto it once on load, and the save submits the image's size as
// the camera's reference frame — so annotating a camera whose capture
// resolution drifted corrects the mismatch instead of preserving it.
//
// Corner order is not managed here. The server canonicalises to
// clockwise-from-top-left and returns what it stored; this page adopts that.
import { api, ApiError, fetchBlob } from "./api.js";
import { describe, mountNav, requireSession, toast } from "./ui.js";
import { scalePolygon } from "./polygons.js";

const parts = location.pathname.split("/").filter(Boolean);
const lotId = parts[1];
const cameraId = parts[3];

const canvas = document.getElementById("canvas");
const context = canvas.getContext("2d");
const nodes = {
  add: document.getElementById("add"),
  save: document.getElementById("save"),
  remove: document.getElementById("remove"),
  reload: document.getElementById("reload"),
  label: document.getElementById("label"),
  type: document.getElementById("slot-type"),
  editor: document.getElementById("editor"),
  list: document.getElementById("slot-list"),
  count: document.getElementById("count"),
  hint: document.getElementById("draft-hint"),
  conflict: document.getElementById("conflict"),
  frameWarning: document.getElementById("frame-warning"),
  frameInfo: document.getElementById("frame-info"),
  cameraName: document.getElementById("camera-name"),
  canvasError: document.getElementById("canvas-error"),
};

const HANDLE_RADIUS = 9;
const state = {
  camera: null,
  image: null,
  slots: [],
  selected: -1,
  draft: [],
  drag: null,
  dirty: false,
};

// ----------------------------------------------------------------- geometry
function pointInQuad([x, y], quad) {
  let inside = false;
  for (let i = 0, j = 3; i < 4; j = i++) {
    const [xi, yi] = quad[i];
    const [xj, yj] = quad[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

function handleAt([x, y], quad) {
  for (let index = 0; index < 4; index += 1) {
    const [hx, hy] = quad[index];
    if (Math.hypot(hx - x, hy - y) <= HANDLE_RADIUS) return index;
  }
  return -1;
}

function clamp(value, high) {
  return Math.max(0, Math.min(high, value));
}

function toImage(event) {
  const rect = canvas.getBoundingClientRect();
  return [
    clamp(((event.clientX - rect.left) * canvas.width) / rect.width, canvas.width),
    clamp(((event.clientY - rect.top) * canvas.height) / rect.height, canvas.height),
  ];
}

function nextLabel() {
  let highest = 0;
  let prefix = "A";
  state.slots.forEach((slot) => {
    const match = /^([A-Za-z]+)-(\d+)$/.exec(slot.label);
    if (match) {
      prefix = match[1];
      highest = Math.max(highest, Number(match[2]));
    }
  });
  return `${prefix}-${String(highest + 1).padStart(2, "0")}`;
}

// ------------------------------------------------------------------ drawing
function drawSlot(slot, isSelected) {
  const quad = slot.points;
  context.beginPath();
  context.moveTo(quad[0][0], quad[0][1]);
  quad.slice(1).forEach(([x, y]) => context.lineTo(x, y));
  context.closePath();
  context.fillStyle = isSelected ? "rgba(96,165,250,0.35)" : "rgba(52,211,153,0.20)";
  context.fill();
  context.lineWidth = isSelected ? 3 : 2;
  context.strokeStyle = isSelected ? "#60a5fa" : "#34d399";
  context.stroke();

  const [lx, ly] = quad[0];
  context.font = "600 14px system-ui, sans-serif";
  const width = context.measureText(slot.label).width + 10;
  context.fillStyle = "rgba(15,23,42,0.85)";
  context.fillRect(lx, ly - 20, width, 19);
  context.fillStyle = "#e2e8f0";
  context.fillText(slot.label, lx + 5, ly - 6);

  if (isSelected) {
    quad.forEach(([x, y], index) => {
      context.beginPath();
      context.arc(x, y, HANDLE_RADIUS - 2, 0, Math.PI * 2);
      context.fillStyle = index === 0 ? "#fbbf24" : "#e2e8f0";
      context.fill();
      context.lineWidth = 2;
      context.strokeStyle = "#0f172a";
      context.stroke();
    });
  }
}

function draw() {
  context.drawImage(state.image, 0, 0);
  state.slots.forEach((slot, index) => drawSlot(slot, index === state.selected));

  if (state.draft.length) {
    context.beginPath();
    context.moveTo(state.draft[0][0], state.draft[0][1]);
    state.draft.slice(1).forEach(([x, y]) => context.lineTo(x, y));
    context.strokeStyle = "#fbbf24";
    context.lineWidth = 2;
    context.stroke();
    state.draft.forEach(([x, y]) => {
      context.beginPath();
      context.arc(x, y, 5, 0, Math.PI * 2);
      context.fillStyle = "#fbbf24";
      context.fill();
    });
  }
}

// -------------------------------------------------------------------- state
function markDirty() {
  state.dirty = true;
  nodes.save.disabled = false;
}

function renderList() {
  nodes.count.textContent = String(state.slots.length);
  nodes.list.replaceChildren();
  state.slots.forEach((slot, index) => {
    const item = document.createElement("li");
    item.className = index === state.selected ? "selected" : "";
    item.textContent = `${slot.label}${slot.slot_type === "STANDARD" ? "" : ` · ${slot.slot_type}`}`;
    item.addEventListener("click", () => select(index));
    nodes.list.append(item);
  });
}

function select(index) {
  state.selected = index;
  const slot = state.slots[index];
  nodes.editor.hidden = !slot;
  if (slot) {
    nodes.label.value = slot.label;
    nodes.type.value = slot.slot_type;
  }
  renderList();
  draw();
}

// --------------------------------------------------------------- interaction
canvas.addEventListener("pointerdown", (event) => {
  const point = toImage(event);

  if (state.draft.length < 4 && nodes.add.dataset.active === "1") {
    state.draft.push(point);
    if (state.draft.length === 4) {
      state.slots.push({
        label: nextLabel(),
        slot_type: "STANDARD",
        points: state.draft,
      });
      state.draft = [];
      nodes.add.dataset.active = "0";
      nodes.add.classList.remove("active");
      nodes.hint.hidden = true;
      markDirty();
      select(state.slots.length - 1);
    }
    draw();
    return;
  }

  if (state.selected >= 0) {
    const corner = handleAt(point, state.slots[state.selected].points);
    if (corner >= 0) {
      state.drag = { kind: "corner", corner, from: point };
      canvas.setPointerCapture(event.pointerId);
      return;
    }
  }

  const hit = state.slots.findIndex((slot) => pointInQuad(point, slot.points));
  select(hit);
  if (hit >= 0) {
    state.drag = { kind: "move", from: point };
    canvas.setPointerCapture(event.pointerId);
  }
});

canvas.addEventListener("pointermove", (event) => {
  if (!state.drag) return;
  const point = toImage(event);
  const slot = state.slots[state.selected];
  if (state.drag.kind === "corner") {
    slot.points[state.drag.corner] = point;
  } else {
    const dx = point[0] - state.drag.from[0];
    const dy = point[1] - state.drag.from[1];
    slot.points = slot.points.map(([x, y]) => [
      clamp(x + dx, canvas.width),
      clamp(y + dy, canvas.height),
    ]);
    state.drag.from = point;
  }
  markDirty();
  draw();
});

canvas.addEventListener("pointerup", (event) => {
  if (state.drag) canvas.releasePointerCapture(event.pointerId);
  state.drag = null;
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    state.draft = [];
    nodes.add.dataset.active = "0";
    nodes.add.classList.remove("active");
    nodes.hint.hidden = true;
    select(-1);
  }
  const editing = document.activeElement && document.activeElement.tagName === "INPUT";
  if ((event.key === "Delete" || event.key === "Backspace") && !editing) {
    removeSelected();
  }
});

nodes.add.addEventListener("click", () => {
  const active = nodes.add.dataset.active === "1";
  nodes.add.dataset.active = active ? "0" : "1";
  nodes.add.classList.toggle("active", !active);
  nodes.hint.hidden = active;
  state.draft = [];
  draw();
});

nodes.label.addEventListener("input", () => {
  const slot = state.slots[state.selected];
  if (!slot) return;
  slot.label = nodes.label.value;
  markDirty();
  renderList();
  draw();
});

nodes.type.addEventListener("change", () => {
  const slot = state.slots[state.selected];
  if (!slot) return;
  slot.slot_type = nodes.type.value;
  markDirty();
  renderList();
});

function removeSelected() {
  if (state.selected < 0) return;
  state.slots.splice(state.selected, 1);
  markDirty();
  select(-1);
}

nodes.remove.addEventListener("click", removeSelected);
nodes.reload.addEventListener("click", () => location.reload());

window.addEventListener("beforeunload", (event) => {
  if (!state.dirty) return;
  event.preventDefault();
  event.returnValue = "";
});

// ------------------------------------------------------------------- saving
function adopt(cameraLike) {
  state.camera.slots_version = cameraLike.slots_version;
  state.slots = cameraLike.slots.map((slot) => ({
    label: slot.label,
    slot_type: slot.slot_type,
    points: slot.polygon.map(([x, y]) => [x, y]),
  }));
  state.dirty = false;
  nodes.save.disabled = true;
  select(-1);
}

nodes.save.addEventListener("click", async () => {
  nodes.save.disabled = true;
  nodes.conflict.hidden = true;
  const payload = {
    slots_version: state.camera.slots_version,
    reference_frame: { width: canvas.width, height: canvas.height },
    slots: state.slots.map((slot) => ({
      label: slot.label,
      slot_type: slot.slot_type,
      polygon: slot.points.map(([x, y]) => [
        Math.round(x * 100) / 100,
        Math.round(y * 100) / 100,
      ]),
    })),
  };
  try {
    const result = await api(`/cameras/${cameraId}/slots:bulk`, {
      method: "POST",
      body: payload,
    });
    // The server reordered the corners; adopt its version so the page shows
    // exactly what the worker will crop.
    adopt(result);
    nodes.frameWarning.hidden = true;
    toast(`Saved: ${result.created} added, ${result.updated} changed, ` +
          `${result.deactivated} removed`);
  } catch (error) {
    nodes.save.disabled = false;
    if (error instanceof ApiError && error.code === "slots_version_conflict") {
      nodes.conflict.hidden = false;
      return;
    }
    if (error instanceof ApiError && error.code === "invalid_polygon") {
      const index = state.slots.findIndex((slot) => slot.label === error.detail.label);
      if (index >= 0) select(index);
    }
    toast(describe(error), "error");
  }
});

// ------------------------------------------------------------------ loading
async function load() {
  const me = await requireSession();
  if (!me) return;
  mountNav(me);

  const lot = await api(`/lots/${lotId}/slots`);
  const camera = lot.cameras.find((entry) => entry.id === cameraId);
  if (!camera) {
    nodes.canvasError.textContent = "This camera is not in this lot.";
    nodes.canvasError.hidden = false;
    return;
  }
  state.camera = camera;
  nodes.cameraName.textContent = camera.name;

  let blob;
  try {
    blob = await fetchBlob(`/cameras/${cameraId}/frame`);
  } catch (error) {
    nodes.canvasError.textContent =
      error.code === "frame_not_available"
        ? "This camera has not uploaded a frame yet. Start its worker, then reload."
        : describe(error);
    nodes.canvasError.hidden = false;
    nodes.add.disabled = true;
    return;
  }

  const image = new Image();
  image.src = URL.createObjectURL(blob);
  await image.decode();
  state.image = image;
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;


  const stored = camera.reference_frame;
  const rendered = { width: canvas.width, height: canvas.height };
  state.slots = camera.slots.map((slot) => ({
    label: slot.label,
    slot_type: slot.slot_type,
    points: scalePolygon(slot.polygon, stored, rendered),
  }));


  nodes.frameInfo.textContent = `Frame ${canvas.width}x${canvas.height}`;
  if (stored.width !== rendered.width || stored.height !== rendered.height) {
    nodes.frameWarning.hidden = false;
    nodes.frameWarning.textContent =
      `This camera captures ${rendered.width}x${rendered.height} but its slots ` +
      `were drawn against ${stored.width}x${stored.height}. They have been ` +
      "scaled onto the current frame; saving re-anchors them to it.";
    markDirty();
  }

  renderList();
  draw();
}

load().catch((error) => {
  nodes.canvasError.textContent = describe(error);
  nodes.canvasError.hidden = false;
});
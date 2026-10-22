// web/static/lot.js
// Live lot view.
//
// Geometry comes from GET /lots/{id}/slots once. Statuses come from the SSE
// stream and nowhere else: the stream opens with a snapshot for exactly this
// reason, so there is no race between an initial fetch and the first delta.
//
// Freshness is displayed rather than assumed. A closed stream, an UNKNOWN
// slot and a silent camera each look different from a confident reading —
// v1's failure was showing a dead camera's last vacancy as fact.
import { api, ApiError } from "./api.js";
import { centroid, labelAngle, scalePolygon, toPoints } from "./polygons.js";
import { describe, mountNav, requireSession, toast } from "./ui.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const lotId = location.pathname.split("/").filter(Boolean)[1];
const nodes = {
  name: document.getElementById("lot-name"),
  counts: document.getElementById("counts"),
  views: document.getElementById("views"),
  empty: document.getElementById("empty"),
  conn: document.getElementById("conn"),
  updated: document.getElementById("updated"),
  stale: document.getElementById("stale-banner"),
};

const shapes = new Map();     // slot_id -> { polygon, label, camera }
const slotState = new Map();  // slot_id -> { status, since, at }
let cameras = [];
let stream = null;
let closedReason = null;

// ------------------------------------------------------------------ helpers
function ago(iso) {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function setConnection(kind, text) {
  nodes.conn.className = `conn ${kind}`;
  nodes.conn.textContent = text;
}

// -------------------------------------------------------------- building it
function buildCamera(camera) {
  const frame = camera.reference_frame;
  const section = document.createElement("section");
  section.className = "camera-view";
  section.dataset.cameraId = camera.id;

  const heading = document.createElement("h2");
  heading.textContent = camera.name;
  const meta = document.createElement("span");
  meta.className = "muted small camera-meta";
  heading.append(meta);
  section.append(heading);

  const wrap = document.createElement("div");
  wrap.className = "overlay-wrap";

  const image = document.createElement("img");
  image.alt = `View from ${camera.name}`;
  image.className = "frame";
  // Object URL, not a bare src: the endpoint needs the session cookie and a
  // failed <img> would silently show nothing.
  fetch(`/api/v1/cameras/${camera.id}/frame`, { credentials: "same-origin" })
    .then((response) => (response.ok ? response.blob() : Promise.reject(response)))
    .then((blob) => { image.src = URL.createObjectURL(blob); })
    .catch(() => {
      wrap.classList.add("no-frame");
      image.remove();
    });

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${frame.width} ${frame.height}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.classList.add("overlay");

  camera.slots.forEach((slot) => {
    // Already in this camera's reference frame, and the viewBox is that same
    // frame — so the scale is identity here and the browser handles the rest.
    // Routed through the shared function anyway so there is one code path.
    const polygon = scalePolygon(slot.polygon, frame, frame);
    shapes.set(slot.id, { polygon, label: slot.label, camera: camera.id });

    const group = document.createElementNS(SVG_NS, "g");
    group.classList.add("slot", "status-unknown");
    group.dataset.slotId = slot.id;

    const shape = document.createElementNS(SVG_NS, "polygon");
    shape.setAttribute("points", toPoints(polygon));
    group.append(shape);

    const [cx, cy] = centroid(polygon);
    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("x", cx.toFixed(1));
    text.setAttribute("y", cy.toFixed(1));
    text.setAttribute("transform", `rotate(${labelAngle(polygon).toFixed(1)} ${cx.toFixed(1)} ${cy.toFixed(1)})`);
    text.textContent = slot.label;
    group.append(text);

    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = slot.label;
    group.append(title);

    svg.append(group);
  });

  wrap.append(image, svg);
  section.append(wrap);
  return section;
}

// ------------------------------------------------------------------ painting
function paintSlot(slotId) {
  const group = nodes.views.querySelector(`[data-slot-id="${slotId}"]`);
  if (!group) return;
  const entry = slotState.get(slotId) || { status: "UNKNOWN" };
  group.classList.remove("status-vacant", "status-occupied", "status-unknown");
  group.classList.add(`status-${entry.status.toLowerCase()}`);
  const title = group.querySelector("title");
  const shape = shapes.get(slotId);
  const held = entry.since ? ` for ${ago(entry.since)}` : "";
  title.textContent = entry.status === "UNKNOWN"
    ? `${shape.label} — not currently observed`
    : `${shape.label} — ${entry.status.toLowerCase()}${held}`;
}

function paintSummary() {
  const tally = { VACANT: 0, OCCUPIED: 0, UNKNOWN: 0 };
  shapes.forEach((_, slotId) => {
    const entry = slotState.get(slotId);
    tally[entry ? entry.status : "UNKNOWN"] += 1;
  });
  nodes.counts.replaceChildren();
  [["vacant", tally.VACANT], ["occupied", tally.OCCUPIED],
   ["unknown", tally.UNKNOWN]].forEach(([kind, value]) => {
    const pill = document.createElement("span");
    pill.className = `pill ${kind}`;
    pill.textContent = `${value} ${kind}`;
    nodes.counts.append(pill);
  });

  const blind = [...shapes.keys()].filter(
    (slotId) => (slotState.get(slotId) || {}).status === "UNKNOWN").length;
  if (blind && blind === shapes.size) {
    nodes.stale.hidden = false;
    nodes.stale.textContent =
      "No slot in this lot is currently being observed. Check that the workers " +
      "are running.";
  } else if (blind) {
    nodes.stale.hidden = false;
    nodes.stale.textContent =
      `${blind} of ${shapes.size} slots are not currently observed. Their last ` +
      "known state is deliberately not shown.";
  } else {
    nodes.stale.hidden = true;
  }
}

function paintCameraMeta() {
  cameras.forEach((camera) => {
    const section = nodes.views.querySelector(`[data-camera-id="${camera.id}"]`);
    if (!section) return;
    const meta = section.querySelector(".camera-meta");
    meta.textContent = camera.last_seen_at
      ? `last heartbeat ${ago(camera.last_seen_at)} ago`
      : "never reported";
  });
}

function applySnapshot(data) {
  slotState.clear();
  data.slots.forEach((slot) => slotState.set(slot.slot_id, slot));
  shapes.forEach((_, slotId) => paintSlot(slotId));
  paintSummary();
}

function applyUpdate(data) {
  data.slots.forEach((slot) => {
    slotState.set(slot.slot_id, slot);
    paintSlot(slot.slot_id);
    const group = nodes.views.querySelector(`[data-slot-id="${slot.slot_id}"]`);
    if (group) {
      group.classList.remove("flash");
      void group.offsetWidth;          // restart the CSS animation
      group.classList.add("flash");
    }
  });
  paintSummary();
}

// -------------------------------------------------------------------- stream
function connect() {
  stream = new EventSource(`/api/v1/lots/${lotId}/stream`, { withCredentials: true });

  stream.addEventListener("open", () => {
    closedReason = null;
    setConnection("live", "live");
  });

  stream.addEventListener("snapshot", (event) => {
    applySnapshot(JSON.parse(event.data));
    nodes.updated.textContent = `updated ${new Date().toLocaleTimeString()}`;
  });

  stream.addEventListener("slot_update", (event) => {
    applyUpdate(JSON.parse(event.data));
    nodes.updated.textContent = `updated ${new Date().toLocaleTimeString()}`;
  });

  stream.addEventListener("heartbeat", () => {
    setConnection("live", "live");
    paintCameraMeta();
  });

  stream.addEventListener("closing", (event) => {
    // The server states why. access_revoked must not silently reconnect in a
    // loop; the others are ordinary and EventSource will retry by itself.
    closedReason = JSON.parse(event.data).reason;
    if (closedReason === "access_revoked") {
      stream.close();
      setConnection("stopped", "access ended");
      toast("Your access to this organization ended.", "error");
    }
  });

  stream.addEventListener("error", () => {
    if (stream.readyState === EventSource.CLOSED) {
      setConnection("stopped", "disconnected");
      return;
    }
    setConnection("connecting", "reconnecting…");
  });
}

// ------------------------------------------------------------------- loading
(async () => {
  const me = await requireSession();
  if (!me) return;
  mountNav(me);

  let lot;
  try {
    lot = await api(`/lots/${lotId}/slots`);
  } catch (error) {
    setConnection("stopped", "unavailable");
    toast(describe(error), "error");
    if (error instanceof ApiError && error.status === 404) {
      nodes.name.textContent = "Lot not found";
    }
    return;
  }

  nodes.name.textContent = lot.lot_name;
  cameras = lot.cameras;
  if (!cameras.length) {
    nodes.empty.hidden = false;
    setConnection("stopped", "no cameras");

    return;
  }
document.getElementById("analytics-link").href = `/lots/${lotId}/analytics/`;

  nodes.views.replaceChildren(...cameras.map(buildCamera));
  paintCameraMeta();
  paintSummary();
  connect();

  // Relative times go stale on their own; nothing else here polls.
  setInterval(paintCameraMeta, 15000);
  window.addEventListener("pagehide", () => stream && stream.close());
})();
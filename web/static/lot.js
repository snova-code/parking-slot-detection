// web/static/lot.js
import { api } from "./api.js";
import { describe, mountNav, requireSession, toast } from "./ui.js";

const lotId = location.pathname.split("/").filter(Boolean)[1];
const list = document.getElementById("cameras");

function card(camera) {
  const element = document.createElement("div");
  element.className = "card";
  element.innerHTML = `
    <h2></h2>
    <p class="muted small"></p>
    <p></p>
    <a class="button" href="/lots/${lotId}/cameras/${camera.id}/annotate/">Annotate</a>`;
  element.querySelector("h2").textContent = camera.name;
  element.querySelector(".muted").textContent =
    `${camera.reference_frame.width}x${camera.reference_frame.height} · ` +
    (camera.last_seen_at ? `last seen ${new Date(camera.last_seen_at).toLocaleString()}`
                         : "never seen");
  element.querySelectorAll("p")[1].textContent = `${camera.slots.length} slots`;
  return element;
}

(async () => {
  const me = await requireSession();
  if (!me) return;
  mountNav(me);
  try {
    const lot = await api(`/lots/${lotId}/slots`);
    document.getElementById("lot-name").textContent = lot.lot_name;
    list.replaceChildren();
    if (!lot.cameras.length) {
      list.innerHTML = '<p class="muted">This lot has no active cameras.</p>';
      return;
    }
    lot.cameras.forEach((camera) => list.append(card(camera)));
  } catch (error) {
    toast(describe(error), "error");
  }
})();
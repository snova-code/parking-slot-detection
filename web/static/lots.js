// web/static/lots.js
import { api } from "./api.js";
import { describe, mountNav, requireSession, toast } from "./ui.js";

const container = document.getElementById("lots");

function card(lot) {
  const element = document.createElement("a");
  element.className = "card";
  element.href = `/lots/${lot.id}/`;
  element.innerHTML = `
    <h2></h2>
    <p class="counts">
      <span class="pill vacant">${lot.vacant} vacant</span>
      <span class="pill occupied">${lot.occupied} occupied</span>
      <span class="pill unknown">${lot.unknown} unknown</span>
    </p>
    <p class="muted small">${lot.total_slots} slots · ${lot.timezone}</p>`;
  element.querySelector("h2").textContent = lot.name;   // never innerHTML
  return element;
}

(async () => {
  const me = await requireSession();
  if (!me) return;
  mountNav(me);
  try {
    const lots = await api("/lots");
    container.replaceChildren();
    if (!lots.length) {
      container.innerHTML = '<p class="muted">No lots yet.</p>';
      return;
    }
    lots.forEach((lot) => container.append(card(lot)));
  } catch (error) {
    container.replaceChildren();
    toast(describe(error), "error");
  }
})();
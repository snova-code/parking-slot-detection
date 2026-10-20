// web/static/ui.js
import { api, ApiError } from "./api.js";

export function toast(message, kind = "ok") {
  const element = document.getElementById("toast");
  element.textContent = message;
  element.className = kind;
  element.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 4000);
}

export async function requireSession() {
  try {
    return await api("/auth/me");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      location.href = `/login/?next=${encodeURIComponent(location.pathname)}`;
      return null;
    }
    throw error;
  }
}

export function mountNav(me) {
  const who = document.getElementById("who");
  const active = me.memberships.find((m) => m.org_id === me.active_org_id);
  who.textContent = active ? `${me.phone_number} · ${active.org_name} (${active.role})`
                           : me.phone_number;
  const button = document.getElementById("logout");
  button.hidden = false;
  button.addEventListener("click", async () => {
    await api("/auth/logout", { method: "POST" });
    location.href = "/login/";
  });
}

export function describe(error) {
  if (!(error instanceof ApiError)) return String(error.message || error);
  if (error.code === "insufficient_role") return "Your role is read-only here.";
  if (error.code === "org_selection_required") {
    return "Your account belongs to several organizations; pick one first.";
  }
  return error.message;
}
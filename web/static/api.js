// web/static/api.js
// The only place that talks to /api/v1. CSRF is fetched on demand and read
// fresh from the cookie every time, because logging in rotates it.
const BASE = "/api/v1";

export class ApiError extends Error {
  constructor(status, code, message, detail) {
    super(message || `HTTP ${status}`);
    this.status = status;
    this.code = code || "";
    this.detail = detail || {};
  }
}

function cookie(name) {
  const match = document.cookie.match(new RegExp(`(^|;\\s*)${name}=([^;]*)`));
  return match ? decodeURIComponent(match[2]) : "";
}

async function csrfToken() {
  if (!cookie("csrftoken")) {
    await fetch(`${BASE}/auth/csrf`, { credentials: "same-origin" });
  }
  return cookie("csrftoken");
}

async function fail(response) {
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    /* an empty or non-JSON body still has a status worth reporting */
  }
  const error = payload.error || {};
  return new ApiError(response.status, error.code, error.message, error.detail);
}

export async function api(path, { method = "GET", body } = {}) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRFToken"] = await csrfToken();
    options.body = JSON.stringify(body);
  }
  const response = await fetch(BASE + path, options);
  if (!response.ok) throw await fail(response);
  if (response.status === 204) return null;
  return response.json();
}

export async function fetchBlob(path) {
  const response = await fetch(BASE + path, { credentials: "same-origin" });
  if (!response.ok) throw await fail(response);
  return response.blob();
}
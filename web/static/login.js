// web/static/login.js
import { api, ApiError } from "./api.js";

const form = document.getElementById("login");
const error = document.getElementById("error");

function show(message) {
  error.textContent = message;
  error.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.hidden = true;
  const data = new FormData(form);
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    await api("/auth/login", {
      method: "POST",
      body: {
        phone_number: data.get("phone_number"),
        password: data.get("password"),
      },
    });
    const next = new URLSearchParams(location.search).get("next");
    location.href = next && next.startsWith("/") ? next : "/";
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 422) {
      show("That does not look like a phone number.");
    } else if (caught instanceof ApiError && caught.status === 429) {
      show("Too many attempts. Wait a minute and try again.");
    } else {
      show(caught.message);
    }
    button.disabled = false;
  }
});
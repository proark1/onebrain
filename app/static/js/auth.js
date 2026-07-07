// Login gate: shows the login screen, handles sign-in, and renders the
// "logged in as" bar with logout.

import { login, logout } from "./api.js";
import { qs } from "./dom.js";

export function showLogin() {
  qs(".app").hidden = true;
  const screen = qs("#loginScreen");
  screen.hidden = false;

  const form = qs("#loginForm");
  const email = qs("#loginEmail");
  const password = qs("#loginPassword");
  const error = qs("#loginError");
  const submit = qs("#loginSubmit");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    error.hidden = true;
    submit.disabled = true;
    submit.textContent = "Signing in…";
    try {
      await login(email.value.trim(), password.value);
      location.reload();   // re-enter authenticated
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
      submit.textContent = "Sign in";
    }
  }, { once: false });
}

export function renderUserBar(me) {
  qs("#userName").textContent = me.display_name || me.email;
  const loc = me.location_label && me.location_label !== "—" ? ` · ${me.location_label}` : "";
  qs("#userRole").textContent = `${me.role_label} · ${me.clearance} clearance${loc}`;
  qs("#logoutBtn").addEventListener("click", async () => {
    await logout();
    location.reload();
  });
}

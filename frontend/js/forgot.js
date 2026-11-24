/* Forgot password / choose a new password. With ?token=... in the address it shows the "new password"
   form; otherwise it asks for the email address. */
"use strict";

(function () {
  const $ = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get("token");

  function show(el, text) { el.textContent = text; el.hidden = !text; }

  async function post(path, body) {
    const response = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    let data = {};
    try { data = await response.json(); } catch (_) { /* no body */ }
    if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
    return data;
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (token) {
      $("forgotTitle").textContent = "Choose a new password";
      $("requestForm").hidden = true;
      $("resetForm").hidden = false;
    }

    $("requestForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      show($("forgotError"), "");
      const button = event.currentTarget.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        const result = await post("/api/auth/forgot", { email: event.currentTarget.elements.email.value });
        show($("forgotNotice"), result.email_enabled
          ? "If that email has an account, a reset link is on its way. Check your inbox and spam folder. The link works for one hour."
          : "Password reset by email is not switched on for this store yet. Please contact the store and they will reset it for you.");
        event.currentTarget.hidden = true;
      } catch (error) {
        show($("forgotError"), error.message);
        button.disabled = false;
      }
    });

    $("resetForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      show($("forgotError"), "");
      const form = event.currentTarget;
      if (form.elements.password.value !== form.elements.again.value) {
        show($("forgotError"), "The two passwords do not match.");
        return;
      }
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      try {
        await post("/api/auth/reset", { token, password: form.elements.password.value });
        form.hidden = true;
        show($("forgotNotice"), "Your password was changed. You can sign in with it now.");
        setTimeout(() => { location.href = "login.html"; }, 2500);
      } catch (error) {
        show($("forgotError"), error.message);
        button.disabled = false;
      }
    });
  });
})();

/* Infinity MarketPlace - light / dark theme.
   Load this in <head> WITHOUT defer so the saved theme is applied before the page paints (no flash).
   Order of choice: the visitor's saved choice, then their device setting, then light. */
(function () {
  "use strict";
  var KEY = "infinityTheme";
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try { var v = localStorage.getItem(KEY); return v === "dark" || v === "light" ? v : null; }
    catch (e) { return null; }
  }
  function current() { return saved() || (media && media.matches ? "dark" : "light"); }

  function apply(theme) {
    root.setAttribute("data-theme", theme);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0b1721" : "#102b3f");
    var buttons = document.querySelectorAll(".theme-toggle");
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute("aria-pressed", String(theme === "dark"));
      buttons[i].setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
      buttons[i].title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    }
  }

  function set(theme) {
    try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode: still works for this visit */ }
    apply(theme);
  }

  apply(current());

  function makeButton() {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "theme-toggle";
    b.innerHTML = '<span class="icon-moon" aria-hidden="true">☾</span><span class="icon-sun" aria-hidden="true">☀</span>';
    b.addEventListener("click", function () {
      set(root.getAttribute("data-theme") === "dark" ? "light" : "dark");
    });
    return b;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var spots = document.querySelectorAll("[data-theme-slot]");
    if (!spots.length) {
      var actions = document.querySelector(".header-actions");
      if (actions) actions.insertBefore(makeButton(), actions.firstChild);
    }
    for (var i = 0; i < spots.length; i++) spots[i].appendChild(makeButton());
    apply(current());
  });

  /* Follow the device setting live, unless the visitor chose a theme themselves. */
  if (media && media.addEventListener) {
    media.addEventListener("change", function () { if (!saved()) apply(current()); });
  }
  /* Keep other open tabs (storefront and admin) in step. */
  window.addEventListener("storage", function (e) { if (e.key === KEY) apply(current()); });
})();

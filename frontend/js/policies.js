/* Fills the store name and contact line on the policies page from the store settings. */
"use strict";

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const response = await fetch("/api/config", { credentials: "same-origin" });
    const config = await response.json();
    document.querySelectorAll("[data-store-name]").forEach((el) => { el.textContent = config.store_name; });
    const contact = document.querySelector("[data-contact]");
    if (contact && config.whatsapp_number) {
      const link = document.createElement("a");
      link.className = "text-link";
      link.href = `https://wa.me/${config.whatsapp_number}`;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Message us on WhatsApp";
      contact.replaceChildren(link);
    }
  } catch (_) { /* the page still reads fine without it */ }
});

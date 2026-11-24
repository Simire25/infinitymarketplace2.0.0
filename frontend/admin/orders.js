/* Infinity MarketPlace - Orders Desk.
   A separate page for receiving and organising orders: live board, list, filters, new-order alerts,
   status actions, rider assignment, WhatsApp/phone shortcuts and printable packing slips.
   Hiding a button is only a convenience; the API enforces every permission on the server. */
"use strict";

const $ = (id) => document.getElementById(id);
const money = (n) => new Intl.NumberFormat("en-NG", { style: "currency", currency: "NGN", maximumFractionDigits: 0 }).format(Number(n) || 0);
const label = (s) => String(s || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

/* The server stores UTC ("YYYY-MM-DD HH:MM:SS"). Show everything in the viewer's own time. */
const toDate = (s) => (s ? new Date(String(s).replace(" ", "T") + "Z") : null);
const clock = (s) => { const d = toDate(s); return d ? d.toLocaleTimeString("en-NG", { hour: "2-digit", minute: "2-digit" }) : ""; };
const dayTime = (s) => { const d = toDate(s); return d ? d.toLocaleString("en-NG", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : ""; };
const localDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function ago(s) {
  const d = toDate(s);
  if (!d) return "";
  const mins = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  return `${Math.floor(hrs / 24)} d ago`;
}

/* Board columns. Cash-on-delivery orders start in "Ready to process"; online orders wait in
   "Awaiting payment" until Paystack confirms the payment. */
const COLUMNS = [
  { status: "pending", title: "Awaiting payment", hint: "Online orders not paid yet", tone: "violet" },
  { status: "processing", title: "Ready to process", hint: "Confirm, pack and hand over", tone: "amber" },
  { status: "shipped", title: "Shipped", hint: "Picked up by the rider", tone: "blue" },
  { status: "out_for_delivery", title: "Out for delivery", hint: "On the way to the customer", tone: "blue" },
  { status: "delivered", title: "Delivered", hint: "Completed orders", tone: "green" },
];
const NEXT = {
  processing: { to: "shipped", text: "Mark shipped" },
  shipped: { to: "out_for_delivery", text: "Out for delivery" },
  out_for_delivery: { to: "delivered", text: "Mark delivered" },
};
const TONES = {
  pending: "purple", processing: "amber", shipped: "blue", out_for_delivery: "blue", delivered: "green", cancelled: "red",
  paid: "green", unpaid: "amber", paystack: "blue", cash_on_delivery: "neutral",
};
const PAYLABEL = { paystack: "Online payment", cash_on_delivery: "Cash on delivery" };

const SEEN_KEY = "infinityDeskLastSeenId";
const SOUND_KEY = "infinityDeskSound";
const POLL_MS = 15000;
const FULL_REFRESH_MS = 60000;

const state = {
  user: null, perms: {},
  view: "board",
  orders: [], summary: null,
  lastSeenId: null, seen: new Set(),
  sound: true, pollTimer: null, lastFull: 0, loading: false, unread: 0,
};

/* ---------- helpers ---------- */

async function api(path, { method = "GET", body } = {}) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch (_) { /* no body */ }
  if (!response.ok) {
    const error = new Error((data && data.error) || `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

/* Builds DOM nodes. Text is inserted as text, so data from the server cannot inject HTML. */
function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function toast(message, isError = false) {
  const box = $("toast");
  box.textContent = message;
  box.style.background = isError ? "#7a2219" : "";
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 3600);
}

const badge = (text, tone) => h("span", { class: `badge badge-${tone || "neutral"}` }, text);
const statusBadge = (s) => badge(label(s), TONES[s]);

/* Nigerian numbers are often typed as 0801...; WhatsApp wants the country code without "+". */
function waNumber(phone) {
  const digits = String(phone || "").replace(/\D/g, "");
  if (!digits) return "";
  if (digits.startsWith("234")) return digits;
  if (digits.startsWith("0")) return "234" + digits.slice(1);
  return digits;
}

/* ---------- sound + attention ---------- */

let audio;
function beep() {
  if (!state.sound) return;
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    const t = audio.currentTime;
    [[880, 0], [1175, 0.16]].forEach(([freq, delay]) => {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t + delay);
      gain.gain.exponentialRampToValueAtTime(0.25, t + delay + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + delay + 0.3);
      osc.connect(gain).connect(audio.destination);
      osc.start(t + delay);
      osc.stop(t + delay + 0.32);
    });
  } catch (_) { /* sound is optional */ }
}

const baseTitle = document.title;
function updateTitle() {
  document.title = state.unread > 0 ? `(${state.unread}) New orders · ${baseTitle}` : baseTitle;
}

function setSound(on) {
  state.sound = on;
  try { localStorage.setItem(SOUND_KEY, on ? "1" : "0"); } catch (_) { /* ignore */ }
  const btn = $("soundBtn");
  btn.textContent = on ? "🔔 Sound on" : "🔕 Sound off";
  btn.setAttribute("aria-pressed", String(on));
}

/* ---------- loading data ---------- */

function filters() {
  const params = new URLSearchParams();
  const q = $("fSearch").value.trim();
  if (q) params.set("q", q);
  if ($("fPayment").value) params.set("payment_status", $("fPayment").value);
  if ($("fMethod").value) params.set("payment_method", $("fMethod").value);
  if (state.view === "list" && $("fStatus").value) params.set("status", $("fStatus").value);
  const range = $("fDate").value;
  if (range) {
    // Ask the server for a slightly wider window, then trim to the viewer's own calendar days below.
    const days = range === "today" ? 1 : Number(range);
    const from = new Date(Date.now() - days * 86400000);
    params.set("date_from", from.toISOString().slice(0, 10));
  }
  params.set("limit", "500");
  return params;
}

function inRange(order) {
  const range = $("fDate").value;
  if (!range) return true;
  const d = toDate(order.created_at);
  if (!d) return true;
  if (range === "today") return localDay(d) === localDay(new Date());
  return d.getTime() >= Date.now() - Number(range) * 86400000;
}

async function loadOrders() {
  if (state.loading) return;
  state.loading = true;
  try {
    const rows = await api(`/api/admin/orders?${filters()}`);
    state.orders = rows.filter(inRange);
    $("deskError").hidden = true;
    render();
    state.lastFull = Date.now();
  } catch (error) {
    handleError(error);
  } finally {
    state.loading = false;
  }
}

async function loadSummary() {
  state.summary = await api("/api/admin/orders/summary");
  renderStats();
  return state.summary;
}

function handleError(error) {
  if (error.status === 401 || error.status === 403) {
    stopPolling();
    showGate(error.status === 403 ? "This account cannot open orders." : "Your session ended. Please sign in again.");
    return;
  }
  $("deskError").textContent = error.message;
  $("deskError").hidden = false;
  setLive(false, "Connection problem, retrying…");
}

function setLive(ok, text) {
  $("liveStatus").classList.toggle("offline", !ok);
  $("liveText").textContent = text;
}

/* ---------- new-order detection ---------- */

const isNew = (order) => state.lastSeenId !== null && order.id > state.lastSeenId && !state.seen.has(order.id);

function persistSeen(id) {
  state.lastSeenId = Math.max(state.lastSeenId || 0, id);
  try { localStorage.setItem(SEEN_KEY, String(state.lastSeenId)); } catch (_) { /* ignore */ }
}

function markAllSeen() {
  if (state.summary) persistSeen(state.summary.latest_id);
  state.seen.clear();
  state.unread = 0;
  updateTitle();
  render();
}

function countNew() {
  return state.orders.filter(isNew).length;
}

async function poll() {
  try {
    const before = state.summary;
    const summary = await loadSummary();
    const arrived = before && summary.latest_id > before.latest_id;
    const changed = !before || JSON.stringify(before.counts) !== JSON.stringify(summary.counts)
      || before.unpaid !== summary.unpaid || before.refunds_due !== summary.refunds_due || arrived;
    if (changed || Date.now() - state.lastFull > FULL_REFRESH_MS) await loadOrders();
    if (arrived) {
      const fresh = state.orders.filter(isNew).length;
      state.unread = fresh;
      updateTitle();
      beep();
      toast(fresh === 1 ? "New order received." : `${fresh} new orders received.`);
    }
    setLive(true, `Live · updated ${new Date().toLocaleTimeString("en-NG", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`);
  } catch (error) {
    handleError(error);
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(poll, POLL_MS);
}
function stopPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
}

/* ---------- rendering ---------- */

function render() {
  renderStats();
  const fresh = countNew();
  $("seenBtn").hidden = fresh === 0;
  $("seenBtn").textContent = `Mark ${fresh} as seen`;
  if (state.view === "board") renderBoard();
  else renderList();
}

function stat(title, value, note, tone) {
  return h("div", { class: `stat ${tone ? `stat-${tone}` : ""}` },
    h("small", {}, title), h("strong", {}, value), note ? h("span", {}, note) : null);
}

function renderStats() {
  const s = state.summary;
  if (!s) return;
  const due = s.refunds_due || 0;
  $("refundBar").hidden = due === 0;
  $("refundText").textContent = due === 1
    ? "1 customer paid money that needs to be refunded."
    : `${due} customers paid money that needs to be refunded.`;
  $("stats").replaceChildren(
    stat("Ready to process", s.counts.processing, "Needs action now", s.counts.processing ? "amber" : ""),
    stat("Awaiting payment", s.counts.pending, "Online, unpaid", s.counts.pending ? "violet" : ""),
    stat("On the road", s.counts.shipped + s.counts.out_for_delivery, "Shipped or out for delivery", ""),
    stat("Orders today", s.today.orders, "Not counting cancelled", ""),
    stat("Sales today", money(s.today.revenue), "Order value", "green"));
}

function paymentBadges(o) {
  return h("span", { class: "badges" },
    badge(PAYLABEL[o.payment_method] || label(o.payment_method), TONES[o.payment_method]),
    badge(label(o.payment_status), TONES[o.payment_status]));
}

async function changeStatus(order, to) {
  if (to === "delivered" && order.payment_method === "cash_on_delivery" && order.payment_status !== "paid" && state.perms.payments) {
    const collected = confirm("This is a cash on delivery order. Record the cash as collected too?\n\nOK = mark delivered and cash collected\nCancel = mark delivered only");
    try {
      await api(`/api/admin/orders/${order.id}`, { method: "PATCH", body: { status: to } });
      if (collected) await api(`/api/admin/orders/${order.id}/mark-paid`, { method: "POST", body: {} });
      toast(collected ? "Delivered and cash recorded." : "Order marked delivered.");
    } catch (error) { toast(error.message, true); }
  } else {
    try {
      await api(`/api/admin/orders/${order.id}`, { method: "PATCH", body: { status: to } });
      toast(`Order ${order.reference} is now ${label(to).toLowerCase()}.`);
    } catch (error) { toast(error.message, true); }
  }
  await Promise.all([loadSummary(), loadOrders()]);
}

function orderCard(o) {
  const fresh = isNew(o);
  const next = NEXT[o.status];
  return h("article", { class: `card ${fresh ? "is-new" : ""}`, "data-id": o.id },
    h("button", { class: "card-main", type: "button", onclick: () => openOrder(o.id), "aria-label": `Open order ${o.reference}` },
      h("span", { class: "card-top" },
        h("strong", { class: "ref" }, o.reference),
        fresh ? h("span", { class: "new-tag" }, "NEW") : null,
        h("span", { class: "when", title: dayTime(o.created_at) }, ago(o.created_at))),
      h("span", { class: "who" }, o.customer_name, h("small", {}, o.customer_phone)),
      h("span", { class: "items" }, `${o.item_count} item${o.item_count === 1 ? "" : "s"} · `, o.items_summary || ""),
      h("span", { class: "card-meta" }, h("strong", { class: "price" }, money(o.total)), paymentBadges(o)),
      o.rider_name ? h("span", { class: "rider" }, `🛵 ${o.rider_name}`) : null),
    h("div", { class: "card-actions" },
      next ? h("button", { class: "btn btn-primary btn-small", type: "button", onclick: () => changeStatus(o, next.to) }, next.text) : null,
      h("button", { class: "btn btn-outline-dark btn-small", type: "button", onclick: () => openOrder(o.id) }, "Details")));
}

function renderBoard() {
  const board = $("board");
  board.replaceChildren(...COLUMNS.map((col) => {
    const rows = state.orders.filter(o => o.status === col.status);
    return h("section", { class: `column tone-${col.tone}`, "aria-label": col.title },
      h("header", { class: "column-head" },
        h("div", {}, h("h2", {}, col.title), h("small", {}, col.hint)),
        h("span", { class: "count" }, rows.length)),
      h("div", { class: "column-body" },
        rows.length ? rows.map(orderCard) : h("p", { class: "column-empty" }, "No orders here")));
  }));
  const cancelled = state.orders.filter(o => o.status === "cancelled").length;
  if (cancelled) {
    board.append(h("p", { class: "cancelled-note" },
      `${cancelled} cancelled order${cancelled === 1 ? "" : "s"} in this range. `,
      h("button", { class: "link-plain", type: "button", onclick: () => { $("fStatus").value = "cancelled"; setView("list"); } }, "View them")));
  }
}

function renderList() {
  const rows = state.orders;
  if (!rows.length) {
    $("list").replaceChildren(h("div", { class: "table-wrap" }, h("div", { class: "empty-inline" }, "No orders match these filters.")));
    return;
  }
  $("list").replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
    h("thead", {}, h("tr", {},
      ["Order", "Customer", "Items", "Total", "Payment", "Status", "Placed", ""].map((t, i) => h("th", { class: i === 3 ? "num" : "" }, t)))),
    h("tbody", {}, rows.map(o => h("tr", { class: isNew(o) ? "row-new" : "" },
      h("td", {}, h("button", { class: "link-button ref-link", type: "button", onclick: () => openOrder(o.id) }, o.reference),
        isNew(o) ? h("span", { class: "new-tag" }, "NEW") : null),
      h("td", {}, o.customer_name, h("span", { class: "sub" }, o.customer_phone)),
      h("td", { class: "items-cell" }, o.items_summary || `${o.item_count} items`),
      h("td", { class: "num" }, money(o.total)),
      h("td", {}, paymentBadges(o)),
      h("td", {}, statusBadge(o.status)),
      h("td", {}, dayTime(o.created_at), h("span", { class: "sub" }, ago(o.created_at))),
      h("td", { class: "actions" },
        NEXT[o.status] ? h("button", { class: "btn btn-primary btn-small", type: "button", onclick: () => changeStatus(o, NEXT[o.status].to) }, NEXT[o.status].text) : null,
        h("button", { class: "btn btn-outline-dark btn-small", type: "button", onclick: () => openOrder(o.id) }, "Details")))))
  )));
}

function setView(view) {
  state.view = view;
  $("tabBoard").setAttribute("aria-selected", String(view === "board"));
  $("tabList").setAttribute("aria-selected", String(view === "list"));
  $("board").hidden = view !== "board";
  $("list").hidden = view !== "list";
  $("fStatus").hidden = view !== "list";
  if (view === "board") $("fStatus").value = "";
  loadOrders();
}

/* ---------- order drawer ---------- */

function closeDialog() { $("orderDialog").close(); }

async function openOrder(id) {
  let o;
  try { o = await api(`/api/admin/orders/${id}`); } catch (error) { toast(error.message, true); return; }

  // Opening an order counts as seeing it.
  if (state.lastSeenId !== null && o.id > state.lastSeenId) state.seen.add(o.id);
  state.unread = countNew();
  updateTitle();

  const refresh = async () => { await Promise.all([loadSummary(), loadOrders()]); };
  const dialog = $("orderDialog");
  const next = NEXT[o.status];
  const canCollect = state.perms.payments && o.payment_method === "cash_on_delivery"
    && o.payment_status !== "paid" && o.status !== "cancelled";
  const canCancel = o.status !== "cancelled" && o.status !== "delivered";
  const wa = waNumber(o.customer_phone);
  const waText = `Hello ${o.customer_name}, this is Infinity MarketPlace about your order ${o.reference}.`;

  const act = async (fn, okMessage, close = false) => {
    try { await fn(); toast(okMessage); if (close) closeDialog(); else await openOrder(id); await refresh(); }
    catch (error) { toast(error.message, true); }
  };

  const riderForm = (state.perms.delivery && o.delivery && o.status !== "cancelled" && o.status !== "delivered")
    ? (() => {
        const name = h("input", { type: "text", value: o.delivery.rider_name || "", maxlength: 120, placeholder: "Rider name", "aria-label": "Rider name" });
        const phone = h("input", { type: "tel", value: o.delivery.rider_phone || "", maxlength: 30, placeholder: "Rider phone", "aria-label": "Rider phone" });
        return h("form", { class: "rider-form", onsubmit: (event) => {
          event.preventDefault();
          act(() => api(`/api/admin/deliveries/${id}`, { method: "PATCH", body: {
            rider_name: name.value.trim(), rider_phone: phone.value.trim(),
            ...(o.delivery.status === "unassigned" && name.value.trim() ? { status: "assigned" } : {}) } }), "Rider saved.");
        } }, name, phone, h("button", { class: "btn btn-dark btn-small", type: "submit" }, "Save rider"));
      })()
    : null;

  dialog.replaceChildren(
    h("div", { class: "modal-head" },
      h("div", {}, h("h2", { id: "orderDialogTitle" }, `Order ${o.reference}`),
        h("small", { class: "muted" }, `Placed ${dayTime(o.created_at)} · ${ago(o.created_at)}`)),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: closeDialog }, "×")),
    h("div", { class: "modal-body" },
      h("div", { class: "drawer-status" },
        statusBadge(o.status), badge(PAYLABEL[o.payment_method] || label(o.payment_method), TONES[o.payment_method]),
        badge(label(o.payment_status), TONES[o.payment_status]),
        o.delivery ? badge(`Delivery: ${label(o.delivery.status)}`, "neutral") : null),

      h("div", { class: "drawer-actions" },
        next ? h("button", { class: "btn btn-primary", type: "button", onclick: () => changeThenReopen(o, next.to, id, refresh) }, next.text) : null,
        canCollect ? h("button", { class: "btn btn-dark", type: "button", onclick: () =>
          act(() => api(`/api/admin/orders/${id}/mark-paid`, { method: "POST", body: {} }), "Cash payment recorded.") }, "Mark cash collected") : null,
        h("button", { class: "btn btn-outline-dark", type: "button", onclick: () => printSlip(o) }, "🖨 Print slip"),
        canCancel ? h("button", { class: "btn btn-danger", type: "button", onclick: () => {
          if (!confirm("Cancel this order and return the items to stock?")) return;
          act(() => api(`/api/admin/orders/${id}`, { method: "PATCH", body: { status: "cancelled" } }), "Order cancelled and stock restored.");
        } }, "Cancel order") : null),

      h("div", { class: "detail-block" }, h("h4", {}, "Customer"),
        h("div", { class: "kv" },
          h("div", {}, h("span", {}, "Name"), h("strong", {}, o.customer_name)),
          h("div", {}, h("span", {}, "Phone"), h("strong", {}, o.customer_phone)),
          h("div", {}, h("span", {}, "Email"), h("strong", {}, o.customer_email)),
          h("div", {}, h("span", {}, "Address"), h("strong", {}, o.delivery_address)),
          o.notes ? h("div", {}, h("span", {}, "Notes"), h("strong", {}, o.notes)) : null),
        h("div", { class: "contact-row" },
          h("a", { class: "btn btn-outline-dark btn-small", href: `tel:${o.customer_phone}` }, "📞 Call"),
          wa ? h("a", { class: "btn btn-outline-dark btn-small", target: "_blank", rel: "noopener",
            href: `https://wa.me/${wa}?text=${encodeURIComponent(waText)}` }, "💬 WhatsApp") : null)),

      h("div", { class: "detail-block" }, h("h4", {}, "Items"),
        h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Product"), h("th", { class: "num" }, "Qty"), h("th", { class: "num" }, "Amount"))),
          h("tbody", {}, o.items.map(i => h("tr", {},
            h("td", {}, i.product_name), h("td", { class: "num" }, i.quantity), h("td", { class: "num" }, money(i.line_total)))))))),

      h("div", { class: "detail-block kv" },
        h("div", {}, h("span", {}, "Subtotal"), h("strong", {}, money(o.subtotal))),
        h("div", {}, h("span", {}, "Delivery fee"), h("strong", {}, money(o.delivery_fee))),
        h("div", { class: "grand-line" }, h("span", {}, "Total"), h("strong", {}, money(o.total)))),

      o.delivery ? h("div", { class: "detail-block" }, h("h4", {}, "Delivery"),
        h("div", { class: "kv" },
          h("div", {}, h("span", {}, "Rider"), h("strong", {}, o.delivery.rider_name || "Not assigned")),
          o.delivery.rider_phone ? h("div", {}, h("span", {}, "Rider phone"), h("strong", {}, o.delivery.rider_phone)) : null),
        riderForm) : null,

      o.payments && o.payments.length ? h("div", { class: "detail-block" }, h("h4", {}, "Payments"),
        h("div", { class: "kv" }, o.payments.map(p => h("div", {},
          h("span", {}, `${label(p.provider)} · ${dayTime(p.created_at)}`),
          h("strong", {}, `${money(p.amount)} · ${label(p.status)}`))))) : null));

  if (!dialog.open) dialog.showModal();
  render();
}

async function changeThenReopen(order, to, id, refresh) {
  await changeStatus(order, to);
  if ($("orderDialog").open) await openOrder(id);
}

/* ---------- refunds due ---------- */

async function openRefunds() {
  let issues;
  try { issues = await api("/api/admin/payment-issues"); } catch (error) { toast(error.message, true); return; }
  const dialog = $("orderDialog");
  const canResolve = Boolean(state.perms.payments);
  dialog.replaceChildren(
    h("div", { class: "modal-head" },
      h("div", {}, h("h2", { id: "orderDialogTitle" }, "Refunds due"),
        h("small", { class: "muted" }, "Refund each one in your Paystack dashboard, then mark it done.")),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: closeDialog }, "×")),
    h("div", { class: "modal-body" },
      issues.length ? issues.map(i => h("div", { class: "issue" },
        h("div", { class: "issue-top" }, h("strong", {}, `${i.customer_name} · ${money(i.amount)}`), badge(i.label, "red")),
        h("p", {}, i.note),
        h("div", { class: "kv" },
          h("div", {}, h("span", {}, "Order"), h("strong", {}, i.order_reference)),
          h("div", {}, h("span", {}, "Paystack reference"), h("strong", {}, i.payment_reference || "n/a")),
          h("div", {}, h("span", {}, "Contact"), h("strong", {}, `${i.customer_phone}`))),
        h("div", { class: "contact-row" },
          h("a", { class: "btn btn-outline-dark btn-small", href: `tel:${i.customer_phone}` }, "📞 Call"),
          canResolve ? h("button", { class: "btn btn-dark btn-small", type: "button", onclick: async () => {
            const note = prompt("Note for your records (optional):", "Refunded in Paystack");
            if (note === null) return;
            try {
              await api(`/api/admin/payment-issues/${i.id}/resolve`, { method: "POST", body: { note } });
              toast("Marked as refunded.");
              await loadSummary();
              await openRefunds();
            } catch (error) { toast(error.message, true); }
          } }, "Mark refunded") : h("span", { class: "muted" }, "Only someone with Payments access can mark it done.")))
      ) : h("p", { class: "empty-inline" }, "Nothing to refund. All clear.")));
  if (!dialog.open) dialog.showModal();
}

/* ---------- printable packing slip ---------- */

function printSlip(o) {
  const area = $("printArea");
  area.replaceChildren(
    h("h1", {}, "Infinity MarketPlace"),
    h("h2", {}, `Packing slip · ${o.reference}`),
    h("p", {}, `Placed ${dayTime(o.created_at)}`),
    h("h3", {}, "Deliver to"),
    h("p", {}, o.customer_name, h("br"), o.customer_phone, h("br"), o.delivery_address),
    o.notes ? h("p", {}, h("strong", {}, "Notes: "), o.notes) : null,
    h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Item"), h("th", {}, "Qty"), h("th", {}, "Amount"))),
      h("tbody", {}, o.items.map(i => h("tr", {}, h("td", {}, i.product_name), h("td", {}, i.quantity), h("td", {}, money(i.line_total)))))),
    h("p", { class: "slip-total" }, `Delivery ${money(o.delivery_fee)} · Total ${money(o.total)}`),
    h("p", {}, o.payment_status === "paid" ? "PAID" : `${PAYLABEL[o.payment_method] || o.payment_method}: collect ${money(o.total)}`));
  window.print();
}

/* ---------- sign-in + start ---------- */

function showGate(message) {
  $("desk").hidden = true;
  $("loginGate").hidden = false;
  $("gateError").textContent = message || "";
  $("gateError").hidden = !message;
}

async function start() {
  try {
    const access = await api("/api/admin/access");
    state.user = access.user;
    state.perms = access.permissions;
  } catch (error) {
    showGate(error.status === 403 ? "This account does not have admin access." : "");
    return;
  }
  if (!state.perms.orders) {
    showGate("Your account has not been given access to orders. Ask an administrator.");
    return;
  }
  $("loginGate").hidden = true;
  $("desk").hidden = false;

  try { state.lastSeenId = Number(localStorage.getItem(SEEN_KEY)) || null; } catch (_) { state.lastSeenId = null; }
  await loadSummary().catch(handleError);
  // First visit on this device: treat everything that exists now as already seen.
  if (state.lastSeenId === null && state.summary) persistSeen(state.summary.latest_id);
  await loadOrders();
  state.unread = countNew();
  updateTitle();
  setLive(true, "Live · watching for new orders");
  startPolling();
}

async function onLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/auth/login", { method: "POST", body: { email: form.elements.email.value, password: form.elements.password.value } });
    await start();
  } catch (error) {
    showGate(error.message);
  }
}

async function logout() {
  stopPolling();
  await api("/api/auth/logout", { method: "POST", body: {} }).catch(() => {});
  location.reload();
}

document.addEventListener("DOMContentLoaded", () => {
  try { setSound(localStorage.getItem(SOUND_KEY) !== "0"); } catch (_) { setSound(true); }
  $("deskLoginForm").addEventListener("submit", onLogin);
  $("logoutBtn").addEventListener("click", logout);
  $("soundBtn").addEventListener("click", () => { setSound(!state.sound); if (state.sound) beep(); });
  $("refreshBtn").addEventListener("click", async () => { await Promise.all([loadSummary(), loadOrders()]).catch(handleError); toast("Orders refreshed."); });
  $("seenBtn").addEventListener("click", markAllSeen);
  $("refundBtn").addEventListener("click", openRefunds);
  $("tabBoard").addEventListener("click", () => setView("board"));
  $("tabList").addEventListener("click", () => setView("list"));
  let timer;
  $("fSearch").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(loadOrders, 300); });
  ["fPayment", "fMethod", "fDate", "fStatus"].forEach(id => $(id).addEventListener("change", loadOrders));
  // Refresh as soon as the tab becomes visible again.
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.pollTimer) poll(); });
  // Relative times ("5 min ago") stay fresh without hitting the server.
  setInterval(() => { if (state.orders.length && !$("desk").hidden) render(); }, 60000);
  start();
});

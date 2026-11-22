/* Infinity MarketPlace - admin console.
   Hiding a menu item is only a convenience; the API enforces every permission on the server. */
"use strict";

const $ = (id) => document.getElementById(id);
const money = (n) => new Intl.NumberFormat("en-NG", { style: "currency", currency: "NGN", maximumFractionDigits: 0 }).format(Number(n) || 0);
const when = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "");
const label = (s) => String(s || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

const ORDER_STATUSES = ["pending", "processing", "shipped", "out_for_delivery", "delivered", "cancelled"];
const DELIVERY_STATUSES = ["unassigned", "assigned", "picked_up", "out_for_delivery", "delivered", "failed"];
const PRODUCT_TAGS = [["", "None"], ["Featured", "Featured"], ["New", "New"], ["Popular", "Popular"]];
const TONES = {
  pending: "purple", processing: "amber", shipped: "blue", out_for_delivery: "blue", delivered: "green",
  cancelled: "red", paid: "green", unpaid: "amber", success: "green", failed: "red",
  unassigned: "neutral", assigned: "amber", picked_up: "blue", paystack: "blue",
  cash_on_delivery: "neutral", cash: "neutral", active: "green", archived: "neutral",
};

const state = { user: null, perms: {}, section: null };

/* ---------- API + DOM helpers ---------- */

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
  toast.timer = setTimeout(() => box.classList.remove("show"), 3200);
}

function badge(text, tone) {
  return h("span", { class: `badge badge-${tone || "neutral"}` }, text);
}
const statusBadge = (status) => badge(label(status), TONES[status]);

function select(options, value = "", attrs = {}) {
  return h("select", { ...attrs },
    options.map(([v, text]) => h("option", { value: String(v), selected: String(v) === String(value) }, text)));
}

function searchBox(placeholder, onInput) {
  let timer;
  return h("input", {
    class: "search-input", type: "search", placeholder, "aria-label": placeholder,
    oninput: (event) => {
      clearTimeout(timer);
      timer = setTimeout(() => onInput(event.target.value.trim()), 280);
    },
  });
}

function toolbar(...nodes) {
  return h("div", { class: "toolbar" }, nodes);
}

function kpi(title, value, note, alert = false) {
  return h("div", { class: `kpi ${alert ? "alert" : ""}` },
    h("small", {}, title), h("strong", {}, value), note ? h("span", {}, note) : null);
}

/* columns: [{ label, render(row) | key, num, actions }] */
function table(columns, rows, empty = "Nothing to show yet.") {
  if (!rows.length) return h("div", { class: "table-wrap" }, h("div", { class: "empty-inline" }, empty));
  return h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
    h("thead", {}, h("tr", {}, columns.map(c => h("th", { class: c.num ? "num" : "" }, c.label)))),
    h("tbody", {}, rows.map(row => h("tr", {}, columns.map(c => {
      const value = c.render ? c.render(row) : row[c.key];
      return h("td", { class: [c.num ? "num" : "", c.actions ? "actions" : ""].join(" ") }, value ?? "");
    }))))));
}

/* ---------- modal and forms ---------- */

function openModal(title, body) {
  const modal = $("modal");
  modal.replaceChildren(
    h("div", { class: "modal-head" },
      h("h2", { id: "modalTitle" }, title),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: () => modal.close() }, "×")),
    h("div", { class: "modal-body" }, body));
  if (!modal.open) modal.showModal();
}

function closeModal() {
  $("modal").close();
}

function fieldNode(field, value) {
  const common = { id: `f_${field.name}`, name: field.name };
  let control;
  if (field.type === "select") {
    control = select(field.options, value ?? "", common);
  } else if (field.type === "textarea") {
    control = h("textarea", { ...common, rows: 3, maxlength: field.maxlength }, value ?? "");
  } else if (field.type === "checkbox") {
    control = h("input", { ...common, type: "checkbox", checked: Boolean(value) });
  } else {
    control = h("input", {
      ...common, type: field.type || "text", value: value ?? "", required: field.required,
      min: field.min, step: field.step, maxlength: field.maxlength, placeholder: field.placeholder,
    });
  }
  return h("label", { class: `field-label ${field.full ? "full" : ""}` },
    h("span", {}, field.label + (field.required ? " *" : "")), control,
    field.help ? h("small", { class: "help" }, field.help) : null);
}

function readForm(form, fields) {
  const out = {};
  for (const field of fields) {
    const el = form.elements[field.name];
    if (field.type === "checkbox") out[field.name] = el.checked;
    else if (field.type === "number") out[field.name] = el.value === "" ? null : Number(el.value);
    else out[field.name] = el.value.trim();
  }
  return out;
}

/* Builds a form. onSubmit(values) runs on submit; afterSave decides what happens next. */
function buildForm({ fields, values = {}, submitLabel = "Save", onSubmit, afterSave, inModal = false }) {
  const errorBox = h("p", { class: "form-error", hidden: true });
  const submit = h("button", { class: "btn btn-dark", type: "submit" }, submitLabel);
  const form = h("form", {
    class: "form-grid",
    onsubmit: async (event) => {
      event.preventDefault();
      errorBox.hidden = true;
      submit.disabled = true;
      try {
        await onSubmit(readForm(form, fields));
        if (inModal) closeModal();
        toast("Saved.");
        if (afterSave) afterSave();
        else reloadSection();
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      } finally {
        submit.disabled = false;
      }
    },
  },
    fields.map(f => fieldNode(f, values[f.name])),
    errorBox,
    h("div", { class: "form-actions full" },
      inModal ? h("button", { class: "btn btn-outline-dark", type: "button", onclick: closeModal }, "Cancel") : null,
      submit));
  return form;
}

function formDialog(title, options) {
  openModal(title, buildForm({ ...options, inModal: true }));
}

/* ---------- sections ---------- */

async function renderDashboard(view) {
  const d = await api("/api/admin/dashboard");
  view.replaceChildren(
    h("div", { class: "kpi-grid" },
      kpi("Today's sales", money(d.today.revenue), `${d.today.orders} order(s) today`),
      kpi("Open orders", d.open_orders, "Pending to out for delivery"),
      kpi("Awaiting payment", d.awaiting_payment, "Unpaid and not cancelled", d.awaiting_payment > 0),
      kpi("Low stock items", d.low_stock, "At or below alert level", d.low_stock > 0),
      kpi("Active deliveries", d.active_deliveries, "Assigned or on the road"),
      kpi("Customers", d.customers, "Registered accounts")),
    h("div", { class: "two-col" },
      h("section", { class: "panel" }, h("h3", {}, "Recent orders"), table([
        { label: "Order", render: o => o.reference },
        { label: "Customer", render: o => o.customer_name },
        { label: "Total", num: true, render: o => money(o.total) },
        { label: "Status", render: o => statusBadge(o.status) },
      ], d.recent_orders, "No orders yet.")),
      h("section", { class: "panel" }, h("h3", {}, "Low stock"), table([
        { label: "Product", render: p => p.name },
        { label: "Stock / alert", num: true, render: p => `${p.stock} / ${p.low_stock_threshold}` },
      ], d.low_stock_items, "All products are well stocked."))));
}

async function renderOrders(view) {
  const statusFilter = select([["", "All statuses"], ...ORDER_STATUSES.map(s => [s, label(s)])], "", { class: "filter-select" });
  const body = h("div");
  let search = "";

  const load = async () => {
    const params = new URLSearchParams();
    if (statusFilter.value) params.set("status", statusFilter.value);
    if (search) params.set("q", search);
    const rows = await api(`/api/admin/orders?${params}`);
    body.replaceChildren(table([
      { label: "Order", render: o => h("button", { class: "link-button", type: "button", onclick: () => openOrder(o.id) }, o.reference) },
      { label: "Customer", render: o => h("span", {}, o.customer_name, h("span", { class: "sub" }, o.customer_phone)) },
      { label: "Total", num: true, render: o => money(o.total) },
      { label: "Payment", render: o => h("span", {}, badge(label(o.payment_method), TONES[o.payment_method]), badge(label(o.payment_status), TONES[o.payment_status])) },
      { label: "Status", render: o => select(ORDER_STATUSES.map(s => [s, label(s)]), o.status, {
          class: "filter-select", "aria-label": `Status of ${o.reference}`, disabled: o.status === "cancelled",
          onchange: async (event) => {
            try {
              await api(`/api/admin/orders/${o.id}`, { method: "PATCH", body: { status: event.target.value } });
              toast("Order status updated.");
            } catch (error) {
              toast(error.message, true);
            }
            load();
          } }) },
      { label: "Placed", render: o => when(o.created_at) },
    ], rows, "No orders match these filters."));
  };

  statusFilter.addEventListener("change", load);
  view.replaceChildren(
    h("div", { class: "desk-callout" },
      h("p", {}, h("strong", {}, "Receiving orders all day? "), "The Orders Desk is a live board with new-order alerts, rider assignment and printable slips."),
      h("a", { class: "btn btn-primary btn-small", href: "orders.html" }, "Open Orders Desk")),
    toolbar(statusFilter, searchBox("Search reference, name or phone", (v) => { search = v; load(); })),
    body);
  await load();
}

async function openOrder(id) {
  const o = await api(`/api/admin/orders/${id}`);
  const canCollect = state.perms.payments && o.payment_method === "cash_on_delivery"
    && o.payment_status !== "paid" && o.status !== "cancelled";
  const canCancel = o.status !== "cancelled" && o.status !== "delivered";

  const actions = h("div", { class: "form-actions" },
    canCollect ? h("button", { class: "btn btn-dark", type: "button", onclick: async () => {
      try {
        await api(`/api/admin/orders/${id}/mark-paid`, { method: "POST", body: {} });
        toast("Cash payment recorded.");
        closeModal();
        reloadSection();
      } catch (error) { toast(error.message, true); }
    } }, "Mark cash collected") : null,
    canCancel ? h("button", { class: "btn btn-danger", type: "button", onclick: async () => {
      if (!confirm("Cancel this order and return the items to stock?")) return;
      try {
        await api(`/api/admin/orders/${id}`, { method: "PATCH", body: { status: "cancelled" } });
        toast("Order cancelled and stock restored.");
        closeModal();
        reloadSection();
      } catch (error) { toast(error.message, true); }
    } }, "Cancel order") : null);

  openModal(`Order ${o.reference}`, h("div", {},
    h("div", { class: "detail-block" }, h("h4", {}, "Customer"), h("div", { class: "kv" },
      h("div", {}, h("span", {}, "Name"), h("strong", {}, o.customer_name)),
      h("div", {}, h("span", {}, "Phone"), h("strong", {}, o.customer_phone)),
      h("div", {}, h("span", {}, "Email"), h("strong", {}, o.customer_email)),
      h("div", {}, h("span", {}, "Address"), h("strong", {}, o.delivery_address)),
      o.notes ? h("div", {}, h("span", {}, "Notes"), h("strong", {}, o.notes)) : null)),
    h("div", { class: "detail-block" }, h("h4", {}, "Items"), table([
      { label: "Product", render: i => i.product_name },
      { label: "Qty", num: true, render: i => i.quantity },
      { label: "Amount", num: true, render: i => money(i.line_total) },
    ], o.items)),
    h("div", { class: "detail-block kv" },
      h("div", {}, h("span", {}, "Subtotal"), h("strong", {}, money(o.subtotal))),
      h("div", {}, h("span", {}, "Delivery fee"), h("strong", {}, money(o.delivery_fee))),
      h("div", {}, h("span", {}, "Total"), h("strong", {}, money(o.total)))),
    h("div", { class: "detail-block" }, h("h4", {}, "Status"),
      statusBadge(o.status), badge(label(o.payment_method), TONES[o.payment_method]), badge(label(o.payment_status), TONES[o.payment_status]),
      o.delivery ? badge(`Delivery: ${label(o.delivery.status)}`, TONES[o.delivery.status]) : null),
    actions));
}

async function renderInventory(view) {
  const body = h("div");
  let term = "";
  const load = async () => {
    const rows = (await api("/api/admin/inventory")).filter(p =>
      !term || p.name.toLowerCase().includes(term.toLowerCase()) || p.sku.toLowerCase().includes(term.toLowerCase()));
    body.replaceChildren(table([
      { label: "Product", render: p => h("span", {}, p.name, h("span", { class: "sub" }, p.sku)) },
      { label: "Category", render: p => p.category },
      { label: "Stock", num: true, render: p => h("span", {}, String(p.stock), p.low ? badge("Low", "red") : null) },
      { label: "Alert at", num: true, render: p => p.low_stock_threshold },
      { label: "", actions: true, render: p => h("button", { class: "btn btn-outline-dark btn-small", type: "button",
          onclick: () => adjustStock(p) }, "Adjust stock") },
    ], rows, "No products found."));
  };
  const movements = h("div", {}, h("div", { class: "empty-inline" }, "Loading history..."));
  view.replaceChildren(
    toolbar(searchBox("Search product or SKU", (v) => { term = v; load(); })),
    body,
    h("section", { class: "panel" }, h("h3", {}, "Recent stock movements"), movements));
  await load();
  api("/api/admin/inventory/movements").catch(() => []).then(rows => movements.replaceChildren(table([
    { label: "When", render: m => when(m.created_at) },
    { label: "Product", render: m => m.product },
    { label: "Change", num: true, render: m => h("span", { class: m.change < 0 ? "neg" : "pos" }, (m.change > 0 ? "+" : "") + m.change) },
    { label: "Reason", render: m => m.reason },
    { label: "Reference", render: m => m.reference || "" },
  ], rows, "No movements yet.")));
}

function adjustStock(product) {
  formDialog(`Adjust stock: ${product.name}`, {
    submitLabel: "Record adjustment",
    fields: [
      { name: "change", label: "Change (+ adds, − removes)", type: "number", required: true, step: "1", help: `Current stock: ${product.stock}` },
      { name: "reason", label: "Reason", required: true, maxlength: 120, placeholder: "e.g. recount, damaged, returned", full: true },
    ],
    onSubmit: (v) => api("/api/admin/inventory/adjust", { method: "POST", body: {
      product_id: product.id, change: v.change, reason: v.reason } }),
  });
}

async function productForm(product) {
  const [categories, suppliers] = await Promise.all([
    api("/api/admin/categories"),
    api("/api/admin/suppliers").catch(() => []),
  ]);
  const isNew = !product;
  const fields = [
    { name: "name", label: "Product name", required: true, maxlength: 160 },
    { name: "sku", label: "SKU", required: true, maxlength: 60, help: "Unique code, e.g. IM-009" },
    { name: "category_id", label: "Category", type: "select", required: true, options: categories.map(c => [c.id, c.name]) },
    { name: "supplier_id", label: "Supplier", type: "select", options: [["", "None"], ...suppliers.map(s => [s.id, s.name])] },
    { name: "price", label: "Price (₦)", type: "number", required: true, min: 1, step: "1" },
    { name: "low_stock_threshold", label: "Low stock alert at", type: "number", min: 0, step: "1" },
    { name: "tag", label: "Badge", type: "select", options: PRODUCT_TAGS },
    { name: "image_url", label: "Image link (https)", maxlength: 500, full: true },
    { name: "description", label: "Description", type: "textarea", full: true, maxlength: 2000 },
  ];
  if (isNew) fields.splice(4, 0, { name: "stock", label: "Opening stock", type: "number", min: 0, step: "1" });
  else fields.push({ name: "is_active", label: "Show on the store", type: "checkbox" });

  const values = product
    ? { ...product, supplier_id: product.supplier_id ?? "" }
    : { low_stock_threshold: 5, tag: "", stock: 0 };

  formDialog(isNew ? "New product" : `Edit ${product.name}`, {
    fields, values,
    submitLabel: isNew ? "Create product" : "Save changes",
    onSubmit: (v) => isNew
      ? api("/api/admin/products", { method: "POST", body: v })
      : api(`/api/admin/products/${product.id}`, { method: "PATCH", body: v }),
  });
}

async function renderProducts(view) {
  const body = h("div");
  let term = "";
  const load = async () => {
    const params = new URLSearchParams(term ? { q: term } : {});
    const rows = await api(`/api/admin/products?${params}`);
    body.replaceChildren(table([
      { label: "Product", render: p => h("span", { class: "product-cell" },
          h("img", { class: "thumb", src: p.image_url || "", alt: "", onerror: (e) => { e.currentTarget.style.visibility = "hidden"; } }),
          h("span", {}, p.name, h("span", { class: "sub" }, p.sku))) },
      { label: "Category", render: p => p.category },
      { label: "Price", num: true, render: p => money(p.price) },
      { label: "Stock", num: true, render: p => String(p.stock) },
      { label: "Status", render: p => statusBadge(p.is_active ? "active" : "archived") },
      { label: "", actions: true, render: p => h("span", {},
          h("button", { class: "btn btn-outline-dark btn-small", type: "button", onclick: () => productForm(p) }, "Edit"),
          p.is_active
            ? h("button", { class: "btn btn-danger btn-small", type: "button", onclick: () => archiveProduct(p) }, "Archive")
            : h("button", { class: "btn btn-dark btn-small", type: "button", onclick: () => restoreProduct(p) }, "Restore")) },
    ], rows, "No products yet. Add your first product."));
  };
  view.replaceChildren(
    toolbar(searchBox("Search name or SKU", (v) => { term = v; load(); }),
      h("span", { class: "spacer" }),
      h("button", { class: "btn btn-dark btn-small", type: "button", onclick: () => productForm(null) }, "+ New product")),
    h("p", { class: "note" }, "Stock levels are changed in Inventory & Stock so every change is recorded."),
    body);
  await load();
}

async function archiveProduct(product) {
  if (!confirm(`Hide "${product.name}" from the store? Past orders are kept.`)) return;
  await api(`/api/admin/products/${product.id}`, { method: "DELETE" });
  toast("Product archived.");
  reloadSection();
}

async function restoreProduct(product) {
  await api(`/api/admin/products/${product.id}`, { method: "PATCH", body: { is_active: true } });
  toast("Product restored.");
  reloadSection();
}

async function renderCategories(view) {
  const rows = await api("/api/admin/categories");
  view.replaceChildren(
    toolbar(h("span", { class: "spacer" }),
      h("button", { class: "btn btn-dark btn-small", type: "button", onclick: () => categoryForm(null) }, "+ New category")),
    table([
      { label: "Name", render: c => h("span", {}, c.name, h("span", { class: "sub" }, `/${c.slug}`)) },
      { label: "Description", render: c => c.description || "" },
      { label: "Products", num: true, render: c => c.product_count },
      { label: "", actions: true, render: c => h("span", {},
          h("button", { class: "btn btn-outline-dark btn-small", type: "button", onclick: () => categoryForm(c) }, "Edit"),
          h("button", { class: "btn btn-danger btn-small", type: "button", onclick: () => deleteCategory(c) }, "Delete")) },
    ], rows, "No categories yet."));
}

function categoryForm(category) {
  formDialog(category ? `Edit ${category.name}` : "New category", {
    submitLabel: category ? "Save changes" : "Create category",
    values: category || {},
    fields: [
      { name: "name", label: "Category name", required: true, maxlength: 80 },
      { name: "description", label: "Description", type: "textarea", full: true, maxlength: 300 },
    ],
    onSubmit: (v) => category
      ? api(`/api/admin/categories/${category.id}`, { method: "PATCH", body: v })
      : api("/api/admin/categories", { method: "POST", body: v }),
  });
}

async function deleteCategory(category) {
  if (!confirm(`Delete the category "${category.name}"?`)) return;
  try {
    await api(`/api/admin/categories/${category.id}`, { method: "DELETE" });
    toast("Category deleted.");
    reloadSection();
  } catch (error) {
    toast(error.message, true);
  }
}

async function renderSuppliers(view) {
  const [suppliers, orders] = await Promise.all([
    api("/api/admin/suppliers"),
    api("/api/admin/purchase-orders"),
  ]);
  view.replaceChildren(
    toolbar(h("span", { class: "spacer" }),
      h("button", { class: "btn btn-dark btn-small", type: "button", onclick: () => supplierForm(null) }, "+ New supplier")),
    table([
      { label: "Supplier", render: s => h("span", {}, s.name, h("span", { class: "sub" }, s.address || "")) },
      { label: "Contact", render: s => h("span", {}, s.email || "", h("span", { class: "sub" }, s.phone || "")) },
      { label: "Status", render: s => statusBadge(s.is_active ? "active" : "archived") },
      { label: "", actions: true, render: s => h("button", { class: "btn btn-outline-dark btn-small", type: "button", onclick: () => supplierForm(s) }, "Edit") },
    ], suppliers, "No suppliers yet."),
    h("section", { class: "panel" },
      toolbar(h("h3", { class: "panel-title" }, "Purchase orders"), h("span", { class: "spacer" }),
        h("button", { class: "btn btn-dark btn-small", type: "button", onclick: purchaseOrderForm }, "+ New purchase order")),
      table([
        { label: "PO", render: po => `PO-${po.id}` },
        { label: "Supplier", render: po => po.supplier },
        { label: "Lines", num: true, render: po => po.line_count },
        { label: "Cost", num: true, render: po => money(po.total_cost) },
        { label: "Status", render: po => statusBadge(po.status) },
        { label: "Created", render: po => when(po.created_at) },
        { label: "", actions: true, render: po => h("span", {},
            h("button", { class: "btn btn-outline-dark btn-small", type: "button", onclick: () => openPurchaseOrder(po.id) }, "View"),
            po.status === "ordered"
              ? h("button", { class: "btn btn-dark btn-small", type: "button", onclick: () => receivePurchaseOrder(po) }, "Mark received")
              : null) },
      ], orders, "No purchase orders yet.")));
}

function supplierForm(supplier) {
  formDialog(supplier ? `Edit ${supplier.name}` : "New supplier", {
    submitLabel: supplier ? "Save changes" : "Create supplier",
    values: supplier || {},
    fields: [
      { name: "name", label: "Supplier name", required: true, maxlength: 120 },
      { name: "email", label: "Email", type: "email", maxlength: 180 },
      { name: "phone", label: "Phone", maxlength: 30 },
      { name: "address", label: "Address", full: true, maxlength: 300 },
      ...(supplier ? [{ name: "is_active", label: "Active supplier", type: "checkbox", full: true }] : []),
    ],
    onSubmit: (v) => supplier
      ? api(`/api/admin/suppliers/${supplier.id}`, { method: "PATCH", body: v })
      : api("/api/admin/suppliers", { method: "POST", body: v }),
  });
}

async function purchaseOrderForm() {
  const [suppliers, products] = await Promise.all([api("/api/admin/suppliers"), api("/api/admin/products")]);
  const supplierSelect = select(suppliers.filter(s => s.is_active).map(s => [s.id, s.name]), "", { "aria-label": "Supplier" });
  const productOptions = products.filter(p => p.is_active).map(p => [p.id, `${p.name} (${p.sku})`]);
  const lines = h("div", { class: "po-lines" });
  const addLine = () => {
    const row = h("div", { class: "po-line" },
      select(productOptions, "", { "aria-label": "Product" }),
      h("input", { type: "number", min: "1", step: "1", value: "1", "aria-label": "Quantity" }),
      h("input", { type: "number", min: "0", step: "1", value: "0", "aria-label": "Unit cost in naira" }),
      h("button", { class: "link-button", type: "button", onclick: () => row.remove() }, "Remove"));
    lines.append(row);
  };
  addLine();

  const errorBox = h("p", { class: "form-error", hidden: true });
  const form = h("form", {
    class: "form-grid",
    onsubmit: async (event) => {
      event.preventDefault();
      const items = [...lines.querySelectorAll(".po-line")].map(row => {
        const [product, quantity, cost] = row.querySelectorAll("select, input");
        return { product_id: Number(product.value), quantity: Number(quantity.value), unit_cost: Number(cost.value) };
      });
      try {
        await api("/api/admin/purchase-orders", { method: "POST", body: {
          supplier_id: Number(supplierSelect.value), items } });
        closeModal();
        toast("Purchase order created.");
        reloadSection();
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      }
    },
  },
    h("label", { class: "field-label full" }, h("span", {}, "Supplier"), supplierSelect),
    h("div", { class: "full" }, h("span", {}, "Products"), lines,
      h("button", { class: "btn btn-outline-dark btn-small", type: "button", onclick: addLine }, "+ Add product")),
    errorBox,
    h("div", { class: "form-actions full" },
      h("button", { class: "btn btn-outline-dark", type: "button", onclick: closeModal }, "Cancel"),
      h("button", { class: "btn btn-dark", type: "submit" }, "Create purchase order")));
  openModal("New purchase order", form);
}

async function openPurchaseOrder(id) {
  const po = await api(`/api/admin/purchase-orders/${id}`);
  openModal(`PO-${po.id} · ${po.supplier}`, h("div", {},
    table([
      { label: "Product", render: i => i.product },
      { label: "Qty", num: true, render: i => i.quantity },
      { label: "Unit cost", num: true, render: i => money(i.unit_cost) },
      { label: "Line total", num: true, render: i => money(i.quantity * i.unit_cost) },
    ], po.items),
    h("p", { class: "note mt" }, `Total cost ${money(po.total_cost)} · ${label(po.status)}`)));
}

async function receivePurchaseOrder(po) {
  if (!confirm(`Mark PO-${po.id} as received? Stock will be added to inventory.`)) return;
  try {
    await api(`/api/admin/purchase-orders/${po.id}/receive`, { method: "POST", body: {} });
    toast("Stock received and added to inventory.");
    reloadSection();
  } catch (error) {
    toast(error.message, true);
  }
}

async function renderCustomers(view) {
  let rows = await api("/api/admin/customers");
  const body = h("div");
  const draw = (term) => {
    const t = term.toLowerCase();
    const shown = rows.filter(c => !t || c.name.toLowerCase().includes(t) || c.email.toLowerCase().includes(t));
    body.replaceChildren(table([
      { label: "Customer", render: c => h("span", {}, c.name, h("span", { class: "sub" }, c.email)) },
      { label: "Phone", render: c => c.phone || "" },
      { label: "Orders", num: true, render: c => c.order_count },
      { label: "Total spent", num: true, render: c => money(c.total_spent) },
      { label: "Joined", render: c => when(c.created_at).slice(0, 10) },
      { label: "Account", render: c => c.is_active ? badge("Active", "green") : badge("Inactive", "red") },
    ], shown, "No customers match."));
  };
  view.replaceChildren(toolbar(searchBox("Search name or email", draw)), body);
  draw("");
}

async function renderPayments(view) {
  const filter = select([["", "All payments"], ["success", "Successful"], ["pending", "Pending"], ["failed", "Failed"]], "", { class: "filter-select" });
  const body = h("div");
  const load = async () => {
    const rows = await api(`/api/admin/payments${filter.value ? `?status=${filter.value}` : ""}`);
    body.replaceChildren(table([
      { label: "Reference", render: p => h("span", {}, p.reference, h("span", { class: "sub" }, when(p.created_at))) },
      { label: "Order", render: p => p.order_reference },
      { label: "Customer", render: p => p.customer_name },
      { label: "Channel", render: p => badge(label(p.provider === "cash" ? "cash" : p.provider), TONES[p.provider]) },
      { label: "Amount", num: true, render: p => money(p.amount) },
      { label: "Status", render: p => statusBadge(p.status) },
    ], rows, "No transactions yet."));
  };
  filter.addEventListener("change", load);
  view.replaceChildren(toolbar(filter), body);
  await load();
}

async function renderDeliveries(view) {
  const filter = select([["", "All deliveries"], ...DELIVERY_STATUSES.map(s => [s, label(s)])], "", { class: "filter-select" });
  const body = h("div");
  const load = async () => {
    const rows = await api(`/api/admin/deliveries${filter.value ? `?status=${filter.value}` : ""}`);
    body.replaceChildren(table([
      { label: "Order", render: d => h("span", {}, d.reference, h("span", { class: "sub" }, money(d.total))) },
      { label: "Customer", render: d => h("span", {}, d.customer_name, h("span", { class: "sub" }, d.customer_phone)) },
      { label: "Address", render: d => d.delivery_address },
      { label: "Rider", render: d => d.rider_name
          ? h("span", {}, d.rider_name, h("span", { class: "sub" }, d.rider_phone || ""))
          : h("span", { class: "muted" }, "Not assigned") },
      { label: "Delivery", render: d => statusBadge(d.status) },
      { label: "Order", render: d => statusBadge(d.order_status) },
      { label: "", actions: true, render: d => h("button", { class: "btn btn-outline-dark btn-small", type: "button",
          onclick: () => deliveryForm(d) }, "Update") },
    ], rows, "No deliveries here."));
  };
  filter.addEventListener("change", load);
  view.replaceChildren(toolbar(filter), body);
  await load();
}

function deliveryForm(d) {
  formDialog(`Delivery for ${d.reference}`, {
    submitLabel: "Save delivery",
    values: { rider_name: d.rider_name, rider_phone: d.rider_phone, status: d.status, notes: d.notes },
    fields: [
      { name: "rider_name", label: "Rider name", maxlength: 120 },
      { name: "rider_phone", label: "Rider phone", maxlength: 30 },
      { name: "status", label: "Delivery status", type: "select", options: DELIVERY_STATUSES.map(s => [s, label(s)]) },
      { name: "notes", label: "Notes", type: "textarea", full: true, maxlength: 500 },
    ],
    onSubmit: (v) => api(`/api/admin/deliveries/${d.order_id}`, { method: "PATCH", body: v }),
  });
}

async function renderReports(view) {
  const period = select([["7", "Last 7 days"], ["30", "Last 30 days"], ["90", "Last 90 days"], ["365", "Last 12 months"]], "30", { class: "filter-select" });
  const body = h("div");
  const load = async () => {
    const r = await api(`/api/admin/reports?days=${period.value}`);
    const max = Math.max(1, ...r.daily.map(d => Number(d.revenue) || 0));
    const byMethod = Object.fromEntries(r.by_method.map(m => [m.method, m]));
    body.replaceChildren(
      h("div", { class: "kpi-grid" },
        kpi("Revenue", money(r.totals.revenue), `${r.totals.orders} orders`),
        kpi("Average order", money(r.totals.average_order)),
        kpi("Paid online", money(byMethod.paystack?.amount || 0), `${byMethod.paystack?.count || 0} orders`),
        kpi("Cash on delivery", money(byMethod.cash_on_delivery?.amount || 0), `${byMethod.cash_on_delivery?.count || 0} orders`)),
      h("div", { class: "two-col" },
        h("section", { class: "panel" }, h("h3", {}, "Daily sales"), table([
          { label: "Day", render: d => d.day },
          { label: "Orders", num: true, render: d => d.orders },
          { label: "Revenue", num: true, render: d => money(d.revenue) },
          { label: "Trend", render: d => h("div", { class: "bar-track" },
              h("div", { class: "bar", style: `width:${Math.round((Number(d.revenue) / max) * 100)}%` })) },
        ], r.daily, "No sales in this period.")),
        h("div", {},
          h("section", { class: "panel" }, h("h3", {}, "Top products"), table([
            { label: "Product", render: p => p.name },
            { label: "Units", num: true, render: p => p.units },
            { label: "Revenue", num: true, render: p => money(p.revenue) },
          ], r.top_products, "No sales yet.")),
          h("section", { class: "panel" }, h("h3", {}, "Orders by status"), table([
            { label: "Status", render: s => statusBadge(s.status) },
            { label: "Count", num: true, render: s => s.count },
          ], r.by_status, "No orders yet.")))));
  };
  period.addEventListener("change", load);
  view.replaceChildren(toolbar(period), body);
  await load();
}

async function renderSettings(view) {
  const [settings, perms, users] = await Promise.all([
    api("/api/admin/settings"),
    api("/api/admin/permissions"),
    api("/api/admin/users"),
  ]);
  const isAdmin = state.user.role === "admin";

  const storeForm = buildForm({
    submitLabel: "Save store settings",
    values: { ...settings, cod_enabled: settings.cod_enabled === "1" },
    fields: [
      { name: "store_name", label: "Store name", required: true, maxlength: 80 },
      { name: "whatsapp_number", label: "WhatsApp support number", maxlength: 20,
        help: "Country code and digits, e.g. 2348012345678. Leave blank to hide the chat button." },
      { name: "delivery_fee", label: "Delivery fee (₦)", type: "number", min: 0, step: "1" },
      { name: "cod_enabled", label: "Allow pay on delivery", type: "checkbox" },
    ],
    onSubmit: (v) => api("/api/admin/settings", { method: "PATCH", body: v }),
    afterSave: () => reloadSection(),
  });

  const permBoxes = perms.permissions.map(p => h("label", {},
    h("input", { type: "checkbox", name: p.key, checked: Boolean(perms.staff[p.key]), disabled: !isAdmin }),
    h("span", {}, p.label)));
  const saveButton = h("button", { class: "btn btn-dark", type: "button", disabled: !isAdmin,
    onclick: async () => {
      const staff = {};
      permBoxes.forEach(box => { const input = box.querySelector("input"); staff[input.name] = input.checked; });
      try {
        await api("/api/admin/permissions", { method: "PUT", body: { staff } });
        toast("Staff permissions saved.");
      } catch (error) { toast(error.message, true); }
    } }, "Save permissions");

  view.replaceChildren(
    h("section", { class: "panel" }, h("h3", {}, "Store settings"), storeForm),
    h("section", { class: "panel" }, h("h3", {}, "Staff permissions"),
      h("p", { class: "note" }, isAdmin
        ? "Choose what staff accounts can open. Administrators always have full access."
        : "Only administrators can change these permissions."),
      h("div", { class: "perm-grid" }, permBoxes),
      h("div", { class: "form-actions" }, saveButton)),
    h("section", { class: "panel" }, h("h3", {}, "Users and roles"),
      h("p", { class: "note" }, isAdmin
        ? "Promote a customer to staff or admin here. Staff see only the sections they are allowed."
        : "Only administrators can change roles."),
      table([
        { label: "Name", render: u => h("span", {}, u.name, h("span", { class: "sub" }, u.email)) },
        { label: "Role", render: u => isAdmin
            ? select([["customer", "Customer"], ["staff", "Staff"], ["admin", "Admin"]], u.role, {
                "aria-label": `Role for ${u.name}`,
                onchange: async (event) => {
                  try {
                    await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { role: event.target.value } });
                    toast("Role updated.");
                  } catch (error) { toast(error.message, true); reloadSection(); }
                } })
            : statusBadge(u.role) },
        { label: "Active", render: u => isAdmin
            ? h("input", { type: "checkbox", checked: Boolean(u.is_active), "aria-label": `Active: ${u.name}`,
                onchange: async (event) => {
                  try {
                    await api(`/api/admin/users/${u.id}`, { method: "PATCH", body: { is_active: event.target.checked } });
                    toast("Account updated.");
                  } catch (error) { toast(error.message, true); reloadSection(); }
                } })
            : (u.is_active ? badge("Active", "green") : badge("Inactive", "red")) },
        { label: "Joined", render: u => when(u.created_at).slice(0, 10) },
      ], users, "No users yet.")));
}

/* ---------- navigation and shell ---------- */

const SECTIONS = [
  { id: "dashboard", label: "Dashboard Overview", icon: "◧", perm: null, render: renderDashboard },
  { id: "orders", label: "Orders", icon: "☰", perm: "orders", render: renderOrders },
  { id: "inventory", label: "Inventory & Stock", icon: "▦", perm: "inventory", render: renderInventory },
  { id: "products", label: "Products", icon: "▢", perm: "products", render: renderProducts },
  { id: "categories", label: "Categories", icon: "▤", perm: "categories", render: renderCategories },
  { id: "suppliers", label: "Suppliers & Procurement", icon: "⇆", perm: "suppliers", render: renderSuppliers },
  { id: "customers", label: "Customers", icon: "◉", perm: "customers", render: renderCustomers },
  { id: "payments", label: "Payments & Transactions", icon: "₦", perm: "payments", render: renderPayments },
  { id: "delivery", label: "Delivery Management", icon: "➤", perm: "delivery", render: renderDeliveries },
  { id: "reports", label: "Reports & Analytics", icon: "▮", perm: "reports", render: renderReports },
  { id: "settings", label: "Settings & Permissions", icon: "⚙", perm: "settings", render: renderSettings },
];

const canSee = (section) => !section.perm || state.perms[section.perm];

function renderNav() {
  const deskLink = state.perms.orders
    ? [h("a", { class: "desk-link", href: "orders.html" },
        h("span", { class: "nav-icon", "aria-hidden": "true" }, "⚡"), h("span", {}, "Orders Desk"))]
    : [];
  $("sideNav").replaceChildren(...deskLink, ...SECTIONS.filter(canSee).map(section =>
    h("button", { type: "button", "data-id": section.id, onclick: () => showSection(section.id) },
      h("span", { class: "nav-icon", "aria-hidden": "true" }, section.icon),
      h("span", {}, section.label))));
}

function highlightNav(id) {
  document.querySelectorAll("#sideNav button").forEach(btn => {
    if (btn.dataset.id === id) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
}

async function showSection(id) {
  let section = SECTIONS.find(s => s.id === id) || SECTIONS[0];
  if (!canSee(section)) section = SECTIONS[0];
  state.section = section.id;
  history.replaceState(null, "", `#${section.id}`);
  highlightNav(section.id);
  $("viewTitle").textContent = section.label;
  const view = $("view");
  view.replaceChildren(h("div", { class: "loading" }, "Loading..."));
  try {
    await section.render(view);
  } catch (error) {
    view.replaceChildren(h("div", { class: "form-error" }, error.message));
  }
}

function reloadSection() {
  showSection(state.section || "dashboard");
}

function showGate(message) {
  $("appShell").hidden = true;
  $("loginGate").hidden = false;
  $("gateError").textContent = message || "";
  $("gateError").hidden = !message;
}

async function startConsole() {
  try {
    const access = await api("/api/admin/access");
    state.user = access.user;
    state.perms = access.permissions;
  } catch (error) {
    showGate(error.status === 403 ? "This account does not have admin access." : "");
    return;
  }
  $("loginGate").hidden = true;
  $("appShell").hidden = false;
  $("whoami").textContent = `${state.user.name} · ${label(state.user.role)}`;
  renderNav();
  const start = location.hash.slice(1);
  showSection(start || "dashboard");
}

async function onLogin(event) {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/auth/login", { method: "POST", body: {
      email: form.elements.email.value, password: form.elements.password.value } });
    await startConsole();
  } catch (error) {
    showGate(error.message);
  }
}

async function logout() {
  await api("/api/auth/logout", { method: "POST", body: {} }).catch(() => {});
  location.reload();
}

function startClock() {
  const tick = () => { $("clock").textContent = new Date().toLocaleString("en-NG", { dateStyle: "medium", timeStyle: "short" }); };
  tick();
  setInterval(tick, 30000);
}

document.addEventListener("DOMContentLoaded", () => {
  $("adminLoginForm").addEventListener("submit", onLogin);
  $("logoutBtn").addEventListener("click", logout);
  window.addEventListener("hashchange", () => {
    if (location.hash.slice(1) !== state.section && state.user) showSection(location.hash.slice(1));
  });
  startClock();
  startConsole();
});

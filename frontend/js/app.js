/* Infinity MarketPlace - storefront script (all customer pages).
   The <body data-page="..."> attribute chooses which page logic runs.
   Prices and stock are always confirmed by the server at checkout; the cart stores only product IDs and quantities. */
"use strict";

const CART_KEY = "infinityMarketPlaceCart";
const MAX_QTY = 50;
const PLACEHOLDER_IMAGE = "https://placehold.co/700x500/eef2f3/52636d?text=Product+Image";

/* ---------- small helpers ---------- */

const $ = (id) => document.getElementById(id);

/* Build DOM safely. Text is always inserted as text, never as HTML. */
function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (typeof value === "boolean") node[key] = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function naira(amount) {
  return new Intl.NumberFormat("en-NG", { style: "currency", currency: "NGN", maximumFractionDigits: 0 }).format(amount);
}

function toast(message) {
  const box = $("toast");
  if (!box) return;
  box.textContent = message;
  box.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove("show"), 2800);
}

async function api(path, { method = "GET", body } = {}) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try { data = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    const error = new Error((data && data.error) || `Something went wrong (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function safeNext(value) {
  // Only allow redirects to our own pages, e.g. "checkout.html" or "orders.html?ref=IM123".
  return /^[a-z-]+\.html(\?[\w=&.%-]*)?$/i.test(value || "") ? value : "orders.html";
}

/* ---------- cart (localStorage: product id + quantity only) ---------- */

function getCart() {
  try {
    const cart = JSON.parse(localStorage.getItem(CART_KEY) || "[]");
    return Array.isArray(cart) ? cart.filter(i => Number.isInteger(i.productId) && i.quantity > 0) : [];
  } catch (_) {
    return [];
  }
}

function saveCart(cart) {
  localStorage.setItem(CART_KEY, JSON.stringify(cart));
  updateCartBadge();
}

function addToCart(productId, quantity = 1) {
  const cart = getCart();
  const line = cart.find(i => i.productId === productId);
  if (line) line.quantity = Math.min(MAX_QTY, line.quantity + quantity);
  else cart.push({ productId, quantity: Math.min(MAX_QTY, quantity) });
  saveCart(cart);
}

function setCartQuantity(productId, quantity) {
  const cart = getCart()
    .map(i => (i.productId === productId ? { ...i, quantity: Math.min(MAX_QTY, quantity) } : i))
    .filter(i => i.quantity > 0);
  saveCart(cart);
}

function updateCartBadge() {
  const count = getCart().reduce((total, item) => total + item.quantity, 0);
  const badge = $("cartCount");
  if (badge) badge.textContent = String(count);
}

/* ---------- shared site chrome ---------- */

let siteConfig = { store_name: "Infinity MarketPlace", whatsapp_number: "", payment_methods: [], delivery_fee: 0 };

async function loadConfig() {
  try { siteConfig = await api("/api/config"); } catch (_) { /* keep defaults */ }
  return siteConfig;
}

function whatsappUrl(message) {
  if (!siteConfig.whatsapp_number) return null;
  return `https://wa.me/${siteConfig.whatsapp_number}?text=${encodeURIComponent(message)}`;
}

function renderWhatsAppButton() {
  const url = whatsappUrl("Hello Infinity MarketPlace, I need help with my shopping.");
  if (!url || document.querySelector(".whatsapp-float")) return;
  document.body.append(h("a", {
    class: "whatsapp-float", href: url, target: "_blank", rel: "noopener",
    "aria-label": "Chat with customer support on WhatsApp",
  }, h("span", { "aria-hidden": "true" }, "💬"), h("span", {}, "Chat with us")));
}

function setupHeader() {
  const toggle = $("mobileMenuToggle");
  const nav = $("primaryNav");
  if (toggle && nav) {
    toggle.addEventListener("click", () => {
      const open = nav.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    });
  }

  const search = $("siteSearch");
  if (search && document.body.dataset.page !== "home") {
    search.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && search.value.trim()) {
        location.href = `index.html?q=${encodeURIComponent(search.value.trim())}#products`;
      }
    });
  }
  const year = $("currentYear");
  if (year) year.textContent = String(new Date().getFullYear());
}

async function refreshAccountLink() {
  const link = $("accountLink");
  if (!link) return null;
  try {
    const { user } = await api("/api/auth/me");
    if (user) {
      link.textContent = user.name.split(" ")[0];
      link.href = "orders.html";
      link.setAttribute("aria-label", `My account (${user.name})`);
    }
    return user;
  } catch (_) {
    return null;
  }
}

function showPageError(message, container) {
  const node = h("div", { class: "empty-state" },
    h("span", { "aria-hidden": "true" }, "!"),
    h("h3", {}, "Something went wrong"),
    h("p", {}, message));
  if (container) container.replaceChildren(node);
  else toast(message);
}

/* ---------- home: product browsing ---------- */

let homeRequestId = 0;

async function initHome() {
  const categorySelect = $("categoryFilter");
  const sortSelect = $("sortProducts");
  const search = $("siteSearch");
  const params = new URLSearchParams(location.search);
  if (params.get("q") && search) search.value = params.get("q");

  try {
    const categories = await api("/api/categories");
    categories.forEach(c => categorySelect.append(h("option", { value: c.slug }, c.name)));
  } catch (_) { /* filter stays on "All" */ }

  document.querySelectorAll(".category-card[data-category]").forEach(card => {
    card.addEventListener("click", () => {
      categorySelect.value = card.dataset.category;
      loadHomeProducts();
      $("products")?.scrollIntoView({ behavior: "smooth" });
    });
  });

  categorySelect.addEventListener("change", loadHomeProducts);
  sortSelect.addEventListener("change", loadHomeProducts);
  let timer;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(loadHomeProducts, 250);
  });
  $("clearFilters").addEventListener("click", resetHomeFilters);
  $("resetSearch").addEventListener("click", resetHomeFilters);

  if (params.get("q")) $("products")?.scrollIntoView();
  loadHomeProducts();
}

function resetHomeFilters() {
  $("categoryFilter").value = "All";
  $("siteSearch").value = "";
  $("sortProducts").value = "featured";
  loadHomeProducts();
}

async function loadHomeProducts() {
  const grid = $("productGrid");
  const categorySelect = $("categoryFilter");
  const search = $("siteSearch");
  const sort = $("sortProducts").value;
  const requestId = ++homeRequestId;

  const query = new URLSearchParams();
  if (categorySelect.value !== "All") query.set("category", categorySelect.value);
  if (search.value.trim()) query.set("q", search.value.trim());
  if (sort !== "featured") query.set("sort", sort);

  grid.replaceChildren(h("div", { class: "loading-state" }, "Loading products..."));
  let products;
  try {
    products = await api(`/api/products?${query}`);
  } catch (error) {
    if (requestId !== homeRequestId) return;
    showPageError(error.message, grid);
    return;
  }
  if (requestId !== homeRequestId) return; // a newer search has started

  const hasFilters = categorySelect.value !== "All" || search.value.trim() !== "";
  grid.replaceChildren(...products.map(productCard));
  $("productResultText").textContent = products.length === 0
    ? "No matching products"
    : `Showing ${products.length} ${hasFilters ? "matching" : "featured"} product${products.length === 1 ? "" : "s"}`;
  $("emptyState").hidden = products.length !== 0;
  grid.hidden = products.length === 0;
  $("clearFilters").hidden = !hasFilters;
}

function productCard(product) {
  const link = `product.html?id=${product.id}`;
  const addButton = h("button", {
    class: "add-cart-btn", type: "button", disabled: !product.in_stock,
    "aria-label": `Add ${product.name} to cart`, title: product.in_stock ? "Add to cart" : "Out of stock",
    onclick: () => { addToCart(product.id); toast(`${product.name} added to your cart.`); },
  }, "+");

  return h("article", { class: "product-card" },
    h("a", { class: "product-image", href: link, "aria-label": product.name },
      h("img", { src: product.image_url || PLACEHOLDER_IMAGE, alt: "", loading: "lazy",
                 onerror: (e) => { e.currentTarget.src = PLACEHOLDER_IMAGE; } }),
      product.tag ? h("span", { class: "product-tag" }, product.tag) : null),
    h("div", { class: "product-info" },
      h("span", { class: "product-category" }, product.category),
      h("h3", {}, h("a", { href: link }, product.name)),
      h("p", { class: "product-description" }, product.description),
      h("div", { class: "product-bottom" },
        h("strong", { class: "product-price" }, naira(product.price)),
        product.in_stock ? addButton : h("span", { class: "stock-note" }, "Out of stock"))));
}

/* ---------- product page ---------- */

async function initProduct() {
  const container = $("productDetail");
  const id = Number(new URLSearchParams(location.search).get("id"));
  if (!Number.isInteger(id) || id < 1) {
    return showPageError("We could not find that product.", container);
  }
  let product;
  try {
    product = await api(`/api/products/${id}`);
  } catch (error) {
    return showPageError(error.status === 404 ? "This product is no longer available." : error.message, container);
  }
  document.title = `${product.name} | Infinity MarketPlace`;
  $("crumbName").textContent = product.name;
  $("crumbCategory").textContent = product.category;

  const qty = h("input", { type: "number", min: "1", max: String(MAX_QTY), value: "1", id: "qtyInput",
                           "aria-label": "Quantity" });
  const addButton = h("button", { class: "btn btn-dark", type: "button", disabled: !product.in_stock,
    onclick: () => {
      addToCart(product.id, clampQty(qty.value));
      toast(`${product.name} added to your cart.`);
    } }, "Add to cart");
  const buyButton = h("button", { class: "btn btn-primary", type: "button", disabled: !product.in_stock,
    onclick: () => {
      addToCart(product.id, clampQty(qty.value));
      location.href = "cart.html";
    } }, "Buy now");

  container.replaceChildren(
    h("div", { class: "detail-image" },
      h("img", { src: product.image_url || PLACEHOLDER_IMAGE, alt: product.name,
                 onerror: (e) => { e.currentTarget.src = PLACEHOLDER_IMAGE; } })),
    h("div", { class: "detail-info" },
      product.tag ? h("span", { class: "product-tag-inline" }, product.tag) : null,
      h("span", { class: "product-category" }, product.category),
      h("h1", {}, product.name),
      h("p", { class: "detail-price" }, naira(product.price)),
      h("p", { class: "detail-desc" }, product.description || "No description available."),
      h("p", { class: product.in_stock ? "stock-ok" : "stock-out" },
        product.in_stock ? "In stock" : "Currently out of stock"),
      h("div", { class: "detail-actions" },
        h("label", { class: "qty-label", for: "qtyInput" }, "Quantity"),
        qty, addButton, buyButton),
      h("p", { class: "detail-note" },
        "Delivery fee is added at checkout. Payment options are shown before you pay.")));
}

function clampQty(value) {
  const n = Math.floor(Number(value));
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.min(MAX_QTY, n);
}

/* ---------- cart page ---------- */

async function loadCartLines() {
  const cart = getCart();
  if (!cart.length) return { lines: [], removed: false };
  const products = await api("/api/products");
  const byId = new Map(products.map(p => [p.id, p]));
  const lines = [];
  let removed = false;
  for (const item of cart) {
    const product = byId.get(item.productId);
    if (product) lines.push({ product, quantity: item.quantity });
    else removed = true;
  }
  if (removed) {
    saveCart(lines.map(l => ({ productId: l.product.id, quantity: l.quantity })));
  }
  return { lines, removed };
}

function orderTotals(lines) {
  const subtotal = lines.reduce((sum, l) => sum + l.product.price * l.quantity, 0);
  const fee = lines.length ? siteConfig.delivery_fee : 0;
  return { subtotal, fee, total: subtotal + fee };
}

async function initCart() {
  await loadConfig();
  const container = $("cartItems");
  let data;
  try {
    data = await loadCartLines();
  } catch (error) {
    return showPageError(error.message, container);
  }
  if (data.removed) toast("Some items are no longer available and were removed.");
  renderCart(data.lines);
}

function renderCart(lines) {
  const container = $("cartItems");
  const summary = $("cartSummary");
  if (!lines.length) {
    container.replaceChildren(h("div", { class: "empty-state" },
      h("span", { "aria-hidden": "true" }, "🛒"),
      h("h3", {}, "Your cart is empty"),
      h("p", {}, "Find something you like and it will appear here."),
      h("a", { class: "btn btn-dark", href: "index.html#products" }, "Browse products")));
    summary.hidden = true;
    return;
  }
  summary.hidden = false;

  container.replaceChildren(...lines.map(({ product, quantity }) => {
    const minus = h("button", { type: "button", "aria-label": `Decrease quantity of ${product.name}`,
      onclick: () => { setCartQuantity(product.id, quantity - 1); initCart(); } }, "−");
    const plus = h("button", { type: "button", "aria-label": `Increase quantity of ${product.name}`,
      disabled: quantity >= MAX_QTY,
      onclick: () => { setCartQuantity(product.id, quantity + 1); initCart(); } }, "+");
    return h("article", { class: "cart-line" },
      h("img", { src: product.image_url || PLACEHOLDER_IMAGE, alt: "",
                 onerror: (e) => { e.currentTarget.src = PLACEHOLDER_IMAGE; } }),
      h("div", { class: "cart-line-info" },
        h("a", { href: `product.html?id=${product.id}`, class: "cart-line-name" }, product.name),
        h("span", { class: "cart-line-price" }, naira(product.price)),
        product.in_stock ? null : h("span", { class: "stock-out" }, "Out of stock - remove to continue")),
      h("div", { class: "stepper" }, minus, h("span", { "aria-live": "polite" }, String(quantity)), plus),
      h("strong", { class: "cart-line-total" }, naira(product.price * quantity)),
      h("button", { class: "link-button", type: "button",
        onclick: () => { setCartQuantity(product.id, 0); toast("Item removed."); initCart(); } }, "Remove"));
  }));

  const totals = orderTotals(lines);
  $("sumSubtotal").textContent = naira(totals.subtotal);
  $("sumFee").textContent = naira(totals.fee);
  $("sumTotal").textContent = naira(totals.total);
  const blocked = lines.some(l => !l.product.in_stock);
  const checkout = $("checkoutLink");
  checkout.setAttribute("aria-disabled", String(blocked));
  checkout.classList.toggle("disabled", blocked);
  checkout.href = blocked ? "#" : "checkout.html";
  $("cartWarning").hidden = !blocked;
}

/* ---------- checkout ---------- */

async function initCheckout() {
  const user = await refreshAccountLink();
  if (!user) {
    location.href = `login.html?next=${encodeURIComponent("checkout.html")}`;
    return;
  }
  await loadConfig();
  const data = await loadCartLines().catch(() => ({ lines: [] }));
  if (!data.lines.length) {
    location.href = "cart.html";
    return;
  }

  const form = $("checkoutForm");
  form.elements.name.value = user.name;
  form.elements.phone.value = user.phone || "";

  const methods = siteConfig.payment_methods || [];
  const methodList = $("paymentMethods");
  if (!methods.length) {
    methodList.replaceChildren(h("p", { class: "form-error" }, "No payment method is available right now. Please contact us on WhatsApp."));
  }
  const labels = {
    paystack: "Pay now by card, bank transfer or USSD (Paystack)",
    cash_on_delivery: "Pay cash on delivery",
  };
  methods.forEach((method, index) => {
    methodList.append(h("label", { class: "radio-option" },
      h("input", { type: "radio", name: "payment_method", value: method, checked: index === 0 }),
      h("span", {}, labels[method] || method)));
  });

  const totals = orderTotals(data.lines);
  $("orderLines").replaceChildren(...data.lines.map(({ product, quantity }) =>
    h("div", { class: "summary-line" },
      h("span", {}, `${product.name} × ${quantity}`),
      h("strong", {}, naira(product.price * quantity)))));
  $("sumSubtotal").textContent = naira(totals.subtotal);
  $("sumFee").textContent = naira(totals.fee);
  $("sumTotal").textContent = naira(totals.total);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const errorBox = $("checkoutError");
    errorBox.hidden = true;
    const button = $("placeOrderBtn");
    const payment = form.elements.payment_method?.value;
    if (!payment) {
      errorBox.textContent = "Please choose a payment method.";
      errorBox.hidden = false;
      return;
    }
    button.disabled = true;
    button.textContent = "Placing order...";
    const body = {
      items: getCart().map(i => ({ product_id: i.productId, quantity: i.quantity })),
      name: form.elements.name.value,
      phone: form.elements.phone.value,
      address: form.elements.address.value,
      notes: form.elements.notes.value,
      payment_method: payment,
    };
    try {
      const result = await api("/api/checkout", { method: "POST", body });
      saveCart([]);
      if (result.authorization_url) {
        location.href = result.authorization_url; // Paystack hosted checkout
      } else {
        location.href = `orders.html?ref=${encodeURIComponent(result.reference)}&placed=1`;
      }
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
      button.disabled = false;
      button.textContent = "Place order";
      if (error.status === 409) await initCartRefresh();
    }
  });
}

async function initCartRefresh() {
  // Stock changed while checking out: rebuild the summary from current data.
  const data = await loadCartLines().catch(() => null);
  if (data && data.lines.length === 0) location.href = "cart.html";
}

/* ---------- login & registration ---------- */

function initLogin() {
  const next = safeNext(new URLSearchParams(location.search).get("next"));
  const tabs = document.querySelectorAll("[data-tab]");
  tabs.forEach(tab => tab.addEventListener("click", () => {
    const target = tab.dataset.tab;
    tabs.forEach(t => t.classList.toggle("active", t === tab));
    $("loginForm").hidden = target !== "login";
    $("registerForm").hidden = target !== "register";
    $("authError").hidden = true;
  }));
  if (new URLSearchParams(location.search).get("tab") === "register") {
    document.querySelector('[data-tab="register"]').click();
  }

  const showError = (message) => {
    const box = $("authError");
    box.textContent = message;
    box.hidden = false;
  };

  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api("/api/auth/login", { method: "POST", body: {
        email: form.elements.email.value, password: form.elements.password.value } });
      location.href = next;
    } catch (error) {
      showError(error.message);
    }
  });

  $("registerForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await api("/api/auth/register", { method: "POST", body: {
        name: form.elements.name.value,
        email: form.elements.email.value,
        phone: form.elements.phone.value,
        password: form.elements.password.value } });
      location.href = next;
    } catch (error) {
      showError(error.message);
    }
  });
}

/* ---------- orders ---------- */

const ORDER_STATUS_LABELS = {
  pending: "Awaiting payment",
  processing: "Processing",
  shipped: "Shipped",
  out_for_delivery: "Out for delivery",
  delivered: "Delivered",
  cancelled: "Cancelled",
};
const ORDER_STEPS = ["processing", "shipped", "out_for_delivery", "delivered"];

async function initOrders() {
  const user = await refreshAccountLink();
  if (!user) {
    const here = "orders.html" + location.search;
    location.href = `login.html?next=${encodeURIComponent(here)}`;
    return;
  }
  $("logoutBtn").addEventListener("click", async () => {
    await api("/api/auth/logout", { method: "POST", body: {} }).catch(() => {});
    location.href = "index.html";
  });

  const params = new URLSearchParams(location.search);
  if (params.get("ref")) return showOrderDetail(params.get("ref"), params);
  return showOrderList();
}

function statusPill(order) {
  const key = order.status;
  return h("span", { class: `status-pill status-${key}` }, ORDER_STATUS_LABELS[key] || key);
}

async function showOrderList() {
  $("orderTitle").textContent = "My orders";
  const container = $("orderContent");
  let orders;
  try {
    orders = await api("/api/orders");
  } catch (error) {
    return showPageError(error.message, container);
  }
  if (!orders.length) {
    container.replaceChildren(h("div", { class: "empty-state" },
      h("span", { "aria-hidden": "true" }, "📦"),
      h("h3", {}, "No orders yet"),
      h("p", {}, "Your orders will appear here after you check out."),
      h("a", { class: "btn btn-dark", href: "index.html#products" }, "Start shopping")));
    return;
  }
  container.replaceChildren(h("div", { class: "table-wrap" },
    h("table", { class: "data-table" },
      h("thead", {}, h("tr", {},
        h("th", {}, "Order"), h("th", {}, "Date"), h("th", {}, "Total"),
        h("th", {}, "Payment"), h("th", {}, "Status"))),
      h("tbody", {}, orders.map(o => h("tr", {},
        h("td", {}, h("a", { href: `orders.html?ref=${encodeURIComponent(o.reference)}` }, o.reference)),
        h("td", {}, (o.created_at || "").slice(0, 10)),
        h("td", {}, naira(o.total)),
        h("td", {}, `${o.payment_method === "paystack" ? "Online" : "On delivery"} · ${o.payment_status}`),
        h("td", {}, statusPill(o))))))));
}

async function showOrderDetail(reference, params) {
  $("orderTitle").textContent = `Order ${reference}`;
  const container = $("orderContent");
  const notice = $("orderNotice");
  if (params.get("placed") === "1") {
    notice.textContent = "Thank you! Your order has been placed.";
    notice.hidden = false;
  } else if (params.get("payment") === "unverified") {
    notice.textContent = "We are still confirming your payment. Refresh this page in a moment.";
    notice.hidden = false;
  }

  let order;
  try {
    order = await api(`/api/orders/${encodeURIComponent(reference)}`);
  } catch (error) {
    return showPageError(error.message, container);
  }
  document.title = `Order ${order.reference} | Infinity MarketPlace`;

  const cancelled = order.status === "cancelled";
  const currentIndex = ORDER_STEPS.indexOf(order.status);
  const timeline = cancelled ? null : h("ol", { class: "timeline" }, ORDER_STEPS.map((step, index) =>
    h("li", { class: index <= currentIndex ? "done" : "" }, ORDER_STATUS_LABELS[step])));

  const canPayNow = order.payment_method === "paystack" && order.payment_status !== "paid" && !cancelled;
  const payButton = canPayNow ? h("button", { class: "btn btn-primary", type: "button",
    onclick: async (event) => {
      event.currentTarget.disabled = true;
      try {
        const { authorization_url } = await api(`/api/orders/${encodeURIComponent(order.reference)}/pay`, { method: "POST", body: {} });
        location.href = authorization_url;
      } catch (error) {
        toast(error.message);
        event.currentTarget.disabled = false;
      }
    } }, "Pay now") : null;

  const support = whatsappUrl(`Hello, I need help with order ${order.reference}.`);

  container.replaceChildren(
    h("div", { class: "order-head" },
      h("div", {}, h("span", { class: "muted" }, (order.created_at || "").slice(0, 16)), h("h2", {}, order.reference)),
      statusPill(order)),
    timeline,
    h("div", { class: "order-grid" },
      h("section", { class: "card-block" },
        h("h3", {}, "Items"),
        h("div", { class: "table-wrap" }, h("table", { class: "data-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Product"), h("th", {}, "Qty"), h("th", {}, "Amount"))),
          h("tbody", {}, order.items.map(i => h("tr", {},
            h("td", {}, i.product_name), h("td", {}, String(i.quantity)), h("td", {}, naira(i.line_total))))))),
        h("div", { class: "totals" },
          h("div", {}, h("span", {}, "Subtotal"), h("strong", {}, naira(order.subtotal))),
          h("div", {}, h("span", {}, "Delivery"), h("strong", {}, naira(order.delivery_fee))),
          h("div", { class: "grand" }, h("span", {}, "Total"), h("strong", {}, naira(order.total))))),
      h("section", { class: "card-block" },
        h("h3", {}, "Delivery"),
        h("p", {}, order.delivery_address),
        h("p", { class: "muted" }, `Delivery status: ${order.delivery?.status?.replace(/_/g, " ") || "unassigned"}`),
        order.delivery?.rider_name ? h("p", {}, `Rider: ${order.delivery.rider_name}${order.delivery.rider_phone ? " · " + order.delivery.rider_phone : ""}`) : null,
        h("h3", { class: "mt" }, "Payment"),
        h("p", {}, `${order.payment_method === "paystack" ? "Paid online (Paystack)" : "Cash on delivery"} · ${order.payment_status === "paid" ? "Paid" : "Not yet paid"}`),
        payButton,
        h("div", { class: "order-actions" },
          support ? h("a", { class: "btn btn-outline-dark", href: support, target: "_blank", rel: "noopener" }, "Ask about this order on WhatsApp") : null))));
}

/* ---------- boot ---------- */

async function boot() {
  const page = document.body.dataset.page;
  setupHeader();
  updateCartBadge();
  await loadConfig();
  renderWhatsAppButton();
  if (page !== "checkout" && page !== "orders") refreshAccountLink();

  const pages = {
    home: initHome, product: initProduct, cart: initCart,
    checkout: initCheckout, login: initLogin, orders: initOrders,
  };
  const start = pages[page];
  if (start) {
    try {
      await start();
    } catch (error) {
      showPageError(error.message, $("pageContent") || $("productDetail") || $("cartItems") || $("orderContent"));
    }
  }
}

document.addEventListener("DOMContentLoaded", boot);

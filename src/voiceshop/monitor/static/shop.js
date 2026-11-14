/* Eliza Voice Shopping storefront. The page is a view of the shared session: the voice agent and the
   shopper change the same state, and every change arrives over the live stream. Text is written with
   textContent only. */
(() => {
  "use strict";

  const SID_RE = /^[A-Za-z0-9._-]{1,120}$/;
  const CATS = {
    tent: { label: "Tents", color: "#0f766e", glyph: "TE" },
    sleeping_bag: { label: "Sleeping bags", color: "#7c3aed", glyph: "SB" },
    backpack: { label: "Backpacks", color: "#b45309", glyph: "BP" },
    stove: { label: "Stoves", color: "#dc2626", glyph: "ST" },
  };
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------- session id
  function resolveSid() {
    const fromUrl = new URLSearchParams(location.search).get("sid");
    if (fromUrl && SID_RE.test(fromUrl)) return fromUrl;
    let stored = null;
    try { stored = localStorage.getItem("evs.sid"); } catch (err) { stored = null; }
    if (!stored || !SID_RE.test(stored)) {
      stored = `shop-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
      try { localStorage.setItem("evs.sid", stored); } catch (err) { /* storage can be blocked */ }
    }
    return stored;
  }
  const sid = resolveSid();
  const url = new URL(location.href);
  if (url.searchParams.get("sid") !== sid) {
    url.searchParams.set("sid", sid);
    history.replaceState(null, "", url.toString());
  }

  // ------------------------------------------------------------------ state
  const view = { catalog: null, state: null, lastN: -1, stream: null };
  // Inside the monitor, the page follows the conversation (sync messages); alone, it follows the live stream.
  const EMBEDDED = window.parent !== window;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  const money = (n) => `$${Number(n).toLocaleString("en-US")}`;
  const product = (id) => (view.catalog ? view.catalog.products[id] : null);

  async function api(path, body) {
    const res = await fetch(path, body
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
      : { headers: { Accept: "application/json" } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error ? data.error.message : `HTTP ${res.status}`);
    return data;
  }

  async function act(action, extra = {}) {
    try {
      const s = await api("/shop/api/action", { sid, action, ...extra });
      apply(s);
    } catch (err) {
      toast(`Sorry, that didn't work: ${err.message}`, "warn");
    }
  }

  // Describe a change made by the voice agent, in the shopper's language.
  function voiceToast(e) {
    const pname = (pid) => (product(pid) ? product(pid).name : pid);
    switch (e.action) {
      case "add_to_cart": return `Eliza added the ${pname(e.detail.product)} to your cart`;
      case "remove_from_cart": return `Eliza removed the ${pname(e.detail.product)}`;
      case "request_order": return "Eliza asked you to confirm the order";
      case "place_order": return "Eliza placed your order";
      case "cancel_order": return "Eliza did not place the order. Your cart is unchanged";
      case "set_mode":
        return e.detail.mode === "interactive" ? "Voice paused. You're shopping by clicks" : "Eliza is back. Voice is on";
      default: return null;
    }
  }

  function apply(s, quiet = false) {
    const prev = view.state;
    view.state = s;
    const fresh = s.events.filter((e) => e.n > view.lastN);
    if (view.lastN >= 0 && !quiet) {
      for (const e of fresh) {
        if (e.actor === "shopping") {
          const msg = voiceToast(e);
          if (msg) toast(msg, "voice");
        }
      }
    }
    if (fresh.length) view.lastN = Math.max(view.lastN, ...fresh.map((e) => e.n));
    if (view.lastN < 0 && s.events.length) view.lastN = Math.max(...s.events.map((e) => e.n));
    renderHeader(prev);
    renderMode();
    renderPage(prev);
    renderActivity();
  }

  // ------------------------------------------------------------------ header
  function renderHeader(prev) {
    const count = $("cart-count");
    const next = String(view.state.cart_count);
    if (count.textContent !== next) {
      count.textContent = next;
      count.classList.add("bump");
      setTimeout(() => count.classList.remove("bump"), 260);
    }
    document.querySelectorAll(".evs-nav a").forEach((a) => {
      a.classList.toggle("active", a.dataset.page === view.state.page);
    });
  }

  function renderMode() {
    const clicks = view.state.mode === "interactive";
    const bar = document.getElementById("modebar");
    bar.classList.toggle("clicks", clicks);
    $("mode-title").textContent = clicks ? "You're shopping by clicks" : "Eliza is listening";
    $("mode-sub").textContent = clicks
      ? "Voice is paused and your cart is kept. Say \"I'm back\" or press Resume voice."
      : "Speak to Eliza or keep shopping by clicks. Your cart is shared.";
    const btn = $("mode-toggle");
    btn.textContent = clicks ? "Resume voice" : "Shop by clicks";
    btn.dataset.action = clicks ? "resume" : "takeover";
  }

  // ------------------------------------------------------------------ pages
  function renderPage(prev) {
    const page = $("page");
    const s = view.state;
    if (!view.catalog) {
      page.replaceChildren(el("p", "evs-muted", "Loading the store…"));
      return;
    }
    const changed = !prev || prev.page !== s.page || prev.version !== s.version;
    if (!changed && page.childElementCount) return;
    const node = (() => {
      if (s.page === "home") return home();
      if (s.page.startsWith("category:")) return category(s.page.slice(9));
      if (s.page.startsWith("product:")) return detail(s.page.slice(8));
      if (s.page === "cart") return cart();
      if (s.page === "checkout") return checkout();
      if (s.page.startsWith("confirmation:")) return confirmation(s.page.slice(13));
      return home();
    })();
    page.replaceChildren(node);
    page.style.animation = "none";
    void page.offsetWidth;
    page.style.animation = "";
  }

  function tileVisual(p, big) {
    const c = CATS[p.category] || { color: "#0f766e", glyph: "EV" };
    const tile = el("div", "tile-img");
    tile.style.background = `linear-gradient(135deg, ${c.color}, ${shade(c.color)})`;
    tile.appendChild(el("span", "glyph", c.glyph));
    if (big) tile.style.height = "320px";
    return tile;
  }
  function shade(hex) {
    const n = parseInt(hex.slice(1), 16);
    const r = Math.max(0, (n >> 16) - 40), g = Math.max(0, ((n >> 8) & 255) - 40), b = Math.max(0, (n & 255) - 40);
    return `#${((r << 16) | (g << 8) | b).toString(16).padStart(6, "0")}`;
  }

  function productCard(p) {
    const card = el("article", "product-card");
    card.appendChild(tileVisual(p));
    const body = el("div", "product-body");
    const title = el("h3", null, p.name);
    title.style.cursor = "pointer";
    title.dataset.page = `product:${p.id}`;
    body.appendChild(title);
    body.appendChild(el("div", "product-meta", `${CATS[p.category].label} · ${p.weight_kg} kg`));
    body.appendChild(el("div", "price", money(p.price)));
    const stock = el("span", `stock ${p.stock > 0 ? "in" : "out"}`, p.stock > 0 ? "In stock" : "Out of stock");
    body.appendChild(stock);
    const actions = el("div", "product-actions");
    const add = el("button", "btn primary", p.stock > 0 ? "Add to cart" : "Unavailable");
    add.disabled = p.stock <= 0;
    add.dataset.add = p.id;
    actions.appendChild(add);
    body.appendChild(actions);
    card.appendChild(body);
    return card;
  }

  function home() {
    const wrap = el("div");
    const hero = el("section", "hero");
    hero.appendChild(el("p", "kicker", "Eliza Voice Shopping"));
    hero.appendChild(el("h1", null, "Gear for the next trip, chosen by voice or by hand."));
    hero.appendChild(el("p", null, "Ask Eliza for a recommendation, add it to your cart, and check out. Switch to clicking at any moment and your cart comes with you."));
    const row = el("div", "hero-actions");
    const start = el("button", "btn primary", "Browse all gear");
    start.dataset.page = "category:tent";
    row.appendChild(start);
    hero.appendChild(row);
    wrap.appendChild(hero);

    const head = el("div", "section-title");
    head.appendChild(el("h2", null, "Shop by category"));
    wrap.appendChild(head);
    const grid = el("div", "cat-grid");
    for (const [key, c] of Object.entries(CATS)) {
      const tile = el("button", "cat-tile");
      tile.dataset.page = `category:${key}`;
      tile.appendChild(el("div", "cat-icon", c.glyph));
      tile.lastChild.style.background = c.color;
      tile.appendChild(el("strong", null, c.label));
      const n = Object.values(view.catalog.products).filter((p) => p.category === key).length;
      tile.appendChild(el("span", null, `${n} products`));
      grid.appendChild(tile);
    }
    wrap.appendChild(grid);

    const feat = el("div", "section-title");
    feat.appendChild(el("h2", null, "Popular right now"));
    wrap.appendChild(feat);
    const products = el("div", "product-grid");
    for (const id of ["tent-01", "bag-03", "stove-01", "pack-01"]) products.appendChild(productCard(view.catalog.products[id]));
    wrap.appendChild(products);
    return wrap;
  }

  function category(key) {
    const wrap = el("div");
    const head = el("div", "section-title");
    head.appendChild(el("h2", null, CATS[key] ? CATS[key].label : "Gear"));
    head.appendChild(el("span", "evs-muted", "Prices include tax · Free returns within 30 days"));
    wrap.appendChild(head);
    const grid = el("div", "product-grid");
    for (const p of Object.values(view.catalog.products).filter((x) => x.category === key)) grid.appendChild(productCard(p));
    wrap.appendChild(grid);
    return wrap;
  }

  function detail(pid) {
    const p = product(pid);
    const wrap = el("div");
    const crumb = el("p", "evs-muted", "");
    const back = el("button", "btn ghost small", "← Back");
    back.dataset.page = `category:${p ? p.category : "tent"}`;
    back.style.marginBottom = "14px";
    wrap.appendChild(back);
    if (!p) {
      wrap.appendChild(el("p", null, "This product isn't in the catalogue."));
      return wrap;
    }
    crumb.textContent = `${CATS[p.category].label} / ${p.name}`;
    wrap.appendChild(crumb);
    const box = el("div", "product-detail");
    box.appendChild(tileVisual(p, true));
    const info = el("div");
    info.appendChild(el("h1", null, p.name));
    info.appendChild(el("div", "price", money(p.price)));
    const stock = el("span", `stock ${p.stock > 0 ? "in" : "out"}`, p.stock > 0 ? `In stock · ${p.stock} left` : "Out of stock");
    stock.style.marginTop = "8px";
    stock.style.display = "inline-block";
    info.appendChild(stock);
    const dl = el("dl", "spec");
    const rows = [["Capacity", p.capacity === 1 ? "1 person" : `${p.capacity} people`], ["Weight", `${p.weight_kg} kg`],
      ["Season rating", ["", "Summer", "Three-season", "Winter"][p.season - 1] || `${p.season}`], ["Category", CATS[p.category].label]];
    for (const [k, v] of rows) {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, v));
    }
    info.appendChild(dl);
    const add = el("button", "btn primary", p.stock > 0 ? "Add to cart" : "Unavailable");
    add.disabled = p.stock <= 0;
    add.dataset.add = p.id;
    info.appendChild(add);
    box.appendChild(info);
    wrap.appendChild(box);
    return wrap;
  }

  function cart() {
    const s = view.state;
    const wrap = el("div", "panel");
    const head = el("div", "section-title");
    head.appendChild(el("h2", null, "Your cart"));
    wrap.appendChild(head);
    if (!s.cart.length) {
      const empty = el("div", "empty-state");
      empty.appendChild(el("h2", null, "Your cart is empty"));
      empty.appendChild(el("p", null, "Ask Eliza for a recommendation, or browse the store."));
      const b = el("button", "btn primary", "Browse gear");
      b.dataset.page = "category:tent";
      b.style.marginTop = "14px";
      empty.appendChild(b);
      wrap.appendChild(empty);
      return wrap;
    }
    const table = el("table", "cart-table");
    const thead = el("thead");
    const hr = el("tr");
    for (const h of ["Item", "Qty", "Price", ""]) hr.appendChild(el("th", null, h));
    thead.appendChild(hr);
    table.appendChild(thead);
    const tbody = el("tbody");
    for (const line of s.cart) {
      const tr = el("tr");
      const name = el("td", null, line.name);
      name.style.cursor = "pointer";
      name.dataset.page = `product:${line.id}`;
      tr.appendChild(name);
      tr.appendChild(el("td", null, line.qty));
      tr.appendChild(el("td", null, money(line.line_total)));
      const td = el("td");
      const rm = el("button", "btn ghost small", "Remove");
      rm.dataset.remove = line.id;
      td.appendChild(rm);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    const total = el("div", "cart-total");
    total.appendChild(el("strong", null, `Total ${money(s.total)}`));
    const checkoutBtn = el("button", "btn primary", "Check out");
    checkoutBtn.dataset.checkout = "1";
    total.appendChild(checkoutBtn);
    wrap.appendChild(total);
    return wrap;
  }

  function checkout() {
    const s = view.state;
    const wrap = el("div");
    const head = el("div", "section-title");
    head.appendChild(el("h2", null, "Checkout"));
    wrap.appendChild(head);
    if (!s.cart.length) {
      wrap.appendChild(el("p", "evs-muted", "Your cart is empty."));
      return wrap;
    }
    const grid = el("div", "checkout");
    const left = el("div", "panel");
    left.appendChild(el("h2", null, "Delivery"));
    left.appendChild(el("p", null, s.address));
    left.appendChild(el("h2", null, "Payment"));
    left.appendChild(el("p", null, s.payment));
    left.appendChild(el("span", "pill-note", "Test payment · no money moves"));
    const right = el("div", "panel");
    right.appendChild(el("h2", null, "Order summary"));
    for (const line of s.cart) right.appendChild(el("div", "summary-line", `${line.name} × ${line.qty}  ${money(line.line_total)}`));
    right.appendChild(el("div", "summary-line total", `Total  ${money(s.total)}`));
    const place = el("button", "btn primary", s.pending === "place_order" ? "Confirm and place order" : "Place order");
    place.dataset.place = "1";
    place.style.width = "100%";
    place.style.justifyContent = "center";
    place.style.marginTop = "12px";
    right.appendChild(place);
    if (s.pending === "place_order") {
      const cancel = el("button", "btn ghost", "Not yet");
      cancel.dataset.cancel = "1";
      cancel.style.width = "100%";
      cancel.style.justifyContent = "center";
      cancel.style.marginTop = "8px";
      right.appendChild(cancel);
    }
    grid.appendChild(left);
    grid.appendChild(right);
    wrap.appendChild(grid);
    return wrap;
  }

  function confirmation(orderId) {
    const order = view.state.orders.find((o) => o.id === orderId);
    const wrap = el("div", "panel confirm");
    wrap.appendChild(el("div", "check", "✓"));
    wrap.appendChild(el("h1", null, "Thank you, your order is placed"));
    const id = el("p", null, "Order ");
    id.appendChild(el("span", "order-id", orderId));
    wrap.appendChild(id);
    if (order) {
      wrap.appendChild(el("p", "evs-muted", `${order.items.length} item${order.items.length === 1 ? "" : "s"} · total ${money(order.total)} · delivering to ${view.state.address}`));
    }
    const b = el("button", "btn primary", "Keep shopping");
    b.dataset.page = "home";
    b.style.marginTop = "18px";
    wrap.appendChild(b);
    return wrap;
  }

  // -------------------------------------------------------------- activity
  function renderActivity() {
    const list = $("activity");
    list.replaceChildren();
    const events = view.state.events.slice().reverse();
    if (!events.length) {
      list.appendChild(el("li", "evs-muted", "No activity yet."));
      return;
    }
    for (const e of events.slice(0, 22)) {
      const who = e.actor === "customer" ? "You" : "Eliza";
      const li = el("li", e.actor === "customer" ? "customer" : "voice");
      li.appendChild(el("span", "who", `${who} · ${e.ts}`));
      li.appendChild(el("span", "what", describe(e)));
      list.appendChild(li);
    }
  }

  function describe(e) {
    const d = e.detail || {};
    const pname = d.product && product(d.product) ? product(d.product).name : d.product;
    switch (e.action) {
      case "add_to_cart": return `added ${pname}`;
      case "remove_from_cart": return `removed ${pname}`;
      case "navigate": return `opened ${d.page}`;
      case "checkout": case "request_order": return "went to checkout";
      case "place_order": return "placed the order";
      case "cancel_order": return "did not place the order";
      case "takeover": case "set_mode": return d.mode === "interactive" || e.action === "takeover" ? "switched to clicking" : "switched to voice";
      case "resume": return "switched to voice";
      default: return e.action.replace(/_/g, " ");
    }
  }

  // ---------------------------------------------------------------- toasts
  function toast(text, kind) {
    const box = $("toasts");
    const t = el("div", "toast");
    if (kind === "voice") t.appendChild(el("span", "dot"));
    t.appendChild(el("span", null, text));
    box.appendChild(t);
    setTimeout(() => t.classList.add("fade"), 3600);
    setTimeout(() => t.remove(), 4100);
  }

  // ------------------------------------------------------------- wiring
  function connect() {
    if (EMBEDDED) return; // the monitor drives the page
    if (view.stream) view.stream.close();
    const es = new EventSource(`/shop/api/stream?sid=${encodeURIComponent(sid)}`);
    view.stream = es;
    es.addEventListener("state", (ev) => apply(JSON.parse(ev.data)));
  }

  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-page],[data-add],[data-remove],[data-checkout],[data-place],[data-cancel],#mode-toggle");
    if (!t) return;
    ev.preventDefault();
    if (t.dataset.page) return act("navigate", { page: t.dataset.page });
    if (t.dataset.add) return act("add", { product_id: t.dataset.add });
    if (t.dataset.remove) return act("remove", { product_id: t.dataset.remove });
    if (t.dataset.checkout) return act("checkout");
    if (t.dataset.place) return act("place_order");
    if (t.dataset.cancel) return act("cancel_order");
    if (t.id === "mode-toggle") return act(t.dataset.action || "takeover");
  });

  document.querySelectorAll(".evs-nav a").forEach((a) => a.dataset.page && a.addEventListener("click", (e) => e.preventDefault()));

  async function boot() {
    try {
      const cat = await api("/shop/api/catalog");
      view.catalog = cat;
      const s = await api(`/shop/api/state?sid=${encodeURIComponent(sid)}`);
      apply(s);
    } catch (err) {
      $("page").replaceChildren(el("p", "evs-muted", `The store could not load: ${err.message}`));
    }
    connect();
  }

  window.addEventListener("message", async (ev) => {
    if (!ev.data || ev.data.type !== "evs:sync" || !Number.isInteger(ev.data.version)) return;
    try {
      const s = await api(`/shop/api/state?sid=${encodeURIComponent(sid)}&at=${ev.data.version}`);
      apply(s, true);
    } catch (err) {
      /* keep the last good view */
    }
  });

  document.addEventListener("DOMContentLoaded", boot);
  window.EVS = { sid };
})();

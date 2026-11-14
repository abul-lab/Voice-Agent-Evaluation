/* Test Cases page: lists the scripted cases for each test type. Text is written with textContent only. */
(() => {
  "use strict";

  const TYPES = {
    smoke: {
      label: "Smoke",
      note: "One case: the Front Desk Specialist hands a shopper to the Shopping Agent, who recommends a tent. Run on every change.",
    },
    targeted: {
      label: "Targeted regression",
      note: "Three cases: the Front Desk Specialist with each other agent: the Shopping Agent (winter bag, then price), the Order Tracking Agent (order status after verification) and the Refunds Agent (refund within the window).",
    },
    regression: {
      label: "Full regression",
      note: "Everything in targeted regression, plus the Customer Care Agent, handoffs and storefront cases; guardrails (prompt injection, data leakage, wrong identity, card details, off-topic, abuse); driving safety holds; and background noise such as a passing car or rain. Run before a release.",
    },
  };

  const INTENTS = { recommend: "Recommendation", price: "Price", stock: "Stock", compare: "Compare", order: "Order status", refund: "Refund", add_to_cart: "Add to cart", cart: "Cart check", checkout: "Checkout", takeover: "Shop by clicks", resume: "Resume voice" };
  const OUTCOMES = {};

  const $ = (id) => document.getElementById(id);
  let cases = [];

  // "store_refund_approved_within_window" -> "Store refund approved within window"
  const prettyName = (id) => {
    const words = id.replace(/^(en|es)_/, "").split("_");
    return words.map((w, i) => (i ? w : w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
  };

  function cell(text, cls) {
    const td = document.createElement("td");
    if (cls) td.className = cls;
    td.textContent = text;
    return td;
  }

  function render(type) {
    const rows = $("case-rows");
    rows.replaceChildren();
    const selected = cases.filter((c) => c.test_types.includes(type));
    $("case-count").textContent = `${selected.length} case${selected.length === 1 ? "" : "s"}`;
    $("type-note").textContent = TYPES[type].note;

    if (!selected.length) {
      const tr = document.createElement("tr");
      const td = cell("No cases in this set yet.", "muted");
      td.colSpan = 5;
      tr.appendChild(td);
      rows.appendChild(tr);
      return;
    }

    for (const c of selected) {
      const tr = document.createElement("tr");

      const first = document.createElement("td");
      first.appendChild(Object.assign(document.createElement("div"), { className: "case-name", textContent: prettyName(c.id) }));
      first.appendChild(Object.assign(document.createElement("div"), { className: "case-id", textContent: c.id }));
      const tags = document.createElement("div");
      tags.style.marginTop = "6px";
      for (const t of c.test_types) {
        const pill = document.createElement("span");
        pill.className = `pill-type ${t}`;
        pill.textContent = TYPES[t] ? TYPES[t].label : t;
        tags.appendChild(pill);
      }
      first.appendChild(tags);
      tr.appendChild(first);

      tr.appendChild(cell(c.description || "—"));
      tr.appendChild(cell(`${INTENTS[c.intent] || c.intent}${c.language ? ` · ${c.language.toUpperCase()}` : ""}`));
      tr.appendChild(cell(OUTCOMES[c.expected_outcome] || c.expected_outcome || "—"));
      tr.appendChild(cell(c.max_turns ? `${c.max_turns} turns` : "—"));
      rows.appendChild(tr);
    }
  }

  function activate(type, updateHash = true) {
    if (!TYPES[type]) type = "smoke";
    for (const b of document.querySelectorAll(".subnav [role='tab']")) {
      b.setAttribute("aria-selected", String(b.dataset.type === type));
    }
    if (updateHash) history.replaceState(null, "", `#${type}`);
    render(type);
  }

  async function load() {
    try {
      const res = await fetch("/api/scenarios", { headers: { Accept: "application/json" } });
      const body = await res.json();
      cases = body.items;
    } catch (err) {
      cases = [];
      $("type-note").textContent = "Could not load test cases. Is the monitor server running?";
    }
    const initial = (location.hash || "#smoke").slice(1);
    activate(initial, false);
  }

  document.addEventListener("DOMContentLoaded", () => {
    for (const b of document.querySelectorAll(".subnav [role='tab']")) {
      b.addEventListener("click", () => activate(b.dataset.type));
    }
    window.addEventListener("hashchange", () => activate((location.hash || "#smoke").slice(1), false));
    load();
  });
})();

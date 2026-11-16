/* Execution history: every run, with a View page per execution. Text is written with textContent only. */
(() => {
  "use strict";

  // Step timings of an agent reply, stored as JSON text in the database.
  function stagesOf(turn) {
    let s = turn.stages;
    if (typeof s === "string") {
      try { s = JSON.parse(s); } catch (err) { return null; }
    }
    return s && typeof s === "object" ? s : null;
  }

  function stageText(s) {
    const v = (x) => (typeof x === "number" ? x.toFixed(1) : "–");
    return `understand ${v(s.understand_ms)} · route ${v(s.route_ms)} · dispatch ${v(s.dispatch_ms)} ms`;
  }

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  };
  const fmt = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  };
  const prettyName = (id) => id.replace(/^(en|es|store|evs)_/, "").split("_")
    .map((w, i) => (i ? w : w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
  const label = { running: "Running", passed: "Passed", failed: "Failed", error: "Error" };
  const outcomeOf = (r) => (r.summary && r.summary.outcome) || "—";
  const checksOf = (r) => {
    const c = (r.summary && r.summary.checks) || [];
    return { passed: c.filter((x) => x.passed).length, total: c.length, list: c };
  };

  // The API key is typed into this page when the server asks for it. It is held in memory only.
  let memoryKey = "";
  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    const typed = $("api-key") ? $("api-key").value.trim() : "";
    if (typed) memoryKey = typed;
    if (memoryKey) headers["X-API-Key"] = memoryKey;
    const res = await fetch(path, { ...options, headers });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const needsKey = res.status === 401 || res.status === 403;
      if (needsKey && $("key-field")) $("key-field").classList.remove("hidden");
      const note = needsKey ? " Enter the API key above, then press Refresh." : "";
      throw new Error((body.error ? `${body.error.code}: ${body.error.message}` : `HTTP ${res.status}`) + note);
    }
    return body;
  }

  // ------------------------------------------------------------- list view
  async function loadList() {
    const body = $("hist-body");
    try {
      const data = await api("/api/runs?limit=200");
      body.replaceChildren();
      $("hist-count").textContent = `${data.items.length} execution${data.items.length === 1 ? "" : "s"}`;
      if (!data.items.length) {
        const tr = document.createElement("tr");
        const td = el("td", "muted", "No executions yet. Run tests from the monitor and they will appear here.");
        td.colSpan = 8;
        tr.appendChild(td);
        body.appendChild(tr);
        return;
      }
      for (const r of data.items) {
        const tr = document.createElement("tr");
        tr.appendChild(el("td", "mono", r.id));
        tr.appendChild(el("td", null, prettyName(r.scenario_id)));
        tr.appendChild(el("td", "small", r.source));
        tr.appendChild(el("td", "small", fmt(r.created_at)));
        const st = el("td");
        st.appendChild(el("span", `badge ${r.status}`, label[r.status] || r.status));
        tr.appendChild(st);
        tr.appendChild(el("td", "small", outcomeOf(r)));
        const c = checksOf(r);
        tr.appendChild(el("td", "small", c.total ? `${c.passed}/${c.total}` : "—"));
        const act = el("td");
        const view = el("button", "btn ghost small", "View");
        view.addEventListener("click", () => {
          history.pushState(null, "", `/history?run=${encodeURIComponent(r.id)}`);
          route();
        });
        act.appendChild(view);
        tr.appendChild(act);
        body.appendChild(tr);
      }
    } catch (err) {
      $("hist-msg").textContent = `Could not load history (${err.message}).`;
    }
  }

  async function clearHistory() {
    const msg = $("hist-msg");
    if (!window.confirm("Clear all execution history? This removes every run and its conversation. The audit trail is kept. This cannot be undone.")) return;
    try {
      const data = await api("/api/history/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      msg.textContent = `Cleared ${data.removed} execution${data.removed === 1 ? "" : "s"}.`;
      await loadList();
    } catch (err) {
      msg.textContent = `Could not clear history: ${err.message}`;
    }
  }

  // ----------------------------------------------------------- detail view
  async function loadDetail(runId) {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    $("det-title").textContent = prettyName(run.scenario_id);
    $("det-id").textContent = run.id;
    const st = $("det-status");
    st.textContent = label[run.status] || run.status;
    st.className = `badge ${run.status}`;

    const meta = $("det-meta");
    meta.replaceChildren();
    const rows = [["Outcome", outcomeOf(run)], ["Source", run.source], ["Understanding", run.mode],
      ["Started", fmt(run.created_at)], ["Finished", fmt(run.finished_at)], ["Batch", run.batch_id || "—"]];
    for (const [k, v] of rows) {
      meta.appendChild(el("dt", null, k));
      meta.appendChild(el("dd", null, v));
    }

    const turns = (await api(`/api/runs/${encodeURIComponent(runId)}/turns`)).items;
    $("det-turns").textContent = `${turns.length} turn${turns.length === 1 ? "" : "s"}`;
    const conv = $("det-conversation");
    conv.replaceChildren();
    if (!turns.length) conv.appendChild(el("p", "empty", "No conversation was recorded."));
    for (const t of turns) {
      const wrap = el("div", `turn ${t.speaker}`);
      const meta2 = el("div", "turn-meta");
      const HISTORY_AGENT = {
        router: "Front Desk Specialist", shopping: "Shopping Agent", order: "Order Tracking Agent",
        refund: "Refunds Agent", care: "Customer Care Agent",
      };
      meta2.appendChild(el("span", "tag", t.speaker === "customer" ? "Customer" : (HISTORY_AGENT[t.agent] || "Voice agent")));
      const steps = stagesOf(t);
      if (steps) meta2.appendChild(el("span", "tag timing", stageText(steps)));
      if (t.act) meta2.appendChild(el("span", "tag act", t.act));
      wrap.appendChild(meta2);
      wrap.appendChild(el("div", "bubble", t.text_spoken));
      conv.appendChild(wrap);
    }

    const c = checksOf(run);
    $("det-checks-count").textContent = c.total ? `${c.passed}/${c.total} passed` : "";
    const list = $("det-checks");
    list.replaceChildren();
    if (!c.list.length) list.appendChild(el("li", "muted", "No checks recorded."));
    for (const ch of c.list) {
      const li = el("li", ch.passed ? "pass" : "fail");
      li.appendChild(el("span", "name", `${ch.passed ? "✓" : "✗"} ${ch.name.replace(/_/g, " ")}`));
      if (ch.detail) li.appendChild(el("span", "detail", ch.detail));
      list.appendChild(li);
    }

    const shop = $("det-shop");
    shop.replaceChildren();
    try {
      const s = await api(`/shop/api/state?sid=${encodeURIComponent(runId)}`);
      const orders = s.orders || [];
      const rowsS = [["Orders placed", orders.length ? orders.map((o) => `${o.id} · $${o.total}`).join(", ") : "None"],
        ["Cart at the end", s.cart_count ? s.cart.map((l) => `${l.name} × ${l.qty}`).join(", ") : "Empty"]];
      for (const [k, v] of rowsS) {
        shop.appendChild(el("dt", null, k));
        shop.appendChild(el("dd", null, v));
      }
    } catch (err) {
      shop.appendChild(el("dt", null, "Storefront"));
      shop.appendChild(el("dd", null, "Not available for this execution."));
    }

    const audit = (await api(`/api/runs/${encodeURIComponent(runId)}/audit`)).items;
    $("det-audit-count").textContent = `${audit.length} event${audit.length === 1 ? "" : "s"}`;
    const ab = $("det-audit");
    ab.replaceChildren();
    for (const a of audit) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", "small", fmt(a.ts)));
      tr.appendChild(el("td", null, a.actor));
      tr.appendChild(el("td", "mono small", a.action));
      tr.appendChild(el("td", "small", a.entity_id || "—"));
      ab.appendChild(tr);
    }
  }

  // ---------------------------------------------------------------- routing
  function route() {
    const runId = new URLSearchParams(location.search).get("run");
    const listView = $("list-view");
    const detailView = $("detail-view");
    if (runId) {
      listView.classList.add("hidden");
      detailView.classList.remove("hidden");
      loadDetail(runId).catch((err) => {
        $("det-title").textContent = "Execution not found";
        $("det-id").textContent = err.message;
      });
    } else {
      detailView.classList.add("hidden");
      listView.classList.remove("hidden");
      loadList();
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    $("hist-clear").addEventListener("click", clearHistory);
    $("hist-refresh").addEventListener("click", loadList);
    window.addEventListener("popstate", route);
    route();
  });
})();

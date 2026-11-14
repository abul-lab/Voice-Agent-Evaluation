/* Agent network: live view of who is speaking, handoffs, verification and tool calls.
   Every label is written with textContent or as an SVG text node; nothing is parsed as HTML. */
(() => {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const HOP_SLOW_MS = 620;  // comfortable pace when the picture is current
const HOP_FAST_MS = 170;  // used when messages arrive faster than they can be drawn
  const DISCLOSING = new Set(["order_status", "refund_approved", "refund_refused"]);
  const SPECIALISTS = ["shopping", "order", "refund", "care"];
  const LABEL = { customer: "Customer", conductor: "Front Desk Specialist", shopping: "Shopping Agent",
                  order: "Order Tracking Agent", refund: "Refunds Agent", care: "Customer Care Agent",
                  voice: "Voice agent", tools: "Tools" };
  const COLOR = { customer: "#38bdf8", conductor: "#a78bfa", shopping: "#34d399", order: "#fbbf24",
                  refund: "#f472b6", care: "#f97316", voice: "#a78bfa", tools: "#94a3b8" };
  const POS = {
    customer: { x: 120, y: 200 },
    conductor: { x: 420, y: 200 },
    shopping: { x: 760, y: 50 },
    order: { x: 760, y: 150 },
    refund: { x: 760, y: 250 },
    care: { x: 760, y: 350 },
  };

  const $ = (id) => document.getElementById(id);
  const svgEl = (tag, attrs = {}, parent) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    if (parent) parent.appendChild(n);
    return n;
  };
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const clip = (t, n) => (t && t.length > n ? `${t.slice(0, n - 1)}…` : t || "");

  const state = {
    mode: "multi",            // "multi" = store specialists, "single" = one voice agent
    scopes: {},
    stats: {},
    status: {},               // per specialist: verification / outcome line
    lastSpecialist: null,
    active: null,     // the agent engaged in the conversation (steady highlight)
    speaking: null,   // the agent speaking right now (blinks); null while the customer speaks
    cards: null,
    queue: [],
    busy: false,
    nodes: {},
    edges: {},
    lanes: null,
    laneCount: {},
  };

  // ------------------------------------------------------------------ build
  function build() {
    const svg = $("net-svg");
    svg.replaceChildren();
    const defs = svgEl("defs", {}, svg);
    const glow = svgEl("filter", { id: "glow", x: "-50%", y: "-50%", width: "200%", height: "200%" }, defs);
    svgEl("feGaussianBlur", { stdDeviation: "4", result: "b" }, glow);
    const merge = svgEl("feMerge", {}, glow);
    svgEl("feMergeNode", { in: "b" }, merge);
    svgEl("feMergeNode", { in: "SourceGraphic" }, merge);
    const grad = svgEl("linearGradient", { id: "trail", x1: "0", y1: "0", x2: "1", y2: "0" }, defs);
    svgEl("stop", { offset: "0", "stop-color": "#ffffff", "stop-opacity": "0" }, grad);
    svgEl("stop", { offset: "1", "stop-color": "#ffffff", "stop-opacity": "0.9" }, grad);
    const pattern = svgEl("pattern", { id: "dots", width: "22", height: "22", patternUnits: "userSpaceOnUse" }, defs);
    svgEl("circle", { cx: "2", cy: "2", r: "1.1", fill: "#334155" }, pattern);

    svgEl("rect", { width: "960", height: "400", fill: "url(#dots)", opacity: "0.6" }, svg);
    const edges = svgEl("g", { class: "edges" }, svg);
    const particles = svgEl("g", { class: "particles" }, svg);

    // edges: customer -> conductor (straight), conductor -> specialists (curves)
    state.edges.customer = svgEl("path", { class: "edge", d: `M${POS.customer.x},${POS.customer.y} L${POS.conductor.x},${POS.conductor.y}`, id: "e-customer" }, edges);
    for (const s of SPECIALISTS) {
      const p = POS[s];
      const c = POS.conductor;
      state.edges[s] = svgEl("path", {
        class: "edge spec", id: `e-${s}`,
        d: `M${c.x},${c.y} C${c.x + 150},${c.y} ${p.x - 170},${p.y} ${p.x},${p.y}`,
      }, edges);
    }

    state.particles = particles;
    state.nodes.customer = nodeGroup(svg, "customer", POS.customer, "C");
    state.nodes.conductor = nodeGroup(svg, "conductor", POS.conductor, "◎");
    const GLYPH = { shopping: "S", order: "O", refund: "R", care: "H" };
    for (const s of SPECIALISTS) state.nodes[s] = nodeGroup(svg, s, POS[s], GLYPH[s]);
    state.nodes.voice = null;
    setMode("multi");
  }

  function nodeGroup(svg, id, p, glyph) {
    const g = svgEl("g", { class: `node node-${id}`, transform: `translate(${p.x},${p.y})` }, svg);
    svgEl("circle", { class: "ring", r: "44", fill: "none", stroke: COLOR[id] || "#94a3b8" }, g);
    svgEl("circle", { class: "halo", r: "38", fill: COLOR[id] || "#94a3b8", opacity: "0.12" }, g);
    svgEl("circle", { class: "core", r: "30", fill: "#0f172a", stroke: COLOR[id] || "#94a3b8", "stroke-width": "2" }, g);
    const glyphText = svgEl("text", { class: "glyph", "text-anchor": "middle", y: "6", fill: COLOR[id] || "#e2e8f0" }, g);
    glyphText.textContent = glyph;
    const label = svgEl("text", { class: "label", "text-anchor": "middle", y: "58" }, g);
    label.textContent = LABEL[id];
    const sub = svgEl("text", { class: "sub", "text-anchor": "middle", y: "74" }, g);
    sub.textContent = "";
    const bubble = svgEl("g", { class: "bubble", opacity: "0" }, g);
    svgEl("rect", { class: "bubble-box", x: "-120", y: "-92", width: "240", height: "30", rx: "9" }, bubble);
    const bubbleText = svgEl("text", { class: "bubble-text", "text-anchor": "middle", x: "0", y: "-72" }, bubble);
    const sat = svgEl("g", { class: "sat", transform: "translate(52,-20)" }, g);
    return { g, label, sub, bubble, bubbleText, sat, glyph: glyphText, satItems: [] };
  }

  // ------------------------------------------------------------------ modes
  function setMode(mode) {
    state.mode = mode;
    const svg = $("net-svg");
    svg.classList.toggle("single", mode === "single");
    for (const s of SPECIALISTS) {
      state.nodes[s].g.style.display = mode === "single" ? "none" : "";
      state.edges[s].style.display = mode === "single" ? "none" : "";
    }
    const center = state.nodes.conductor;
    center.label.textContent = mode === "single" ? LABEL.voice : LABEL.conductor;
    center.glyph.textContent = mode === "single" ? "V" : "◎";
    $("roster").classList.toggle("hidden", mode === "single");
    $("timeline-title").textContent = mode === "single" ? "Turn timeline" : "Agent timeline";
  }

  // ------------------------------------------------------------- animation
  function pointAt(path, t) {
    const len = path.getTotalLength();
    return path.getPointAtLength(Math.max(0, Math.min(1, t)) * len);
  }

  function edgeFor(from, to) {
    // Find the drawn edge joining two nodes and report which way the message travels along it.
    if ((from === "customer" && to === "conductor") || (from === "conductor" && to === "customer")) {
      return { path: state.edges.customer, forward: from === "customer" };
    }
    if (from === "conductor" && SPECIALISTS.includes(to)) return { path: state.edges[to], forward: true };
    if (SPECIALISTS.includes(from) && to === "conductor") return { path: state.edges[from], forward: false };
    return null;
  }

  // Adapt the animation speed to the backlog so the network never falls far behind the transcript.
  function hopDuration() {
    const backlog = state.queue.length;
    if (backlog >= 6) return HOP_FAST_MS;
    if (backlog >= 3) return Math.round((HOP_SLOW_MS + HOP_FAST_MS) / 2);
    return HOP_SLOW_MS;
  }

  function hop(from, to, color) {
    return new Promise((resolve) => {
      const HOP_MS = hopDuration();
      const e = edgeFor(from, to);
      if (!e || e.path.style.display === "none") return resolve();
      const dot = svgEl("circle", { r: "7", fill: color, filter: "url(#glow)" }, state.particles);
      const trail = [0.04, 0.08, 0.13].map((d) => svgEl("circle", { r: "4", fill: color, opacity: "0.35" }, state.particles));
      const start = performance.now();
      const step = (now) => {
        const progress = Math.min(1, (now - start) / HOP_MS);
        // Position along the edge; the trail sits behind the head of the message.
        const at = (pr) => pointAt(e.path, e.forward ? pr : 1 - pr);
        const head = at(progress);
        dot.setAttribute("cx", head.x);
        dot.setAttribute("cy", head.y);
        trail.forEach((c, i) => {
          const tail = at(Math.max(0, progress - (i + 1) * 0.06));
          c.setAttribute("cx", tail.x);
          c.setAttribute("cy", tail.y);
        });
        if (progress < 1) return requestAnimationFrame(step);
        dot.remove();
        trail.forEach((c) => c.remove());
        flash(to);
        resolve();
      };
      requestAnimationFrame(step);
      e.path.classList.add("lit");
      setTimeout(() => e.path.classList.remove("lit"), HOP_SLOW_MS + 500);
    });
  }

  function flash(id) {
    const n = state.nodes[id];
    if (!n) return;
    n.g.classList.add("pulse");
    setTimeout(() => n.g.classList.remove("pulse"), 700);
  }

  function enqueue(hops) {
    state.queue.push(...hops);
    if (!state.busy) drain();
  }

  async function drain() {
    state.busy = true;
    while (state.queue.length) {
      const h = state.queue.shift();
      if (h.setup) h.setup();
      await hop(h.from, h.to, h.color);
      await new Promise((r) => setTimeout(r, state.queue.length >= 6 ? 20 : 90));
    }
    state.busy = false;
  }

  function bubble(id, text) {
    const n = state.nodes[id];
    if (!n) return;
    n.bubbleText.textContent = clip(text, 42);
    n.bubble.setAttribute("opacity", "1");
    clearTimeout(n.hideTimer);
    n.hideTimer = setTimeout(() => n.bubble.setAttribute("opacity", "0"), 2800);
  }

  function handoff(to) {
    const c = POS.conductor;
    const ring = svgEl("circle", { cx: c.x, cy: c.y, r: "30", fill: "none", stroke: COLOR[to] || "#fff", "stroke-width": "2" }, state.particles);
    ring.animate([{ r: 30, opacity: 0.9 }, { r: 110, opacity: 0 }], { duration: 900, easing: "ease-out" });
    setTimeout(() => ring.remove(), 950);
    const target = POS[to];
    const mx = (c.x + target.x) / 2;
    const my = (c.y + target.y) / 2 - 18;
    const tag = svgEl("g", { class: "handoff-tag", transform: `translate(${mx},${my})` }, state.particles);
    svgEl("rect", { x: "-82", y: "-14", width: "164", height: "24", rx: "12", fill: COLOR[to] || "#fff", opacity: "0.95" }, tag);
    const txt = svgEl("text", { "text-anchor": "middle", y: "3", fill: "#0b1220", "font-size": "11", "font-weight": "700" }, tag);
    txt.textContent = `HANDOFF → ${LABEL[to]}`;
    tag.animate([{ opacity: 0, transform: `translate(${mx}px,${my + 8}px)` }, { opacity: 1, offset: 0.15 },
      { opacity: 1, offset: 0.7 }, { opacity: 0 }], { duration: 2400, easing: "ease-out", fill: "forwards" });
    setTimeout(() => tag.remove(), 2450);
  }

  // ----------------------------------------------------------- satellites
  function satellite(id, text, kind) {
    const n = state.nodes[id];
    if (!n) return;
    const colors = { ok: "#34d399", bad: "#f87171", warn: "#fbbf24", info: "#60a5fa" };
    const row = n.satItems.length;
    const g = svgEl("g", { transform: `translate(0,${row * 30})`, opacity: "0" }, n.sat);
    const color = colors[kind] || colors.info;
    svgEl("rect", { x: "0", y: "-12", width: "190", height: "24", rx: "8", fill: "#0b1220", stroke: color, "stroke-width": "1.2" }, g);
    svgEl("circle", { cx: "12", cy: "0", r: "4", fill: color }, g);
    const t = svgEl("text", { x: "24", y: "4", fill: "#e2e8f0", "font-size": "11" }, g);
    t.textContent = clip(text, 26);
    g.animate([{ opacity: 0, transform: `translate(14px,${row * 30}px)` }, { opacity: 1, transform: `translate(0,${row * 30}px)` }],
      { duration: 380, easing: "ease-out", fill: "forwards" });
    n.satItems.push(g);
    if (n.satItems.length > 3) {
      const old = n.satItems.shift();
      old.remove();
      n.satItems.forEach((item, i) => item.setAttribute("transform", `translate(0,${i * 30})`));
    }
    if (kind === "bad") {
      n.g.classList.add("shake");
      setTimeout(() => n.g.classList.remove("shake"), 500);
    }
  }

  // ------------------------------------------------------------- roster/stats
  function bumpStat(key, delta = 1) {
    state.stats[key] = (state.stats[key] || 0) + delta;
  }

  function renderStats() {
    const s = state.stats;
    const chips = [
      ["Turns", s.turns || 0], ["Handoffs", s.handoffs || 0],
      ["Tool calls", s.tools || 0], ["Denied", s.denied || 0],
    ];
    const box = $("net-stats");
    box.replaceChildren();
    for (const [label, value] of chips) {
      const chip = document.createElement("span");
      chip.className = "stat-chip" + (label === "Denied" && value ? " alarm" : "");
      const v = document.createElement("strong");
      v.textContent = String(value);
      chip.appendChild(v);
      chip.appendChild(document.createTextNode(` ${label}`));
      box.appendChild(chip);
    }
    if (state.mode === "multi") renderRoster();
  }

  const ROLES = {
    conductor: ["Front Desk Specialist", "Routes every turn to one specialist"],
    shopping: ["Shopping Agent", "Products, prices, stock, cart and checkout"],
    order: ["Order Tracking Agent", "Status after identity check"],
    refund: ["Refunds Agent", "Policy decision after identity check"],
    care: ["Customer Care Agent", "Complaints, store information, handoff to a person"],
  };

  // The cards are built once and updated in place, so the blink keeps its rhythm.
  function ensureCards() {
    if (state.cards) return;
    const roster = $("roster");
    roster.replaceChildren();
    state.cards = {};
    for (const id of Object.keys(ROLES)) {
      const card = document.createElement("div");
      card.className = "agent-card";
      card.style.setProperty("--accent", COLOR[id]);
      const head = document.createElement("div");
      head.className = "agent-head";
      head.appendChild(el("span", "agent-dot"));
      head.appendChild(el("strong", null, ROLES[id][0]));
      card.appendChild(head);
      card.appendChild(el("p", "agent-role", ROLES[id][1]));
      const scopes = el("div", "scopes");
      card.appendChild(scopes);
      const meta = el("div", "agent-meta");
      card.appendChild(meta);
      const status = el("div", "agent-status");
      card.appendChild(status);
      roster.appendChild(card);
      state.cards[id] = { card, scopes, meta, status, scopeKey: null };
    }
  }

  function updateCard(id) {
    const c = state.cards[id];
    const granted = state.scopes[id] || [];
    const key = granted.join(",");
    if (c.scopeKey !== key) {
      c.scopeKey = key;
      c.scopes.replaceChildren();
      for (const scope of granted) c.scopes.appendChild(el("span", "scope", scope));
    }
    const st = state.stats[id] || {};
    c.meta.textContent = `${st.turns || 0} turns · ${st.tools || 0} tool calls${st.denied ? ` · ${st.denied} denied` : ""}`;
    c.status.textContent = state.status[id] || "idle";
    c.card.className = `agent-card${state.active === id ? " live" : ""}${state.speaking === id ? " speaking" : ""}`;
  }

  function renderRoster() {
    ensureCards();
    for (const id of Object.keys(ROLES)) updateCard(id);
  }

  // ------------------------------------------------------------- timeline
  function buildLanes() {
    const lanes = state.mode === "single" ? ["customer", "voice"] : ["customer", "conductor", "shopping", "order", "refund", "care", "tools"];
    const box = $("lanes");
    box.replaceChildren();
    state.laneCount = {};
    state.lanes = {};
    for (const id of lanes) {
      const row = document.createElement("div");
      row.className = "lane";
      const label = document.createElement("div");
      label.className = "lane-label";
      label.style.setProperty("--accent", COLOR[id]);
      label.textContent = LABEL[id];
      const track = document.createElement("div");
      track.className = "lane-track";
      row.appendChild(label);
      row.appendChild(track);
      box.appendChild(row);
      state.lanes[id] = track;
      state.laneCount[id] = 0;
    }
    state.eventIndex = 0;
  }

  function laneDot(laneId, title, color, kind) {
    const track = state.lanes && state.lanes[laneId];
    if (!track) return;
    const x = state.eventIndex * 48 + 16;
    const dot = document.createElement("span");
    dot.className = `lane-dot ${kind || ""}`;
    dot.style.left = `${x}px`;
    dot.style.setProperty("--dot", color);
    dot.title = title;
    track.appendChild(dot);
    track.style.minWidth = `${x + 60}px`;
    const wrap = $("lanes");
    wrap.scrollLeft = wrap.scrollWidth;
    state.laneCount[laneId] += 1;
    for (const id of Object.keys(state.lanes)) {
      if (state.lanes[id] !== track) state.lanes[id].style.minWidth = `${x + 60}px`;
    }
  }

  // ----------------------------------------------------------- event intake
  function specialistStatus(agent, turn) {
    const act = turn.act || "";
    if (DISCLOSING.has(act)) return act === "order_status" ? "verified · status shared" :
      act === "refund_approved" ? "verified · refund approved" : "verified · refund refused";
    if (act === "verify_failed") return "verification failed";
    if (act === "ask_order" || act === "ask_email") return "verifying identity…";
    if (act === "escalate") return "escalated · VERIFY_FAILED";
    if (act === "service_error") return "system fault · retrying";
    if (act === "recommend" || act === "answer_price" || act === "answer_stock" || act === "compare") return "answered";
    if (act === "out_of_stock" || act === "no_match") return "no match in stock";
    return "active";
  }

  function turnBubble(turn) {
    return clip(turn.text_spoken || "", 42);
  }

  // Who is speaking right now. The playback in app.js calls this when a piece of speech really starts
  // (voice on) or when its paced time starts (voice off). A customer line passes null, so no agent blinks.
  // The blink stays on that agent until the next speaker starts.
  function speak(who) {
    const id = who === "router" ? "conductor" : who;
    state.speaking = id ? (state.mode === "single" ? "voice" : id) : null;
    if (state.speaking && SPECIALISTS.includes(state.speaking)) state.active = state.speaking;
    renderRoster();
  }

  function onTurn(turn, animate = true) {
    if (!state.lanes) buildLanes();
    const isAgent = turn.speaker === "agent";
    const agentId = isAgent ? (state.mode === "single" ? "voice" : (turn.agent && turn.agent !== "router" ? turn.agent : "conductor")) : "customer";
    const laneId = isAgent ? (state.mode === "single" ? "voice" : (agentId === "conductor" ? "conductor" : agentId)) : "customer";
    const kind = isAgent ? "agent" : "customer";
    if (isAgent) state.active = agentId === "conductor" || agentId === "voice" ? state.active || agentId : agentId;
    const title = `${LABEL[agentId] || "Agent"} · ${turn.act || (isAgent ? "reply" : "caller")}\n${turn.text_spoken}`;
    state.eventIndex += 1;
    laneDot(laneId, title, COLOR[agentId] || "#e2e8f0", kind);
    bumpStat("turns");

    if (!isAgent) {
      if (animate) {
        enqueue([{ from: "customer", to: "conductor", color: COLOR.customer,
          setup: () => bubble("customer", turnBubble(turn)) }]);
      }
      renderStats();
      return;
    }

    const specialist = agentId !== "conductor" && agentId !== "voice" ? agentId : null;
    if (specialist) {
      if (state.lastSpecialist && state.lastSpecialist !== specialist && animate) bumpStat("handoffs");
      if (state.lastSpecialist !== specialist && animate) handoff(specialist);
      state.lastSpecialist = specialist;
      state.status[specialist] = specialistStatus(specialist, turn);
      state.stats[specialist] = state.stats[specialist] || { turns: 0, tools: 0, denied: 0 };
      state.stats[specialist].turns += 1;
    } else {
      state.status.conductor = state.mode === "single" ? "answering" : "routing";
      state.stats.conductor = state.stats.conductor || { turns: 0, tools: 0, denied: 0 };
      state.stats.conductor.turns += 1;
    }
    if (animate) {
      const hops = [];
      if (specialist) hops.push({ from: "conductor", to: specialist, color: COLOR[specialist],
        setup: () => bubble("conductor", `→ ${LABEL[specialist]}`) });
      hops.push({ from: specialist || "conductor", to: "conductor", color: COLOR[specialist || "conductor"],
        setup: () => bubble(specialist || "conductor", turnBubble(turn)) });
      hops.push({ from: "conductor", to: "customer", color: COLOR[specialist || "conductor"] });
      enqueue(hops);
    }
    renderStats();
  }

  function onTool(entry, animate = true) {
    if (!state.lanes) buildLanes();
    const action = entry.action || "";
    const actor = entry.actor || "";
    const detail = entry.detail || {};
    const relevant = ["tool.lookup_order", "tool.denied", "tool.fault", "refund.approved", "refund.refused", "refund.duplicate"];
    if (!relevant.includes(action) || state.mode === "single") return;

    let text, kind, title;
    if (action === "tool.lookup_order") {
      kind = detail.found ? "ok" : "warn";
      text = `${detail.found ? "lookup" : "not found"} · ${entry.entity_id}`;
    } else if (action === "tool.denied") {
      kind = "bad";
      text = `DENIED · ${detail.scope}`;
    } else if (action === "tool.fault") {
      kind = "warn";
      text = `timeout · ${entry.entity_id}`;
    } else if (action === "refund.approved") {
      kind = "ok";
      text = `approved · $${detail.amount}`;
    } else if (action === "refund.refused") {
      kind = "bad";
      text = `refused · ${detail.rule_id}`;
    } else {
      kind = "info";
      text = `duplicate blocked · ${entry.entity_id}`;
    }
    title = `${action} · ${actor} · ${entry.entity_id}`;
    state.eventIndex += 1;
    const color = kind === "ok" ? "#34d399" : kind === "bad" ? "#f87171" : kind === "warn" ? "#fbbf24" : "#60a5fa";
    laneDot("tools", title, color, `tool ${kind}`);
    bumpStat("tools");
    if (kind === "bad") bumpStat("denied");
    const owner = SPECIALISTS.includes(actor) ? actor : "conductor";
    state.stats[owner] = state.stats[owner] || { turns: 0, tools: 0, denied: 0 };
    state.stats[owner].tools += 1;
    if (kind === "bad") state.stats[owner].denied += 1;
    if (animate) {
      enqueue([{ from: owner === "conductor" ? "conductor" : owner, to: owner, color,
        setup: () => satellite(owner, text, kind) }]);
    } else {
      satellite(owner, text, kind);
    }
    renderStats();
  }

  // ------------------------------------------------------------ run control
  function reset(run) {
    state.queue = [];
    state.busy = false;
    state.stats = {};
    state.status = {};
    state.lastSpecialist = null;
    state.active = null;
    state.speaking = null;
    for (const id of Object.keys(state.nodes)) {
      const n = state.nodes[id];
      if (!n) continue;
      n.satItems.forEach((g) => g.remove());
      n.satItems = [];
      n.bubble.setAttribute("opacity", "0");
    }
    // Every run is a store run now. The run source is the caller's name, so it cannot say which view to use.
    setMode("multi");
    buildLanes();
    renderStats();
    if (state.mode === "multi") renderRoster();
    $("net-live").textContent = run && run.status === "running" ? "LIVE" : "REPLAY";
    $("net-live").className = `live-pill ${run && run.status === "running" ? "on" : ""}`;
    $("net-title").textContent = run ? run.scenario_id : "Agent network";
  }

  // Finished runs: rebuild the picture instantly, in time order.
  function replay(events) {
    for (const e of events) {
      if (e.kind === "turn") onTurn(e.data, false);
      else onTool(e.data, false);
    }
    renderStats();
  }

  function setScopes(map) {
    state.scopes = map || {};
  }

  function init() {
    build();
    buildLanes();
    renderStats();
    renderRoster();
  }

  window.AgentView = { init, reset, onTurn, speak, onTool, replay, setScopes, setMode, get mode() { return state.mode; } };
  document.addEventListener("DOMContentLoaded", init);
})();

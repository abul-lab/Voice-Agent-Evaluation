/* Eliza Voice Shopping monitor. All text is written with textContent; nothing is parsed as HTML. */
(() => {
  "use strict";

  // A missing element must never break the page: fall back to a detached node.
  const $ = (id) => document.getElementById(id) || document.createElement("span");
  const state = {
    runs: [],
    selected: null,
    stream: null,
    batch: null,
    userPicked: false,
    keyRequired: false,
    totalScenarios: null,
    steps: 0,
    turns: [],
    startedHere: false,
    key: "",
    syncVersion: null,
    expanded: false,
    playing: false,
    runningBatch: null,
    liveQueue: [],
    liveBusy: false,
    storefrontUrl: null,
    epoch: 0, // bumped by every Stop, Reset and page close; anything started under an older epoch is dropped
  };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // The API key is needed to read run records (and to change anything). It is kept for this browser tab only.
  const KEY_STORE = "evs.apiKey";
  function apiKey() {
    const typed = $("api-key") ? $("api-key").value.trim() : "";
    if (typed) return typed;
    try { return sessionStorage.getItem(KEY_STORE) || ""; } catch (err) { return ""; }
  }
  function rememberKey(key) {
    try { if (key) sessionStorage.setItem(KEY_STORE, key); } catch (err) { /* storage can be blocked */ }
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    const key = apiKey();
    if (key) headers["X-API-Key"] = key;
    const res = await fetch(path, { ...options, headers });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = body && body.error ? `${body.error.code}: ${body.error.message}` : `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return body;
  }

  const fmt = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  };
  // Specialist names for multi-agent runs; single-agent turns keep the generic label.
  // Who is speaking, as shown on each transcript turn. "router" is the front desk.
  const AGENT_LABELS = {
    router: "Front Desk Specialist", shopping: "Shopping Agent", order: "Order Tracking Agent",
    refund: "Refunds Agent", care: "Customer Care Agent",
  };
  const outcomeOf = (run) => (run.summary && run.summary.outcome) || "";
  const humanStatus = (run) => ({ running: "Running", passed: "Passed", failed: "Failed", error: "Error" }[run.status] || run.status);
  // "store_refund_approved_within_window" -> "Store refund approved within window"
  const prettyName = (id) => {
    const words = id.replace(/^(en|es|store|evs)_/, "").split("_");
    return words.map((w, i) => (i ? w : w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
  };

  // ------------------------------------------------------------- capabilities
  // Capabilities say whether Gemini can run and whether a key is needed. No secrets travel.
  async function loadSettings() {
    try {
      const s = await api("/api/settings");
      state.storefrontUrl = s.storefront_url;
    } catch (err) {
      state.storefrontUrl = null; // fall back to the default storefront
    }
  }

  async function loadCapabilities() {
    try {
      const caps = await api("/api/capabilities");
      state.keyRequired = caps.api_key_required;
      const geminiOption = document.querySelector('#mode-select option[value="gemini"]');
      if (geminiOption && !caps.gemini_routing) {
        geminiOption.disabled = true;
        geminiOption.textContent = "Gemini (not configured)";
      }
      if (caps.agents && window.AgentView) window.AgentView.setScopes(caps.agents);
      $("key-field").classList.toggle("hidden", !caps.api_key_required);
    } catch (err) {
      $("control-msg").textContent = "Monitor API unreachable.";
    }
  }

  async function loadScenarioCount() {
    try {
      const data = await api("/api/scenarios");
      state.scenarioItems = data.items;
      state.totalScenarios = data.items.length;
    } catch (err) {
      state.totalScenarios = null;
    }
  }

  // ------------------------------------------------------------ suite progress
  function renderSuite() {
    const type = $("test-type").value;
    const total = state.scenarioItems
      ? state.scenarioItems.filter((x) => (x.test_types || []).includes(type)).length
      : state.totalScenarios;
    const finished = state.runs.filter((r) => r.status !== "running").length;
    const running = state.runs.some((r) => r.status === "running");
    if (!state.batch && !state.runs.length) {
      $("suite-text").textContent = "No suite running";
      $("suite-fill").style.width = "0%";
      return;
    }
    const denom = total || state.runs.length || 1;
    const pct = Math.min(100, Math.round((finished / denom) * 100));
    $("suite-fill").style.width = `${pct}%`;
    $("suite-bar").setAttribute("aria-valuenow", String(pct));
    if (state.batch && running) {
      $("suite-text").textContent = `${finished} of ${denom} scenarios done · running`;
    } else if (state.batch) {
      $("suite-text").textContent = `${finished} of ${denom} scenarios done`;
    } else {
      $("suite-text").textContent = `${finished} run${finished === 1 ? "" : "s"} recorded`;
    }
  }

  // --------------------------------------------------------------- run list
  function renderRuns() {
    const body = $("runs-body");
    body.replaceChildren();
    $("runs-count").textContent = String(state.runs.length);
    if (!state.runs.length) {
      const tr = document.createElement("tr");
      const td = el("td", "muted small", "No sessions yet. Run tests to start one.");
      td.colSpan = 4;
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    for (const run of state.runs) {
      const tr = document.createElement("tr");
      tr.className = run.id === state.selected ? "selected" : "";
      tr.tabIndex = 0;
      tr.setAttribute("aria-selected", String(run.id === state.selected));
      tr.appendChild(el("td", "mono", run.id.length > 14 ? `${run.id.slice(0, 14)}…` : run.id));
      tr.appendChild(el("td", null, prettyName(run.scenario_id)));
      const status = el("td");
      status.appendChild(el("span", `badge ${run.status}`, humanStatus(run)));
      tr.appendChild(status);
      tr.appendChild(el("td", "small", outcomeOf(run) || "—"));
      const choose = () => {
        state.userPicked = true;
        selectRun(run.id);
      };
      tr.addEventListener("click", choose);
      tr.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          choose();
        }
      });
      body.appendChild(tr);
    }
  }

  // Live state of the storefront session: cart, mode, and the voice/click switch.
  async function refreshShop() {
    if (!state.selected) return;
    try {
      const at = state.syncVersion == null ? "" : `&at=${state.syncVersion}`;
      const s = await api(`/shop/api/state?sid=${encodeURIComponent(state.selected)}${at}`);
      const cart = $("cart-chip");
      cart.textContent = s.cart_count ? `Cart · ${s.cart_count} · $${s.total}` : "Cart · empty";
      cart.classList.remove("hidden");
      const clicks = s.mode === "interactive";
      const mode = $("mode-pill");
      mode.textContent = clicks ? "Shopping by clicks" : "Voice on";
      mode.className = `chip ${clicks ? "clicks" : "voice"}`;
      $("btn-takeover").disabled = clicks;
      $("btn-resume").disabled = !clicks;
    } catch (err) {
      /* the session may not exist yet; the next poll tries again */
    }
  }

  async function shopAction(action) {
    if (!state.selected) return;
    try {
      await api("/shop/api/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid: state.selected, action }),
      });
      await refreshShop();
      // A switch made here is shown at once, not at the next turn.
      state.syncVersion = null;
    } catch (err) {
      $("control-msg").textContent = `Could not ${action.replace("_", " ")}: ${err.message}`;
    }
  }

  async function refreshRuns() {
    try {
      const query = state.batch ? `?batch=${encodeURIComponent(state.batch)}&limit=200` : "?limit=60";
      const data = await api(`/api/runs${query}`);
      const live = data.items.find((r) => r.status === "running" && r.batch_id);
      state.runningBatch = live ? live.batch_id : null;
      $("stop-run").disabled = !(state.batch || state.runningBatch);
      const since = resetMark();
      state.runs = since ? data.items.filter((r) => (r.created_at || "") >= since) : data.items;
      renderRuns();
      renderSuite();
      renderMetricSummary();
      // While a suite runs, follow the active test unless the user picked a run themselves.
      if (state.batch && !state.userPicked && state.runs.length) {
        const active = state.runs.find((r) => r.status === "running") || state.runs[0];
        // Wait until the current run's turns have all been played before moving on to the next test.
        if (active.id !== state.selected && !state.liveBusy) await selectRun(active.id);
      }
      // Between two scenarios no run is momentarily "running"; keep the batch so Stop and Reset always reach it.
      if (state.batch && state.runningBatch === null && state.runs.length
          && state.runs.every((r) => r.status !== "running") && !state.suiteNoticeShown) {
        state.suiteNoticeShown = true;
        $("control-msg").textContent = "Suite finished. Select any run to review it.";
        renderSuite();
      }
    } catch (err) {
      $("control-msg").textContent = `Could not load runs (${err.message}).`;
    }
  }

  // ---------------------------------------------------------- metric summary
  function renderMetricSummary() {
    $("summary-scope").textContent = state.batch ? "this suite" : "recent runs";
    const box = $("summary");
    box.replaceChildren();
    const done = state.runs.filter((r) => r.status !== "running");
    const count = (fn) => done.filter(fn).length;
    const passed = count((r) => r.status === "passed");
    const checksFailing = done.reduce(
      (n, r) => n + ((r.summary && r.summary.checks) || []).filter((c) => !c.passed).length, 0,
    );
    const rate = done.length ? `${Math.round((passed / done.length) * 100)}%` : "—";
    const tiles = [
      ["Pass rate", rate, passed && done.length ? "good" : ""],
      ["Passed", passed, "good"],
      ["Failed", count((r) => r.status === "failed"), "bad"],
      ["Errors", count((r) => r.status === "error"), "bad"],
      ["Booked", count((r) => outcomeOf(r) === "booked"), ""],
      ["Cancelled", count((r) => outcomeOf(r) === "cancelled"), ""],
      ["Moved", count((r) => outcomeOf(r) === "rescheduled"), ""],
      ["Checks failing", checksFailing, checksFailing ? "bad" : "good"],
    ];
    for (const [label, value, tone] of tiles) {
      const tile = el("div", `tile ${tone}`);
      tile.appendChild(el("span", "tile-value", value));
      tile.appendChild(el("span", "tile-label", label));
      box.appendChild(tile);
    }
  }

  // ------------------------------------------------- system under test frame
  // The storefront is set on the Configuration page; by default it is this monitor's own /shop page.
  // Runs and sessions are attached as ?sid=<id>.
  function storefrontAddress() {
    return state.storefrontUrl || `${location.origin}/shop`;
  }

  function sutHref(runId) {
    const raw = storefrontAddress();
    let url;
    try {
      url = new URL(raw);
    } catch (err) {
      throw new Error("the system under test URL is not valid");
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new Error("the system under test URL must start with http:// or https://");
    }
    url.searchParams.set("sid", runId);
    return url.toString();
  }

  function showSut(runId, live) {
    try {
      const href = sutHref(runId);
      const frame = $("sut-frame");
      if (frame.src !== href) frame.src = href;
      $("sut-address").textContent = href;
      $("demo-link").href = href;
      $("demo-link").classList.remove("hidden");
      $("sut-state").textContent = live ? "Live" : "Finished";
      $("sut-state").className = `badge ${live ? "running" : "passed"}`;
    } catch (err) {
      $("control-msg").textContent = err.message;
    }
  }

  // ---------------------------------------------------------------- transcript

  // Step timings of an agent reply: understand, route and dispatch, in milliseconds. Stored as JSON text
  // in the database and as an object on the live stream, so both forms are accepted.
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

  function renderTurn(turn) {
    const wrap = el("div", `turn ${turn.speaker}`);
    const meta = el("div", "turn-meta");
    if (turn.speaker === "customer") {
      meta.appendChild(el("span", "tag", "Customer"));
      if (turn.intent) meta.appendChild(el("span", "tag", `goal: ${turn.intent}`));
    } else {
      meta.appendChild(el("span", "tag", AGENT_LABELS[turn.agent] || "Voice agent"));
      if (turn.act) meta.appendChild(el("span", "tag act", turn.act));
      if (turn.latency_ms != null) meta.appendChild(el("span", "tag", `${Math.round(turn.latency_ms)} ms`));
      const steps = stagesOf(turn);
      if (steps) {
        const tag = el("span", "tag timing", stageText(steps));
        tag.title = "Time spent on each step of this reply";
        meta.appendChild(tag);
      }
      if (turn.act === "close") meta.appendChild(el("span", "tag close", "call ended"));
    }
    wrap.appendChild(meta);
    wrap.appendChild(el("div", "bubble", turn.text_spoken));
    if (turn.speaker === "customer" && turn.text_heard && turn.text_heard !== turn.text_spoken) {
      wrap.appendChild(el("div", "heard", `Recognised as: “${turn.text_heard}”`));
    }
    return wrap;
  }

  function resetConversation(message) {
    $("conversation").replaceChildren(el("p", "empty", message));
  }

  // Keep the storefront in step with the conversation: it shows the page as it was at the displayed turn.
  function syncFrame(version) {
    state.syncVersion = version;
    const frame = $("sut-frame");
    if (frame.contentWindow) frame.contentWindow.postMessage({ type: "evs:sync", version }, location.origin);
  }

  // Live turns arrive in bursts. They are shown one after another at a readable pace, so the speaking
  // highlight, the bubbles and the storefront all move together with the voice.
  function holdFor(turn, backlog) {
    const words = String(turn.text_spoken || "").length;
    let ms = turn.speaker === "agent" ? 1400 + Math.min(2000, words * 20) : 1000 + Math.min(1200, words * 12);
    if (backlog > 3) ms = Math.round(ms / 3);
    else if (backlog > 1) ms = Math.round(ms / 1.6);
    return ms;
  }

  function enqueueLive(runId, turn, epoch) {
    state.liveQueue.push({ runId, turn, epoch });
    if (!state.liveBusy) drainLive();
  }

  const pause = (ms) => new Promise((r) => setTimeout(r, ms));

  // An agent turn is one or two pieces of speech. An introduction ("I'll connect you with our …") is spoken
  // by the front desk; the rest is spoken by the specialist. A customer line is one piece, spoken by nobody
  // on the agent side.
  const INTRO_RE = /^I'll connect you with our [^.]*\. /;
  function segmentsOf(turn) {
    const text = String(turn.text_spoken || "");
    if (turn.speaker !== "agent") return [{ who: null, text }];
    const who = turn.agent && turn.agent !== "router" ? turn.agent : "router";
    const intro = who !== "router" ? INTRO_RE.exec(text) : null;
    if (intro) return [{ who: "router", text: intro[0] }, { who, text: text.slice(intro[0].length) }];
    return [{ who, text }];
  }

  function setSpeaker(who) {
    if (window.AgentView) window.AgentView.speak(who);
  }

  // One turn, shown and spoken as one: the bubble appears, the speaker blinks while its words are spoken,
  // and the next turn waits until this one is finished. With the laptop speaker on, "finished" means the
  // speech ended. With it off, the same pieces take their paced time.
  async function playTurn(turn, backlog) {
    const segs = segmentsOf(turn);
    appendTurn(turn, true);
    if (window.SpeakerVoice && window.SpeakerVoice.isOn()) {
      await window.SpeakerVoice.speakSegments(segs, turn.speaker, setSpeaker);
      return;
    }
    const total = holdFor(turn, backlog);
    const chars = Math.max(1, segs.reduce((n, s) => n + s.text.length, 0));
    for (const seg of segs) {
      setSpeaker(seg.who);
      await pause(total * (seg.text.length / chars));
    }
  }

  async function drainLive() {
    state.liveBusy = true;
    while (state.liveQueue.length) {
      const item = state.liveQueue.shift();
      if (item.epoch !== state.epoch) continue; // silenced since this turn arrived
      if (item.runId !== state.selected) continue; // a different run is shown now
      await playTurn(item.turn, state.liveQueue.length);
    }
    state.liveBusy = false;
  }

  // `live` turns are being played (the bubble, the blink and, when on, the voice); history is shown as text.
  function appendTurn(turn, live = false) {
    const box = $("conversation");
    if (box.querySelector(".empty")) box.replaceChildren();
    box.appendChild(renderTurn(turn));
    box.scrollTop = box.scrollHeight;
    box.classList.toggle("collapsed", !state.expanded);
    if (turn.shop_version != null) syncFrame(turn.shop_version);
    addExecStep(turn);
    if (live && window.AgentView) window.AgentView.onTurn(turn, true);
  }

  // ----------------------------------------------------- live execution monitor
  function resetExec(runStatus) {
    state.steps = 0;
    $("exec-status").textContent = runStatus === "running" ? "Executing" : "Idle";
    $("exec-status").className = `badge ${runStatus === "running" ? "running" : ""}`;
    $("exec-line").textContent = runStatus === "running" ? "Waiting for the first turn…" : "Select a run to follow its steps.";
  }

  function addExecStep(turn) {
    state.steps += 1;
    const isAgent = turn.speaker === "agent";
    $("exec-line").textContent = `Step ${state.steps} · ${isAgent ? "voice agent replied" : "customer spoke"}`;
  }

  // -------------------------------------------------------- outcome + stream
  // Time spent on each step of the replies in this run: 50th and 95th percentile, in milliseconds.
  function renderStepTimes(turns) {
    const samples = {};
    for (const t of turns) {
      const steps = stagesOf(t);
      if (!steps) continue;
      for (const [name, ms] of Object.entries(steps)) {
        if (typeof ms === "number") (samples[name] ||= []).push(ms);
      }
    }
    const pct = (values, p) => {
      const sorted = [...values].sort((a, b) => a - b);
      const k = (sorted.length - 1) * p / 100;
      const lo = Math.floor(k), hi = Math.min(lo + 1, sorted.length - 1);
      return sorted[lo] + (sorted[hi] - sorted[lo]) * (k - lo);
    };
    const dl = $("metrics");
    const names = { understand_ms: "Understand", route_ms: "Route", dispatch_ms: "Dispatch" };
    for (const key of Object.keys(names)) {
      const values = samples[key];
      if (!values || !values.length) continue;
      dl.appendChild(el("dt", null, `${names[key]} time`));
      dl.appendChild(el("dd", null, `p50 ${pct(values, 50).toFixed(1)} ms · p95 ${pct(values, 95).toFixed(1)} ms`));
    }
  }

  function renderOutcome(run) {
    const badge = $("result-badge");
    badge.textContent = humanStatus(run);
    badge.className = `badge ${run.status}`;

    const dl = $("metrics");
    dl.replaceChildren();
    const rows = [
      ["Scenario", prettyName(run.scenario_id)],
      ["Understanding", run.mode],
      ["Outcome", outcomeOf(run) || "—"],
      ["Started", fmt(run.created_at)],
      ["Finished", fmt(run.finished_at) || "—"],
    ];
    if (run.summary && run.summary.error) rows.push(["Stopped because", String(run.summary.error)]);
    for (const [k, v] of rows) {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, v));
    }

    const list = $("checks");
    list.replaceChildren();
    const checks = (run.summary && run.summary.checks) || [];
    if (!checks.length) {
      list.appendChild(el("li", "muted", run.status === "running" ? "Checks appear when the test finishes." : "No checks recorded."));
      return;
    }
    for (const c of checks) {
      const li = el("li", c.passed ? "pass" : "fail");
      li.appendChild(el("span", "name", `${c.passed ? "✓" : "✗"} ${c.name.replace(/_/g, " ")}`));
      if (c.detail) li.appendChild(el("span", "detail", c.detail));
      list.appendChild(li);
    }
  }

  function closeStream() {
    if (state.stream) {
      state.stream.close();
      state.stream = null;
    }
    $("live-badge").classList.add("hidden");
  }

  async function selectRun(id) {
    const epoch = state.epoch;
    state.selected = id;
    state.syncVersion = null;
    state.playing = false;
    state.liveQueue = [];
    renderRuns();
    closeStream();
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (epoch !== state.epoch) return undefined; // Stop, Reset or page close happened while loading
    const live = run.status === "running";
    $("transcript-title").textContent = prettyName(run.scenario_id);
    $("transcript-sub").textContent = "";
    renderOutcome(run);
    resetExec(run.status);
    if (window.AgentView) window.AgentView.reset(run);
    showSut(id, live);
    resetConversation("Loading conversation…");

    if (!live) {
      const turns = await api(`/api/runs/${encodeURIComponent(id)}/turns`);
      if (epoch !== state.epoch) return undefined;
      state.turns = turns.items;
      renderStepTimes(turns.items);
      $("play-run").disabled = !turns.items.length;
      if (!turns.items.length) return resetConversation("This run recorded no conversation.");
      $("conversation").replaceChildren();
      turns.items.forEach((t) => appendTurn(t));
      // Rebuild the agent network in time order from turns and tool events.
      const audit = await api(`/api/runs/${encodeURIComponent(id)}/audit`);
      if (epoch !== state.epoch) return undefined;
      const events = [
        ...turns.items.map((t) => ({ kind: "turn", ts: t.created_at, data: t })),
        ...audit.items.map((a) => ({ kind: "tool", ts: a.ts, data: a })),
      ].sort((x, y) => (x.ts < y.ts ? -1 : x.ts > y.ts ? 1 : 0));
      if (window.AgentView) window.AgentView.replay(events);
      $("exec-line").textContent = `Finished · ${state.steps} steps`;
      return undefined;
    }

    $("live-badge").classList.remove("hidden");
    // EventSource cannot send the API key header, so the key is exchanged once for a single-use ticket.
    let ticket;
    try {
      const issued = await api("/api/stream-tickets", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      ticket = issued.ticket;
    } catch (err) {
      $("exec-line").textContent = `Enter the API key to watch this run (${err.message}).`;
      return undefined;
    }
    if (epoch !== state.epoch) return undefined;
    const stream = new EventSource(`/api/runs/${encodeURIComponent(id)}/stream?ticket=${encodeURIComponent(ticket)}`);
    state.stream = stream;
    let received = 0;
    // Events from a stream that is no longer the current one (closed by Stop, Reset or a new selection) are ignored.
    stream.addEventListener("turn", (ev) => {
      if (state.stream !== stream) return;
      if (!received) $("conversation").replaceChildren();
      received += 1;
      enqueueLive(id, JSON.parse(ev.data), epoch);
    });
    stream.addEventListener("tool", (ev) => {
      if (state.stream !== stream) return;
      if (window.AgentView) window.AgentView.onTool(JSON.parse(ev.data), true);
    });
    stream.addEventListener("end", async () => {
      if (state.stream !== stream) return;
      closeStream();
      const finished = await api(`/api/runs/${encodeURIComponent(id)}`);
      renderOutcome(finished);
      $("exec-status").textContent = humanStatus(finished);
      $("exec-status").className = `badge ${finished.status}`;
      const failed = finished.status === "error";
      $("exec-line").textContent = failed
        ? `Stopped · ${state.steps} steps · ${(finished.summary && finished.summary.error) || "see Stopped because"}`
        : `Finished · ${state.steps} steps`;
      $("sut-state").textContent = failed ? "Stopped" : "Finished";
      $("sut-state").className = `badge ${failed ? "error" : "passed"}`;
      await refreshRuns();
    });
    return undefined;
  }

  // ------------------------------------------------------------- start a suite
  async function startRun() {
    const btn = $("run-btn");
    const msg = $("control-msg");
    const key = $("api-key").value.trim();
    if (state.keyRequired && !key) {
      msg.textContent = "Enter the API key to run tests. Viewing past runs does not need it.";
      return;
    }
    const headers = { "Content-Type": "application/json" };
    if (key) headers["X-API-Key"] = key;
    try {
      sutHref("preview");
    } catch (err) {
      msg.textContent = err.message;
      return;
    }
    btn.disabled = true;
    msg.textContent = "Starting all tests…";
    try {
      const data = await api("/api/evals", {
        method: "POST",
        headers,
        body: JSON.stringify({
          test_type: $("test-type").value,
          domain: "store",
          mode: $("mode-select").value,
          pace_ms: Number($("speed-select").value),
        }),
      });
      state.batch = data.batch_id;
      state.suiteNoticeShown = false;
      state.startedHere = true;
      state.key = key;
      rememberKey(key);
      $("stop-run").disabled = false;
      state.userPicked = false;
      state.selected = null;
      msg.textContent = `${$("test-type").selectedOptions[0].textContent} suite started. Each test plays in the frame.`;
      await refreshRuns();
      for (let i = 0; i < 20 && !state.runs.length; i += 1) {
        await new Promise((r) => setTimeout(r, 250));
        await refreshRuns();
      }
    } catch (err) {
      msg.textContent = `Could not start tests: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------- stop a suite
  // Silence everything the page is doing for the conversation: queued lines, playback, live feed, voice.
  // The epoch bump makes every load or stream that is still in flight drop its results.
  function silenceConversation() {
    state.epoch += 1;
    state.playing = false;
    state.liveQueue = [];
    closeStream();
    if (window.SpeakerVoice) window.SpeakerVoice.stop();
    setSpeaker(null);
  }

  async function stopSuite(message, unloading = false) {
    silenceConversation();
    const batch = state.batch || state.runningBatch;
    // Forget the batch before waiting on the server, so a poll that lands in the meantime cannot follow it again.
    state.batch = null;
    state.runningBatch = null;
    state.startedHere = false;
    $("stop-run").disabled = true;
    if (!batch) {
      if (message) $("control-msg").textContent = message;
      renderSuite();
      return;
    }
    const headers = { "Content-Type": "application/json" };
    if (state.key) headers["X-API-Key"] = state.key;
    try {
      await fetch(`/api/batches/${encodeURIComponent(batch)}/cancel`, {
        method: "POST", headers, body: "{}", keepalive: unloading,
      });
    } catch (err) {
      /* the server may already be gone; nothing else to do */
    }
    silenceConversation(); // anything that arrived while the cancel was in flight
    if (message) $("control-msg").textContent = message;
    renderSuite();
  }

  // ------------------------------------------------------------------ reset
  const RESET_KEY = "evs.resetAt";
  function localStamp(d = new Date()) {
    const p2 = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}T${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
  }
  function resetMark() {
    try {
      return localStorage.getItem(RESET_KEY) || "";
    } catch (err) {
      return "";
    }
  }

  // A fresh storefront session, so the shop opens on its home page (used on load and on Reset).
  function loadFreshStorefront() {
    const fresh = `shop-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
    try {
      $("sut-frame").src = sutHref(fresh);
      $("sut-address").textContent = sutHref(fresh);
    } catch (err) {
      $("sut-frame").src = "about:blank";
      $("sut-address").textContent = err.message;
    }
  }

  async function resetView() {
    silenceConversation();
    if (state.batch || state.runningBatch) await stopSuite(null);
    try {
      localStorage.setItem(RESET_KEY, localStamp());
    } catch (err) {
      /* storage can be blocked; the reset still applies to this page */
    }
    closeStream();
    state.runs = [];
    state.selected = null;
    state.batch = null;
    state.userPicked = false;
    state.turns = [];
    state.steps = 0;
    state.syncVersion = null;
    renderRuns();
    renderSuite();
    renderMetricSummary();
    $("result-badge").textContent = "—";
    $("result-badge").className = "badge";
    $("metrics").replaceChildren();
    const checks = $("checks");
    checks.replaceChildren();
    checks.appendChild(el("li", "muted", "Checks appear when a run finishes."));
    resetConversation("The conversation appears here when a test runs.");
    $("transcript-title").textContent = "Eliza voice";
    $("transcript-sub").textContent = "No session selected";
    $("play-run").disabled = true;
    $("btn-takeover").disabled = true;
    $("btn-resume").disabled = true;
    $("cart-chip").classList.add("hidden");
    $("mode-pill").classList.add("hidden");
    $("sut-address").textContent = "Select a session or run tests";
    $("sut-state").textContent = "Idle";
    $("sut-state").className = "badge";
    $("demo-link").classList.add("hidden");
    loadFreshStorefront();
    if (window.AgentView) {
      window.AgentView.reset({ source: "store", status: "idle", scenario_id: "Agent network" });
      $("net-live").textContent = "IDLE";
      $("net-live").className = "live-pill";
    }
    resetExec("idle");
    $("control-msg").textContent = "Reset. The view is back to its starting state. Execution history is kept.";
  }

  // ------------------------------------------------------------------ boot
  document.addEventListener("DOMContentLoaded", async () => {
    // Speech left over from a page that was just refreshed must not carry on into this one.
    if (window.SpeakerVoice) window.SpeakerVoice.stop();
    $("run-btn").addEventListener("click", startRun);
    $("stop-run").addEventListener("click", () => stopSuite("Stopped by you."));
    // Closing the page stops the suite it started, so nothing keeps running unattended.
    // Refresh or close: silence the voice at once, and cancel any suite this page started.
    const leaving = () => {
      silenceConversation();
      if (state.batch && state.startedHere) stopSuite(null, true);
    };
    window.addEventListener("pagehide", leaving);
    window.addEventListener("beforeunload", leaving);
    // Play replays a finished conversation turn by turn. The storefront follows each turn, and the
    // voice is heard too when the laptop speaker is on.
    $("play-run").addEventListener("click", async () => {
      if (state.playing || !state.turns || !state.turns.length) return;
      state.playing = true;
      if (window.SpeakerVoice) window.SpeakerVoice.stop();
      $("conversation").replaceChildren();
      for (const t of state.turns) {
        if (!state.playing || state.selected === null) break;
        await playTurn(t, 0);
        await pause(300);
      }
      state.playing = false;
    });
    $("reset-btn").addEventListener("click", resetView);
    $("sut-frame").addEventListener("load", () => {
      if (state.syncVersion != null) syncFrame(state.syncVersion);
    });
    $("conv-view").addEventListener("click", () => {
      state.expanded = !state.expanded;
      $("conversation").classList.toggle("collapsed", !state.expanded);
      $("conv-view").textContent = state.expanded ? "Show latest" : "View all";
    });
    $("btn-takeover").addEventListener("click", () => shopAction("takeover"));
    $("btn-resume").addEventListener("click", () => shopAction("resume"));
    setInterval(refreshShop, 2000);
    resetExec("idle");
    await Promise.all([loadCapabilities(), loadScenarioCount(), loadSettings()]);
    // Show the storefront home page straight away; a finished run selected below replaces it.
    loadFreshStorefront();
    await refreshRuns();
    // Nothing starts on its own: a test still running is only followed after Run is pressed or its row is clicked.
    const finished = state.runs.find((r) => r.status !== "running");
    if (finished) await selectRun(finished.id);
    setInterval(refreshRuns, state.batch ? 1500 : 4000);
  });
})();

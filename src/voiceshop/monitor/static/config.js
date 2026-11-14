/* Configuration page: the storefront address the voice agents shop through, and the agents' tool scopes.
   Text is written with textContent only. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const DEFAULT_PATH = "/shop";
  const SCOPE_TEXT = {
    "catalog:read": "read the catalogue",
    "cart:write": "change the cart",
    "checkout:write": "check out and place orders",
    "orders:read": "read an order after verification",
    "refunds:write": "issue a refund within policy",
  };

  function msg(text, bad = false) {
    const m = $("config-msg");
    m.textContent = text;
    m.className = bad ? "small bad" : "muted small";
  }

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error((body.error && body.error.message) || `request failed (${res.status})`);
    return body;
  }

  function fill(settings) {
    const input = $("storefront-url");
    input.value = settings.storefront_url || "";
    input.placeholder = `${location.origin}${DEFAULT_PATH}`;
    $("storefront-default").textContent = settings.storefront_url
      ? `Using ${settings.storefront_url}.`
      : `Using the default: this monitor's storefront at ${location.origin}${DEFAULT_PATH}.`;
    const rows = $("agent-rows");
    rows.replaceChildren();
    for (const agent of settings.voice_agents) {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = agent.name;
      const tools = document.createElement("td");
      tools.textContent = agent.scopes.length
        ? agent.scopes.map((s) => SCOPE_TEXT[s] || s).join(", ")
        : "none: answers from published information only";
      tr.append(name, tools);
      rows.appendChild(tr);
    }
  }

  async function save(url) {
    try {
      const settings = await api("/api/settings", {
        method: "POST",
        body: JSON.stringify({ storefront_url: url }),
      });
      fill(settings);
      msg(url ? "Saved. The monitor uses the new storefront after it is reloaded." : "Saved. The monitor uses the default storefront after it is reloaded.");
    } catch (err) {
      msg(err.message, true);
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    $("storefront-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const raw = $("storefront-url").value.trim();
      if (raw && !/^https?:\/\//i.test(raw)) {
        msg("The address must start with http:// or https://.", true);
        return;
      }
      save(raw);
    });
    $("storefront-default-btn").addEventListener("click", () => save(""));
    try {
      fill(await api("/api/settings"));
    } catch (err) {
      msg(`Could not load the settings (${err.message}).`, true);
    }
  });
})();

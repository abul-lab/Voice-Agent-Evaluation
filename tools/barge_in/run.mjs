// Runs every barge-in case in a real Chromium. Both the agent's reply and the caller's speech are played as
// audio on the page's own audio clock (no microphone): see lab.js for what that does and does not measure.
// Usage (from the repo root, after tools/barge_in/make_clips.ps1 and build_cases.py):
//   node tools/barge_in/run.mjs            # all cases
//   node tools/barge_in/run.mjs --only interrupt   # cases whose id starts with "interrupt"
import { createRequire } from "node:module";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

// Playwright is installed in e2e/, so load it from there.
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..", "..");
const require = createRequire(path.join(root, "e2e", "package.json"));
const { chromium } = require("playwright");

const outDir = path.join(root, "results", "barge-in");
const casesFile = path.join(outDir, "cases.json");
if (!fs.existsSync(casesFile)) {
  console.error("cases.json not found: run tools/barge_in/build_cases.py first");
  process.exit(2);
}
const { cases } = JSON.parse(fs.readFileSync(casesFile, "utf8"));
const only = process.argv.includes("--only") ? process.argv[process.argv.indexOf("--only") + 1] : null;
const selected = only ? cases.filter((c) => c.case_id.startsWith(only)) : cases;

const types = { ".html": "text/html", ".js": "text/javascript", ".wav": "audio/wav" };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  let file = null;
  if (url.pathname === "/" || url.pathname === "/lab.html") file = path.join(here, "lab.html");
  else if (url.pathname === "/lab.js" || url.pathname === "/lab-worklet.js") file = path.join(here, path.basename(url.pathname));
  else if (url.pathname.startsWith("/clips/")) file = path.join(outDir, "clips", path.basename(url.pathname));
  if (!file || !fs.existsSync(file)) {
    res.writeHead(404).end("not found");
    return;
  }
  res.writeHead(200, { "content-type": types[path.extname(file)] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const port = server.address().port;

const results = [];
for (const c of selected) {
  const browser = await chromium.launch({
    headless: true,
    args: ["--autoplay-policy=no-user-gesture-required"],
  });
  try {
    const page = await browser.newPage();
    const params = new URLSearchParams({
      case: c.case_id,
      expect: c.expect,
      minSpeechMs: String(c.min_speech_ms),
      agentDelayMs: String(c.agent_delay_ms),
      leadSec: String(c.lead_sec),
      caller: `/clips/${c.clip}`,
      agent: "/clips/agent_reply.wav",
    });
    await page.goto(`http://127.0.0.1:${port}/lab.html?${params}`);
    await page.waitForFunction(() => window.__BARGE_RESULT__ !== undefined, null, { timeout: 60000 });
    const result = await page.evaluate(() => window.__BARGE_RESULT__);
    results.push(result);
    const latency = result.decisionLatencyMs == null ? "-" : `${result.decisionLatencyMs} ms`;
    console.log(`${c.case_id}: valid=${result.valid} interrupted=${result.interrupted} decision=${latency} error=${result.error || "none"}`);
  } finally {
    await browser.close();
  }
}

server.close();
fs.writeFileSync(path.join(outDir, "results.json"), JSON.stringify({ run_at: new Date().toISOString(), results }, null, 2));
console.log(`wrote ${results.length} results to ${path.join(outDir, "results.json")}`);

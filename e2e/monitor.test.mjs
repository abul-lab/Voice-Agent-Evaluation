// End-to-end checks of the monitor in a real browser. Starts its own server on a throwaway database.
// Uses the rules router only: no speech service and no language-model calls.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, '..');
const port = 8788;
const base = `http://127.0.0.1:${port}`;
let server;
let browser;

// Use VOICESHOP_PYTHON if set, else the project's virtual environment, else python on the PATH (CI).
const venvPython = process.platform === 'win32'
  ? join(repo, '.venv', 'Scripts', 'python.exe') : join(repo, '.venv', 'bin', 'python');
const python = process.env.VOICESHOP_PYTHON || (existsSync(venvPython) ? venvPython : 'python');

async function waitFor(url, tries = 60) {
  for (let i = 0; i < tries; i++) {
    try { const r = await fetch(url); if (r.ok) return; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`server did not start at ${url}`);
}

before(async () => {
  const dir = mkdtempSync(join(tmpdir(), 'voiceshop-e2e-'));
  server = spawn(python, ['-m', 'voiceshop', 'serve', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: repo,
    env: { ...process.env, VOICESHOP_DB: join(dir, 'e2e.db'), VOICESHOP_LOCAL_UI_NO_KEY: '1',
           VOICESHOP_KEYS_FILE: join(dir, 'keys.json') },
    stdio: 'ignore',
  });
  await waitFor(`${base}/healthz`);
  browser = await chromium.launch({ headless: true });
});

after(async () => {
  if (browser) await browser.close();
  if (server) server.kill();
});

async function page() {
  const p = await browser.newPage();
  p.errors = [];
  p.on('pageerror', (e) => p.errors.push(String(e)));
  return p;
}

test('the monitor opens with the four navigation links and no errors', async () => {
  const p = await page();
  await p.goto(`${base}/`);
  const nav = await p.textContent('nav.topnav');
  for (const label of ['Monitor', 'Test Cases', 'Execution history', 'Configuration']) {
    assert.ok(nav.includes(label), `navigation has ${label}`);
  }
  assert.deepEqual(p.errors, [], 'no uncaught page errors');
  await p.close();
});

test('nothing starts on its own: the page waits for Run tests', async () => {
  const p = await page();
  await p.goto(`${base}/`);
  await p.waitForTimeout(1500);
  const msg = (await p.textContent('#control-msg')) || '';
  assert.ok(!msg.toLowerCase().includes('started'), 'the page does not report a suite as started');
  const rows = await p.locator('#runs-body tr').count();
  assert.equal(rows <= 1, true, 'no run was started when the page loaded');
  await p.close();
});

test('clicking the storefront logo returns the shop to its home page', async () => {
  const p = await page();
  await p.goto(`${base}/shop?sid=e2e-logo-check`);
  await p.click('[data-page="category:tent"]');
  await p.waitForTimeout(800);
  await p.click('.evs-logo');
  await p.waitForFunction(() => document.querySelector('.evs-page')?.textContent.includes('Gear for the next trip'),
    null, { timeout: 5000 });
  const active = await p.textContent('.evs-nav a.active');
  assert.equal(active.trim(), 'Home');
  await p.close();
});

test('the test cases page lists the three tiers', async () => {
  const p = await page();
  await p.goto(`${base}/tests`);
  await p.waitForSelector('[data-type="smoke"]');
  const tabs = await p.textContent('.tabs, body');
  assert.ok(tabs.includes('Smoke') && tabs.includes('Targeted regression') && tabs.includes('Full regression'));
  await p.close();
});

test('configuration refuses an address that is not a web address', async () => {
  const p = await page();
  await p.goto(`${base}/config`);
  await p.fill('#storefront-url', 'ftp://example.invalid/shop');
  await p.click('#storefront-save');
  await p.waitForFunction(() => document.querySelector('#config-msg')?.textContent.includes('must start with'),
    null, { timeout: 5000 });
  await p.close();
});

test('a smoke run starts from the page and its row appears', async () => {
  const p = await page();
  await p.goto(`${base}/`);
  await p.waitForTimeout(800);
  await p.click('#run-btn');
  await p.waitForFunction(() => document.querySelector('#control-msg')?.textContent.toLowerCase().includes('started'),
    null, { timeout: 10000 });
  await p.waitForFunction(() => document.querySelectorAll('#runs-body tr').length >= 1, null, { timeout: 15000 });
  assert.deepEqual(p.errors, [], 'no uncaught page errors during the run');
  await p.close();
});

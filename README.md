# Voice Agent Evaluation

Release gates for a multi-agent voice shopping assistant. The shop is fictional (Eliza Voice Shopping). The point is the quality process: scripted callers exercise the agents, every result is checked against evidence (the transcript, the audit log and the store data), and a release gate decides whether a version can ship.

## What is in the repository

- **A four-agent store.** A Front Desk Specialist routes each caller to one of four specialists: Shopping, Order Tracking, Refunds and Customer Care. Specialists act only through a tool gateway that enforces per-agent scopes and writes an audit trail.
- **A storefront** at `/shop`, which the voice agents shop through. Voice and clicks share one cart.
- **A live monitor** at `/`: the storefront inside a device, the conversation, the agent network, test runs and results.
- **A test suite** of 40 scripted callers in three tiers (below), with 20 named checks.
- **Speech checks** (optional): caller audio is generated, transcribed with Gemini, and the transcript is routed by the front desk.
- **A configuration page** at `/config` for the storefront address and each agent's tool permissions.

## Quick start

Requires Python 3.11 or later. The runtime has no third-party dependencies.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; on Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
```

Run the test tiers from the command line:

```bash
voiceshop eval --tags smoke        # 1 case
voiceshop eval --tags targeted     # 3 cases
voiceshop eval                     # full regression, 40 cases
voiceshop scenarios                # list the cases
```

Start the monitor (local use, no key needed):

```bash
VOICESHOP_LOCAL_UI_NO_KEY=1 voiceshop serve --host 127.0.0.1 --port 8765
```

Then open http://127.0.0.1:8765. The storefront is at `/shop`, the configuration page at `/config`.

Run the unit, integration and security tests, and lint:

```bash
pytest -q
ruff check src tests
```

## Test tiers

| Tier | Cases | What it covers |
|---|---|---|
| Smoke | 1 | The Front Desk Specialist hands a shopper to the Shopping Agent, who recommends a tent. Run on every change. |
| Targeted regression | 3 | The Front Desk Specialist with each other agent: Shopping (winter bag, then price), Order Tracking (status after verification), Refunds (refund within the window). |
| Full regression | 40 | Everything above, plus customer care and handoffs, storefront flows, guardrails (prompt injection, data leakage, wrong identity, card details, off-topic requests, abuse), driving safety holds, and background noise (a passing car, rain). Run before a release. |

Each case is a caller with a fixed goal and a fixed seed, so the same case gives the same conversation.

The gate passes when at least 90% of the cases pass fully and none error. The 90% figure is a starting point; the release bar should be set with the quality owner.

### Checks

| Layer | Checks |
|---|---|
| Routing and handoff | `routing_correct`, `dialogue_invariants` |
| Task completion | `task_completed`, `commerce_task`, `recommendations_fit`, `grounded_facts` |
| Policy | `refund_policy`, `idempotent_refund`, `audit_trail` |
| Identity and disclosure | `verification_gate`, `no_repeat_verification` |
| Integrity | `cart_consistency`, `confirmation_before_order` |
| Guardrails and security | `no_forbidden_act`, `no_forbidden_text`, `no_forbidden_tool_call` |
| Resilience | `graceful_failure` (a timed-out back office is reported, never guessed) |
| Conduct | `turn_budget`, `conversation_completed`, `escalation_code` |

Checks read the transcript, the audit log and the store data. An agent's own statement that it did something is not accepted as evidence.

## Architecture

```
Caller (simulated, or audio through speech services)
   │  heard text (background tags are added here)
   ▼
Front Desk Specialist — rules router, no model
   │  driving hold · guardrails · routing by cues
   ├─► Shopping Agent ──────┐
   ├─► Order Tracking ──────┤──► Tool gateway ──► Store back office
   ├─► Refunds Agent ───────┤     • scopes per agent (least privilege)
   └─► Customer Care ───────┘     • audit log for every call
                                  • idempotent refunds, versioned policy
   │
   ▼
Storefront sessions: cart, page, orders, history of every version
   ▲  shared by the caller's voice session and the monitor
Monitor: storefront · agent network · transcript · test runs
```

- **Agents never touch data directly.** Every action goes through the gateway, which checks the agent's scopes and writes the audit trail. Customer Care has no tools.
- **Routing is rules-based on purpose.** It is predictable and testable. The router is not a language model.
- **Refunds follow a versioned policy.** Refusals cite a rule ID and the policy version.
- **Identity is checked before any order details are given.** The order number and the email on the order must match.
- **Driving safety is strict.** Once a caller says they are driving, the voice channel gives no shopping, order or refund help until they say they have parked. Only the goodbye is still accepted.

Source layout:

| Path | Contents |
|---|---|
| `src/voiceshop/catalog/` | Store: router, agents, tool gateway, storefront sessions, caller simulator, suite runner |
| `src/voiceshop/monitor/` | HTTP server (standard library), server-sent events, static monitor, storefront and configuration pages |
| `src/voiceshop/speech/` | Gemini speech adapter and the front desk speech case |
| `src/voiceshop/simulation/` | Conversation loop and the speech-recognition error model |
| `src/voiceshop/evaluation/` | Check records, report writing, conduct invariants |
| `src/voiceshop/storage/` | SQLite storage (WAL mode, schema) |
| `data/catalog/` | Fictional catalogue, orders and refund policy |
| `scenarios/` | The store test cases (`store_scenarios.json`) |
| `tests/` | Unit, integration, security and monitor API tests |

## Speech checks

The speech check runs one case end to end with real speech:

1. The front desk greets the caller. The greeting is spoken and saved as audio.
2. The caller's line is spoken in a different voice and saved as audio.
3. Gemini transcribes the caller's audio. That transcript is what the agent hears.
4. The front desk routes the transcript. The route must match the route for the exact text.

```bash
export GEMINI_API_KEY=...        # Windows PowerShell: $env:GEMINI_API_KEY = "..."
python -m voiceshop.speech.frontdesk_check
```

Evidence (audio and a JSON report) is written to `results/speech/frontdesk_case/`. The key is read from the environment and is never written to the repository or to reports. Models are named in each report. Speech services are external and may change, so the report records which model was used.

Speech checks are not part of CI because they need a key and call an external service.

## Configuration

| Variable | Purpose |
|---|---|
| `VOICESHOP_API_KEY` | Key for changing things through the API (suite runs, history, settings). Required outside local use. |
| `VOICESHOP_LOCAL_UI_NO_KEY=1` | Lets the local monitor change things without a key. Only accepted on a loopback address. |
| `VOICESHOP_DB` | Path to the SQLite database (default `results/voiceshop.db`). |
| `GEMINI_API_KEY` / `VOICESHOP_GEMINI_API_KEY` | Gemini key for the speech check. |
| `VOICESHOP_GEMINI_MODEL` | Gemini model used for transcription. |
| `VOICESHOP_MAX_CONCURRENT_RUNS`, `VOICESHOP_MAX_BODY_BYTES`, `VOICESHOP_LOG_LEVEL` | Server limits and logging. |

The storefront address is set on the Configuration page and saved to `monitor_settings.json` next to the database. It must end in `/shop`. The default is this monitor's own storefront.

## Security

- No secrets are stored in the repository. Keys come only from environment variables, and `.env` files are ignored by git.
- The server listens on loopback by default. Binding to another address requires an API key.
- Changes through the API require the key unless the local, loopback-only exception is enabled. Request bodies have a size limit, and only JSON is accepted.
- Static files are served from a fixed list. Identifiers in URLs are validated with a strict pattern.
- The storefront may be framed only by this monitor (`X-Frame-Options` and CSP).
- The browser code writes all text with `textContent`. Nothing is inserted as HTML.
- The storefront's own actions (adding to cart, placing an order) are not behind the API key. This is a demonstration shop with in-memory data; do not expose it on a public network as it is.

## Known limits

- **Speech in the suite is text-level.** The 40-case suite models recognition errors as text, not audio. Real speech is tested only by the single front desk case.
- **The browser's voices are limited.** The monitor's laptop speaker uses the voices installed on the computer. Some computers have only two or three English voices.
- **The store data is in memory and fictional.** Orders, catalogue and policy live in `data/catalog/northwind_outdoor.json`.
- **One server process, one operator.** There are no user accounts. Storefront sessions and runs are kept per server.
- **Run from a checkout.** The store reads its data files from the repository, so an installed package on its own will not find them.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

1. lint (`ruff check src tests`)
2. unit, integration and security tests (`pytest`)
3. the smoke, targeted and full regression tiers (`voiceshop eval`)

Reports are uploaded as build artifacts.

## License

MIT. See [LICENSE](LICENSE).

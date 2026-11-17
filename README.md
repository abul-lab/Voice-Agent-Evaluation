# Voice Agent Evaluation

[![ci](https://github.com/abul-lab/Voice-Agent-Evaluation/actions/workflows/ci.yml/badge.svg)](https://github.com/abul-lab/Voice-Agent-Evaluation/actions/workflows/ci.yml)

**A quality process for a multi-agent voice shopping assistant.** Scripted callers exercise four specialist agents. Every result is checked against evidence (the transcript, the audit log and the store data), not against the agent's own claims. A release gate decides whether a version can ship. It is built with the Python standard library, has 189 tests, and runs 74 test cases in about a minute.

![The monitor: the storefront, the agent network and the release gate](docs/images/monitor.png)

| Screen | What it shows |
|---|---|
| Monitor | The storefront inside a device, the agents at work, and the live metrics |
| [Execution history](docs/images/history.png) | Every run, its checks and its transcript |
| [Configuration](docs/images/configuration.png) | Storefront address and each agent's tool permissions |

**Try it in two commands**

```bash
pip install -e ".[dev]" && voiceshop eval --tags smoke        # one case, a few seconds
VOICESHOP_LOCAL_UI_NO_KEY=1 voiceshop serve --host 127.0.0.1 --port 8765   # then open http://127.0.0.1:8765
```

Read the [case study](docs/case-study.md) for the design decisions and what failed along the way, and see a [sample release report](docs/examples/sample-report.md) without running anything.

## Quality engineering highlights

| Pillar | How it is built here |
|---|---|
| **Independent oracles** | Every check verifies three sources the agent cannot falsify: the transcript, the append-only SQLite audit log, and the store's back-office state (`src/voiceshop/catalog/storerun.py`). |
| **Deterministic simulation** | Seeded caller goals, synthetic ASR and ambient-noise models, injectable back-office faults (`ToolFault`), and bundled caller audio clips make every failure reproducible (`scenarios/store_scenarios.json`). |
| **Least-privilege tool gateway** | Specialist agents have explicit tool scopes; unauthorized calls are refused and audited, and database triggers forbid editing or deleting audit events (`src/voiceshop/catalog/store.py`, `src/voiceshop/storage/db.py`). |
| **Multi-gate release governance** | A release requires pass rate, zero errors, zero red-team successes, p95 reply latency within budget, and zero regressions against the committed baseline (`voiceshop regress`). |
| **Epistemic discipline on LLM judges** | Rubric grading is paired with human calibration (`voiceshop calibrate`) and model/agreement drift detection (`voiceshop judge-drift`), and is kept advisory until inter-rater agreement is met. |
| **Zero runtime dependencies** | The agents, monitor server, SSE streaming, RBAC keyring, Luhn card masking, and Prometheus `/metrics` endpoint use only the Python 3.11+ standard library (`pyproject.toml`). |

## What is in the repository

- **A four-agent store.** A Front Desk Specialist routes each caller to one of four specialists: Shopping, Order Tracking, Refunds and Customer Care. Specialists act only through a tool gateway that enforces per-agent scopes and writes an audit trail.
- **A storefront** at `/shop`, which the voice agents shop through. Voice and clicks share one cart.
- **A live monitor** at `/`: the storefront inside a device, the conversation, the agent network, test runs and results.
- **A test suite** of 74 scripted callers in three tiers (below), with 26 named checks. 31 of the cases are red-team attacks. A separate barge-in test type (12 cases) interrupts the agents' replies.
- **Speech checks** (optional): caller audio is generated, transcribed with Gemini, and the transcript is routed by the front desk.
- **A configuration page** at `/config` for the storefront address and each agent's tool permissions.

## Capabilities

What the project covers, and what it does not.

| Area | What is covered |
|---|---|
| **Conversations** | Four specialist agents (Shopping, Order Tracking, Refunds, Customer Care) behind a Front Desk router: routing, handoffs, identity checks, refund read-back and confirmation, repair after misunderstanding, and storefront sessions shared with the caller's voice session. |
| **Test types** | Smoke (1 case), Targeted regression (3), Full regression (74), Barge-in (12, through the real agents), and a Barge-in test (1). Every test type runs from the monitor's dropdown, or from the `voiceshop eval` command for the regression tiers. |
| **Red team** | 31 attack cases in six groups: prompt injection, data extraction, impersonation, social engineering, policy bypass, and abuse with off-topic requests. The gate allows no successful attack. |
| **Checks** | 26 named checks, each reading evidence the agent cannot write itself: the transcript, the audit log and the store data. They cover routing, verification, refund policy and confirmation, cart and order consistency, guardrails, graceful failure, conduct, and barge-in. |
| **Release gate** | Pass rate, no errors, no successful attack, latency budget, and a regression check against a saved baseline (`voiceshop regress`). |
| **Barge-in** | A caller interrupts an agent's reply. The stop decision runs on the caller's real audio, and the agent's reply is judged as spoken and as heard. Real interruptions and backchannels are tested at three minimum-speech settings. |
| **Speech** | A speech round trip for one case, with recognition through Gemini. Barge-in timing uses synthetic speech, generated locally. |
| **Judge** | An LLM judge with rubric scores and a calibration set. The calibration labels are one author's and are not trusted until a second rater has labelled them. |
| **Monitor** | A live UI with test runs, storefront playback, agent network, transcripts, execution history, configuration, and an API with audit records. |
| **Security** | Named API keys with roles (viewer, operator, admin), hashed keys, rate limits, an append-only audit trail, sign-in to read transcripts, masking of card numbers, and 14-day transcript retention. |
| **Operations** | Container image, compose file, TLS proxy example, Prometheus alert rules checked with `promtool`, service levels, and data-governance notes. |
| **Quality of the project** | Lint, unit and integration tests with a coverage floor, a dependency audit, the evaluation tiers and the regression gate, and browser checks, all run in CI. |

**Not covered yet:** real phone audio and echo, real caller speech at scale, a calibrated judge, authentication on the storefront (it is open by design for this demonstration), and any production deployment.

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
voiceshop eval                     # full regression, 74 cases
voiceshop regress                  # compare the last full run with the saved baseline
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
| Full regression | 74 | Everything above, plus customer care and handoffs, storefront flows, guardrails, 31 red-team attacks (prompt injection, data extraction, impersonation, social engineering, policy bypass, abuse, off-topic), driving safety holds, and background noise (a passing car, rain). Run before a release. |
| Barge-in | 12 | A caller interrupts the agent's reply at a set point, with real caller clips (synthetic speech) and the stop decision measured on the audio timeline. Checks: the agent stops for a real interruption and not for a backchannel, and it answers what was said. Run on request from the Barge-in test type; not part of Full regression. |

Each case is a caller with a fixed goal and a fixed seed, so the same case gives the same conversation.

The release gate passes only when all four hold:

| Gate | Rule |
|---|---|
| Pass rate | at least 90% of cases pass fully (a starting point; the quality owner sets the real bar) |
| No errors | no case ends in an error |
| Red team | no attack succeeds (limit 0%) |
| Latency | the 95th percentile of agent reply time is within the budget (250 ms) |

The regression gate (`voiceshop regress`) is separate: every case that passed in the saved baseline must still pass.

### Checks

| Layer | Checks |
|---|---|
| Routing and handoff | `routing_correct`, `dialogue_invariants` |
| Task completion | `task_completed`, `commerce_task`, `recommendations_fit`, `grounded_facts` |
| Policy | `refund_policy`, `refund_confirmed_first`, `refund_declined_leaves_ledger_unchanged`, `idempotent_refund`, `audit_trail` |
| Identity and disclosure | `verification_gate`, `no_repeat_verification` |
| Integrity | `cart_consistency`, `confirmation_before_order` |
| Guardrails and security | `no_forbidden_act`, `no_forbidden_text`, `no_forbidden_tool_call` |
| Resilience | `graceful_failure` (a timed-out back office is reported, never guessed) |
| Conduct | `turn_budget`, `conversation_completed`, `escalation_code`, `repair_ladder` |
| Barge-in (separate test type) | `barge_in_timing_valid`, `barge_in_decision`, `barge_in_answered` |

Checks read the transcript, the audit log and the store data. An agent's own statement that it did something is not accepted as evidence.

## Quality measures

| Measure | Where it comes from | Command |
|---|---|---|
| Failure categories | every failed check belongs to one category (routing, policy, verification, guardrail, task, grounding, resilience, conduct) | `voiceshop eval` (report: *Failure categories*) |
| Task completion | completion rate, handoff success and mean turns, per goal (shopping, refund, order, care, safety, noise, red team and others) | `voiceshop eval` (report: *Completion by goal*) |
| Red team | attacks that succeed, out of all attacks | `voiceshop eval --tags redteam` |
| Latency by step | time spent understanding, routing and dispatching each reply, stored with each turn | `voiceshop eval` (report: *Latency by step*) |
| Regression | a saved baseline of every case's result | `voiceshop baseline` (write, review in the change) and `voiceshop regress` (compare) |
| LLM judge | rubric scores for accuracy, helpfulness and tone, and whether a reply invents facts | `voiceshop judge` |
| Judge calibration | how often the judge agrees with human labels | `voiceshop calibrate` |

**The judge is not yet trusted.** The calibration set in `tests/judge/calibration.json` holds 36 agent replies sampled from the clean regression run. The labels were written by the repository author as interim labels, with one rater and no review; each item has a note explaining the decision. The QE team should replace them with their own labels. Until a calibration agreement has been accepted, judge scores are information, not a gate.

The judge runs on a local model (Ollama) for plumbing and integration checks. The Gemini backend exists, but it is not part of any run until the team approves a judged run, because each call has a cost.

## Architecture

```mermaid
flowchart LR
    caller["Caller<br/>scripted goals, caller clips"]
    subgraph agents["Store agents"]
        fd["Front Desk<br/>rules router (default) or Gemini"]
        shop["Shopping"]
        orders["Order Tracking"]
        refunds["Refunds"]
        care["Customer Care<br/>no tools"]
    end
    gw["Tool gateway<br/>scopes per agent, audit trail"]
    store[("Store back office<br/>catalogue, orders, refund ledger")]
    barge["Barge-in timing<br/>caller audio, reply timeline"]
    checks["Checks<br/>transcript, audit log, store data"]
    gate{{"Release gate"}}
    monitor["Monitor<br/>UI, API, history"]
    db[("SQLite<br/>runs, turns, audit")]

    caller -- "text, or audio via the barge-in test" --> fd
    barge -- "cut point, words heard" --> fd
    fd --> shop & orders & refunds
    fd --> care
    shop & orders & refunds --> gw
    gw --> store
    fd -- "transcript" --> checks
    gw -- "audit entries" --> checks
    store -- "end state" --> checks
    checks --> gate
    monitor -- "starts suites, shows results" --> caller
    checks --> db
    monitor --> db
```

- **Agents never touch data directly.** Every action goes through the gateway, which checks the agent's scopes and writes the audit trail. Customer Care has no tools.
- **The default router is rules-based.** It is predictable and testable. An optional Gemini router chooses the team for each message, and it is measured in the same way.
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
| `src/voiceshop/evaluation/` | Check records, report writing, conduct invariants, quality analysis, regression gate, LLM judge |
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

The check also measures the **round trip**: from the caller's audio being sent for transcription, to the agent's spoken reply being ready. It reports the time for each part (speech to text, routing, and speech out). The limit of 3 seconds is a starting point; the quality owner sets the real value.

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
- Transcripts, runs and the audit trail can be read only with a key (any role). The live stream uses a single-use ticket of 60 seconds, because a browser's EventSource cannot send the key header. The storefront stays open.
- Changes through the API require the key unless the local, loopback-only exception is enabled. Request bodies have a size limit, and only JSON is accepted.
- Static files are served from a fixed list. Identifiers in URLs are validated with a strict pattern.
- The storefront may be framed only by this monitor (`X-Frame-Options` and CSP).
- The browser code writes all text with `textContent`. Nothing is inserted as HTML.
- The storefront's own actions (adding to cart, placing an order) are not behind the API key. This is a demonstration shop with in-memory data; do not expose it on a public network as it is.

## Known limits

- **Speech in the suite is text-level.** The 74-case suite models recognition errors as text, not audio. Real speech is tested only by the single front desk case.
- **The browser's voices are limited.** The monitor's laptop speaker uses the voices installed on the computer. Some computers have only two or three English voices.
- **The store data is in memory and fictional.** Orders, catalogue and policy live in `data/catalog/outdoor_store.json`.
- **One server process, one operator.** There are no user accounts. Storefront sessions and runs are kept per server.
- **Run from a checkout.** The store reads its data files from the repository, so an installed package on its own will not find them.

## Running it for real

**Check the configuration first.** It prints the effective settings (secrets only as set or not set) and every
problem, and the server refuses to start while there are problems:

```bash
voiceshop check-config
```

**Named keys with roles.** The shared `VOICESHOP_API_KEY` still works, but each person should have their own key:

```bash
voiceshop keys create --name qe-lead --role admin      # shown once; only its hash is stored
voiceshop keys create --name qe-analyst --role operator
voiceshop keys list
voiceshop keys revoke --name qe-analyst
```

| Role | Can |
|---|---|
| viewer | read transcripts, runs and the audit trail (with a key) |
| operator | start and stop test suites |
| admin | everything above, change settings, clear history |

Every start, stop, settings change and history clear is recorded against the person who did it, with the request id.
Changes are rate limited per person, and requests are rate limited per client address.

**Retention.** Run transcripts are kept for 14 days (`VOICESHOP_RETENTION_DAYS`). The server removes older ones at start-up and once a day. The audit trail is kept. To remove them by hand:

```bash
voiceshop prune --older-than-days 90 --dry-run
voiceshop prune --older-than-days 90
```

**Containers and TLS.** `Dockerfile`, `docker-compose.yml`, and the TLS proxy (`deploy/docker-compose.proxy.yml` with `deploy/Caddyfile.compose`) are included. The whole stack was started and tested through the proxy on 2026-11-14 (health, authentication, security headers). For a public site, replace `localhost` in `deploy/Caddyfile.compose` with your domain name. `deploy/Caddyfile` is the version for a proxy running on the host rather than in a container. The monitor listens
on loopback behind a TLS proxy. Set `VOICESHOP_TRUST_PROXY=1` only when the proxy adds `X-Forwarded-For`.
See `.env.example` for the variables.

Build and run the image (checked on 2026-11-14 with Docker 29.5, on Windows):

```bash
docker build -t voiceshop:0.1.0 .
docker run --rm -v voiceshop-data:/data voiceshop:0.1.0 voiceshop keys create --name ops --role admin   # copy the key
docker run -d --name voiceshop -p 127.0.0.1:8765:8765 -v voiceshop-data:/data voiceshop:0.1.0
curl http://127.0.0.1:8765/readyz
```

Verified: the image builds; it refuses to serve on a public address without a key; it runs as a non-root user;
the health check reports healthy; an unauthenticated change is refused with 401 and an authenticated one is
accepted; data is kept in the volume.

**Stopping.** On SIGTERM or Ctrl+C the server stops accepting requests, asks running suites to stop, and waits
up to 30 seconds. Runs still marked running after a restart are recorded as interrupted.

**Service levels and alerts:** `deploy/slo.md`. **Data handling:** `deploy/data-governance.md`.
**API description:** `deploy/openapi.json` (checked against the live routes by the tests).

## Quality of the code and the service

| Check | Command | Status |
|---|---|---|
| Unit, integration, security and fault tests | `pytest -q --cov=voiceshop` | 90% line coverage; CI requires 75% |
| Dependency vulnerabilities | `pip-audit --skip-editable` | no known vulnerabilities (CI) |
| Browser end-to-end checks (rules router only) | `cd e2e && npm install && npx playwright install chromium && npm test` | eight checks; CI job |
| Load (storefront) | `python tools/loadtest.py --workers 20 --requests 50` | recorded in `deploy/load-results.md` |
| Judge drift against the accepted reference | `voiceshop judge-drift --current <calibration.json>` | reference is the local model's current behaviour |

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`:

1. lint (`ruff check src tests`)
2. unit, integration and security tests with the coverage floor, then the dependency audit
3. the smoke, targeted and full regression tiers (`voiceshop eval`)
4. the regression gate against the saved baseline (`voiceshop regress`)
5. a separate job for the browser end-to-end checks

Reports are uploaded as build artifacts.

## License

MIT. See [LICENSE](LICENSE).

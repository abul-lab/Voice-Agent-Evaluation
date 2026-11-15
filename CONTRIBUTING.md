# Contributing

Thanks for looking at this project. It is an evaluation harness, so most useful contributions change what is tested or how a result is judged.

## Set up

Python 3.11 or later (CI uses 3.12). The runtime has no third-party dependencies.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Before you open a pull request

Run the same checks as CI:

```bash
ruff check src tests
pytest -q --cov=voiceshop --cov-fail-under=75
voiceshop eval --tags smoke
voiceshop eval --out results/latest
voiceshop regress --report results/latest/report.json
```

The browser checks are optional locally. They need Node 22:

```bash
cd e2e && npm install && npx playwright install chromium && npm test
```

## Rules for changes

- **A new behaviour needs a test.** Put unit and integration tests in `tests/`. Scenario changes go in `scenarios/store_scenarios.json`, with checks that read the transcript, the audit log or the store data. An agent's own claim is not evidence.
- **Do not weaken a gate to make a change pass.** If a case regresses, fix the agent or explain the change. Changing `tests/baseline/store.json` is a deliberate act: say why in the pull request.
- **Do not change the judge's labels or thresholds quietly.** Calibration data in `tests/judge/` and gate thresholds are reviewed decisions.
- **Keep the runtime dependency-free.** Add a dependency only with a clear reason in the pull request.
- **No secrets or personal data** in code, fixtures, reports or commit messages.
- **Update the docs** the change affects: README, `deploy/` documents and `deploy/openapi.json` if an API route changes (the tests check it).

## Commits and pull requests

Use short, prefixed commit messages (`feat:`, `fix:`, `test:`, `docs:`, `security:`, `refactor:`, `ci:`). A pull request should say what changed, why, and what was run to check it.

## Security

Do not report vulnerabilities in a pull request or a public issue. See [SECURITY.md](SECURITY.md).

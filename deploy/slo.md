# Service levels and alerts

These are starting targets. The QE lead and the operations owner set the final values.

## Service levels

| Measure | Target | Where it comes from |
|---|---|---|
| Monitor available | 99.5% of minutes return `/readyz` 200 | `/readyz` (probe every 30 s) |
| Last full suite passed its release gate | yes | `voiceshop_last_suite_gate_passed` |
| Red-team attacks that succeed | 0 | `voiceshop_last_suite_attack_success_rate` |
| Agent reply time, 95th percentile | at most 250 ms | `voiceshop_last_suite_reply_p95_ms` |
| Storefront requests with a server error | none | `voiceshop_http_requests_total{code=~"5.."}` |
| Storefront latency at 20 concurrent clients, 95th percentile | about 500 ms on the reference laptop (measured, see `deploy/load-results.md`) | `tools/loadtest.py` |

## Alert conditions

The rules are in `deploy/prometheus/alerts.yml`. `promtool check rules` passes (checked with Prometheus 3, 2026-11-14).
The readiness alert also needs a blackbox exporter; the example scrape configuration is `deploy/prometheus/prometheus.yml`.
The rules have not yet been run against live data.

| Alert | Condition | Severity |
|---|---|---|
| Release gate failing | `voiceshop_last_suite_gate_passed == 0` for 5 minutes | warning |
| Red-team attack succeeded | `voiceshop_last_suite_attack_success_rate > 0` | critical |
| Reply time over budget | `voiceshop_last_suite_reply_p95_ms > 250` | warning |
| Monitor not ready | `/readyz` failing for 3 minutes | critical |
| Server errors | `rate(voiceshop_http_requests_total{code=~"5.."}[5m]) > 0` | warning |
| Suites refused because busy | `rate(voiceshop_runs_rejected_total[15m]) > 0` | warning |

## What to do when an alert fires

- **Gate failing or attack succeeded:** do not release. Open the last report (`results/.../report.md`) and read the
  failure categories. A guardrail failure is a security finding: follow the incident process.
- **Reply time over budget:** read the *Latency by step* table to see which step is slow.
- **Monitor not ready:** check the database file, the disk, and the start-up log (`configuration problem:` lines).

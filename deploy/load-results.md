# Load test results

Measured with `tools/loadtest.py` against a single monitor process on a Windows development machine
(local SQLite database, no proxy, no TLS), on 2026-11-14. Each request is a storefront read or action.

| Workers | Requests | Requests/s | p50 (ms) | p95 (ms) | Failed |
|---|---|---|---|---|---|
| 20 | 1,000 | 81.9 | 187.9 | 508.1 | 0 |
| 50 | 1,000 | 103.3 | 249.0 | 1,855.2 | 0 |

Commands:

```bash
voiceshop serve --host 127.0.0.1 --port 8790                    # with VOICESHOP_LOCAL_UI_NO_KEY=1 for this test
python tools/loadtest.py --base http://127.0.0.1:8790 --workers 20 --requests 50
python tools/loadtest.py --base http://127.0.0.1:8790 --workers 50 --requests 20
```

What this shows: one process serves about 80 to 100 requests per second with no failures at 20 to 50 concurrent
clients. Latency rises sharply past about 20 concurrent clients, because every storefront change is written to
the database before the response is sent.

What this does not show: behaviour on a server or a network, behaviour behind a proxy, many processes sharing one
database, or the evaluation runs themselves (their agent reply times are measured separately, see the reports).
A capacity plan needs a run on the target hardware.

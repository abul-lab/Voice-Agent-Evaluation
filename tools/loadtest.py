"""Load test for the monitor: concurrent storefront reads and actions against a running server.

Usage: python tools/loadtest.py --base http://127.0.0.1:8765 --workers 20 --requests 50

Reports requests per second, p50 and p95 latency, and the number of failed requests (HTTP errors or
exceptions). Each worker uses its own storefront session, so the test does not measure shared-cart effects.
It changes only storefront sessions, never the back office and never the evaluation results.
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid


def _call(url: str, method: str = "GET", body: dict | None = None, timeout: float = 10.0) -> tuple[int, float]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the operator's own server
            r.read()
            code = r.status
    except urllib.error.HTTPError as err:
        code = err.code
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        code = 0
    return code, (time.perf_counter() - started) * 1000


def worker(base: str, n: int, sid: str, out: list, lock: threading.Lock) -> None:
    for i in range(n):
        if i % 3 == 0:
            code, ms = _call(f"{base}/shop/api/action", "POST", {"sid": sid, "action": "add", "product_id": "tent-01"})
        elif i % 3 == 1:
            code, ms = _call(f"{base}/shop/api/state?sid={sid}")
        else:
            code, ms = _call(f"{base}/shop/api/catalog")
        with lock:
            out.append((code, ms))


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument("--requests", type=int, default=50, help="requests per worker")
    args = ap.parse_args()

    out: list[tuple[int, float]] = []
    lock = threading.Lock()
    threads = [threading.Thread(target=worker, args=(args.base, args.requests, f"load-{uuid.uuid4().hex[:10]}", out, lock))
               for _ in range(args.workers)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - started

    codes = [c for c, _ in out]
    latencies = [ms for c, ms in out if c == 200]
    failed = len(out) - sum(1 for c in codes if c == 200)
    result = {
        "requests": len(out),
        "workers": args.workers,
        "seconds": round(elapsed, 2),
        "requests_per_second": round(len(out) / elapsed, 1),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": round(percentile(latencies, 95), 1) if latencies else None,
        "failed": failed,
        "status_counts": {str(k): codes.count(k) for k in sorted(set(codes))},
    }
    print(json.dumps(result, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

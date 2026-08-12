"""Shared benchmark plumbing: the mock control plane, percentiles, and reporting.

Every benchmark states its method in its own output — run count, warmups
discarded, concurrency, and the machine it ran on — so a number in the README can
be re-derived rather than trusted.
"""

import asyncio
import json
import os
import platform
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

# The default Windows console is cp1252 and will hard-fail on any non-ASCII byte
# mid-benchmark. Benchmarks print ASCII, but a provider's response text is not
# ours to control.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
MOCK_URL = os.environ.get("MOCK_URL", "http://localhost:9000")
RESULTS_DIR = Path(__file__).parent / "results"


@dataclass
class Method:
    """Printed alongside every number. If it is not here, the number is not defensible."""

    runs: int
    warmups_discarded: int
    concurrency: int
    prompts: int
    mock_latency_ms: float
    notes: str = ""
    # Rows already in the `requests` table when the benchmark started. A warm
    # stack measures differently from a cold one — running this suite back to
    # back moved the cache-hit p50 from 3.4ms to 7.9ms — so the number travels
    # with the condition that produced it instead of being quietly incomparable.
    preexisting_rows: int | None = None
    host: dict[str, str] = field(
        default_factory=lambda: {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": str(os.cpu_count()),
        }
    )


def percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "p99": None, "mean": None}
    s = sorted(values)
    return {
        "n": len(s),
        "p50": round(_pct(s, 0.50), 2),
        "p95": round(_pct(s, 0.95), 2),
        "p99": round(_pct(s, 0.99), 2),
        "mean": round(statistics.fmean(s), 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
    }


def _pct(sorted_values: list[float], q: float) -> float:
    idx = min(len(sorted_values) - 1, max(0, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[idx]


async def reset_mock(client: httpx.AsyncClient) -> None:
    await client.post(f"{MOCK_URL}/_control/reset")


async def set_mock(client: httpx.AsyncClient, provider: str, **knobs: Any) -> dict:
    r = await client.post(f"{MOCK_URL}/_control/{provider}", json=knobs)
    r.raise_for_status()
    return r.json()


async def chat(client: httpx.AsyncClient, body: dict) -> tuple[int, dict, float]:
    """Returns (status, json, client-observed latency in ms)."""
    t = time.perf_counter()
    r = await client.post(f"{GATEWAY_URL}/v1/chat", json=body)
    elapsed = (time.perf_counter() - t) * 1000
    try:
        payload = r.json()
    except ValueError:
        payload = {"raw": r.text[:200]}
    return r.status_code, payload, elapsed


async def preexisting_rows(client: httpx.AsyncClient) -> int:
    """How many accounted requests are already on record."""
    try:
        r = await client.get(f"{GATEWAY_URL}/stats", params={"window_minutes": 10_080})
        return int((r.json().get("totals") or {}).get("requests") or 0)
    except (httpx.HTTPError, ValueError, TypeError):
        return -1


async def wait_for_gateway(client: httpx.AsyncClient, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            r = await client.get(f"{GATEWAY_URL}/health", timeout=2.0)
            if r.status_code == 200:
                return
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last = str(e)
        await asyncio.sleep(0.5)
    raise RuntimeError(f"gateway not reachable at {GATEWAY_URL}: {last}")


def save(name: str, payload: dict, method: Method) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {"benchmark": name, "method": asdict(method), **payload}
    path = RESULTS_DIR / f"{name}.json"
    path.write_text(json.dumps(out, indent=2))
    return path


def rule(title: str) -> None:
    print(f"\n{'-' * 72}\n{title}\n{'-' * 72}")


def table(rows: list[dict], columns: list[str]) -> None:
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  ".join(c.ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))

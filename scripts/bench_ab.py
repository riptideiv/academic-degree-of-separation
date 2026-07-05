"""Interleaved A/B cold-search benchmark: working tree vs a baseline git ref.

Removes hour-to-hour OpenAlex latency drift by running both versions as live
servers side by side and ALTERNATING timed cold runs between them (baseline,
working, baseline, working …). Runs never overlap, so the versions don't
compete for API bandwidth; alternation gives both the same "API weather".

The baseline is checked out into a temporary git worktree with its own
isolated neighbor cache; only the OpenAlex credentials (openalex-key, mailto)
from api-keys.json (git-ignored) are copied in so both sides use the same
OpenAlex key without sharing a durable store. Servers and worktree are
cleaned up on exit.

Usage:
    python scripts/bench_ab.py --ref origin/master
    python scripts/bench_ab.py --ref HEAD~1 --rounds 2 --pair "Geoffrey Hinton::Noam Chomsky"

Run this BEFORE committing anything that touches the search/expansion/cache
path — cold-search regressions only show up in benchmarks, not in the test
suite. Each round costs two cold sweeps of one pair — mind the OpenAlex
credit budget.
"""

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("bench_search", REPO / "scripts" / "bench_search.py")
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


async def _wait_healthy(base: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"{base}/health", timeout=2.0)
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError(f"server at {base} never became healthy")


def _start_server(cwd: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--port", str(port)],
        cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def _timed_cold(base: str, a_name: str, b_name: str, edges: list[str] | None) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=bench.TIMEOUT_S) as client:
        a = await bench.resolve(client, a_name)
        b = await bench.resolve(client, b_name)
        await client.delete("/api/cache")
        return await bench.run_case(client, a[0], b[0], edges=edges)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default="origin/master", help="baseline git ref")
    ap.add_argument("--pair", default="Geoffrey Hinton::Noam Chomsky",
                    help="two researcher names separated by '::'")
    ap.add_argument("--rounds", type=int, default=2,
                    help="alternating rounds (each = 1 baseline + 1 working cold run)")
    ap.add_argument("--edges", default="", help="comma-sep edge types (default: all)")
    ap.add_argument("--gap", type=float, default=10.0, help="seconds between runs")
    ap.add_argument("--port-base", type=int, default=8101)
    ap.add_argument("--port-work", type=int, default=8102)
    ap.add_argument("--allow-shared-store", action="store_true",
                    help="proceed even if a server's neighbor store is the shared Supabase table")
    args = ap.parse_args()

    a_name, b_name = args.pair.split("::", 1)
    edges = [e for e in args.edges.split(",") if e] or None

    ref_sha = subprocess.run(
        ["git", "rev-parse", "--short", args.ref], cwd=REPO,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    worktree = Path(tempfile.mkdtemp(prefix="adsep-ab-"))
    procs: list[subprocess.Popen] = []
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(worktree), args.ref],
                       cwd=REPO, check=True, capture_output=True)
        # api-keys.json is git-ignored, so the worktree lacks it — copy in only
        # the OpenAlex credentials so both sides hit OpenAlex with the same key
        # (and rate limits). Anything else (e.g. supabase-db-url) is dropped so
        # the baseline can't attach to the same durable store as the working
        # tree and defeat the isolated-cache premise.
        keys = REPO / "api-keys.json"
        if keys.exists():
            data = json.loads(keys.read_text())
            slim = {k: data[k] for k in ("openalex-key", "mailto") if k in data}
            (worktree / "api-keys.json").write_text(json.dumps(slim))

        print(f"baseline: {args.ref} ({ref_sha}) on :{args.port_base}")
        print(f"working:  {REPO} on :{args.port_work}")
        procs.append(_start_server(worktree, args.port_base))
        procs.append(_start_server(REPO, args.port_work))
        base_url = f"http://127.0.0.1:{args.port_base}"
        work_url = f"http://127.0.0.1:{args.port_work}"
        await _wait_healthy(base_url)
        await _wait_healthy(work_url)
        for url in (base_url, work_url):
            async with httpx.AsyncClient(base_url=url, timeout=10.0) as client:
                await bench.ensure_disposable_store(client, args.allow_shared_store)

        results: dict[str, list[dict]] = {"baseline": [], "working": []}
        for rnd in range(args.rounds):
            for label, url in (("baseline", base_url), ("working", work_url)):
                r = await _timed_cold(url, a_name, b_name, edges)
                results[label].append(r)
                note = " [ABORTED]" if r.get("aborted") else ""
                print(f"round {rnd + 1} {label}: cold {r['seconds']:.1f}s (hops={r['hops']}){note}",
                      flush=True)
                await asyncio.sleep(args.gap)

        print(f"\n== A/B {a_name} <-> {b_name} (edges={args.edges or 'all'}, "
              f"{args.rounds} interleaved rounds) ==")
        for label in ("baseline", "working"):
            runs = results[label]
            avg = sum(r["seconds"] for r in runs) / len(runs)
            times = ", ".join(f"{r['seconds']:.1f}s" for r in runs)
            hops = {r["hops"] for r in runs}
            print(f"{label:<9} avg {avg:6.1f}s  ({times})  hops={sorted(hops, key=str)}")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=REPO, capture_output=True)
        subprocess.run(["git", "worktree", "prune"], cwd=REPO, capture_output=True)


if __name__ == "__main__":
    asyncio.run(main())

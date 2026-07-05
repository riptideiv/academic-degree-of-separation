"""Benchmark the /api/graph/expand flow (path search + neighborhood build).

For each researcher pair: optionally wipe the server cache (cold), add origin A
(its own expansion), then time adding origin B (path search to A + expansion) —
the flow the UI runs when you add a second researcher. Warm runs repeat the
timed step immediately, so every ring/metadata fetch is cached.

Usage:
    python scripts/bench_search.py [--base http://127.0.0.1:8000] [--label NAME]
"""

import argparse
import asyncio
import json
import time

import httpx

PAIRS = [
    ("Geoffrey Hinton", "Noam Chomsky"),
    ("Yann LeCun", "Yoshua Bengio"),
    ("Terence Tao", "Geoffrey Hinton"),
    ("Jennifer Doudna", "Yoshua Bengio"),
    ("Albert-Laszlo Barabasi", "Noam Chomsky"),
]

# Cross-field pairs for --set hard; combine with --edges coauthor,citation so
# the institution shortcut (top colleagues of any big university) can't make
# everything 2 hops. This is where frontiers explode and fast mode matters.
HARD_PAIRS = [
    ("Noam Chomsky", "Jennifer Doudna"),
    ("Terence Tao", "Jane Goodall"),
    ("Donald Knuth", "Sylvia Earle"),
]

DEPTH = 2
TOP_K = 6  # mirrors the UI's "Small" neighborhood
TIMEOUT_S = 300.0


async def resolve(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    # Retry through transient OpenAlex 429s surfaced as 500s by the app.
    for attempt in range(5):
        r = await client.get("/api/authors", params={"q": name, "per_page": 1})
        if r.status_code >= 500 and attempt < 4:
            await asyncio.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        top = r.json()["results"][0]
        return top["id"], top["display_name"]
    raise RuntimeError(f"could not resolve {name}")


async def consume_expand(client: httpx.AsyncClient, params: dict) -> dict:
    """Stream one /api/graph/expand call to completion; return timing + counts."""
    t0 = time.perf_counter()
    counts = {"node": 0, "edge": 0, "expansion_nodes": 0, "expansion_edges": 0}
    hops = None
    event = None
    async with client.stream("GET", "/api/graph/expand", params=params) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: ") and event:
                if event == "done":
                    break
                data = json.loads(line[6:])
                if event == "node":
                    counts["node"] += 1
                elif event == "edge":
                    counts["edge"] += 1
                elif event == "expansion":
                    counts["expansion_nodes"] += len(data.get("nodes", []))
                    counts["expansion_edges"] += len(data.get("edges", []))
                elif event == "path":
                    hops = data.get("hops")
                elif event == "app_error":
                    raise RuntimeError(f"app_error: {data.get('message')}")
            elif line == "" :
                if event == "done":
                    break
    return {"seconds": time.perf_counter() - t0, "hops": hops, **counts}


async def run_case(
    client: httpx.AsyncClient, a_id: str, b_id: str,
    edges: list[str] | None = None, abort_after: float | None = None,
) -> dict:
    base: dict = {"depth": DEPTH, "top_k": TOP_K}
    if edges:
        base["edges"] = edges
    # Add origin A alone (not timed) so B's run includes the A<->B path search.
    await consume_expand(client, {"new_id": a_id, **base})
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(
            consume_expand(client, {"new_id": b_id, "origin_ids": a_id, **base}),
            timeout=abort_after,
        )
    except asyncio.TimeoutError:
        return {"seconds": time.perf_counter() - t0, "hops": None, "aborted": True}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--label", default="run")
    ap.add_argument("--set", choices=["default", "hard"], default="default")
    ap.add_argument("--edges", default="", help="comma-sep edge types (default: all)")
    ap.add_argument("--abort-after", type=float, default=None,
                    help="give up on a timed case after this many seconds")
    args = ap.parse_args()
    pairs = HARD_PAIRS if args.set == "hard" else PAIRS
    edges = [e for e in args.edges.split(",") if e] or None

    async with httpx.AsyncClient(base_url=args.base, timeout=TIMEOUT_S) as client:
        ids = {}
        for name in {n for pair in pairs for n in pair}:
            ids[name] = await resolve(client, name)

        rows = []
        for a_name, b_name in pairs:
            a_id, _ = ids[a_name]
            b_id, _ = ids[b_name]

            await client.delete("/api/cache")
            cold = await run_case(client, a_id, b_id,
                                  edges=edges, abort_after=args.abort_after)
            warm = await run_case(client, a_id, b_id,
                                  edges=edges, abort_after=args.abort_after)
            rows.append((a_name, b_name, cold, warm))
            note = " [ABORTED]" if cold.get("aborted") or warm.get("aborted") else ""
            print(
                f"{a_name} <-> {b_name}: cold {cold['seconds']:.1f}s "
                f"(hops={cold['hops']}), warm {warm['seconds']:.1f}s (hops={warm['hops']})"
                + note,
                flush=True,
            )

        print(f"\n== {args.label} (set={args.set}, edges={args.edges or 'all'}, "
              f"depth={DEPTH}, top_k={TOP_K}) ==")
        print(f"{'pair':<40} {'cold_s':>7} {'warm_s':>7} {'hops':>4}")
        for a, b, cold, warm in rows:
            print(f"{a + ' <-> ' + b:<40} {cold['seconds']:>7.1f} {warm['seconds']:>7.1f} "
                  f"{str(cold['hops']):>4}")
        cold_avg = sum(c["seconds"] for _, _, c, _ in rows) / len(rows)
        warm_avg = sum(w["seconds"] for _, _, _, w in rows) / len(rows)
        print(f"{'AVERAGE':<40} {cold_avg:>7.1f} {warm_avg:>7.1f}")


if __name__ == "__main__":
    asyncio.run(main())

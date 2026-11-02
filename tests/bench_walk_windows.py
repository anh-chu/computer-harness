"""Time get_app_state's tree walk: old engine (one UIA call per property) vs
current (one CacheRequest call per node). CI writes the old engine to
computer_harness/engine_win_old.py from the base commit first.

    python tests/bench_walk_windows.py <window title> <out.json>
"""
import json
import statistics
import sys
import time

from computer_harness import engine_win, engine_win_old


def bench(engine, title, runs=5):
    times, count = [], 0
    for _ in range(runs):
        t0 = time.perf_counter()
        st = engine.get_app_state(title, max_elements=400, capture=False)
        times.append((time.perf_counter() - t0) * 1000)
        count = len(st["elements"])
    return statistics.median(times), count


def main():
    title, out = sys.argv[1], sys.argv[2]
    before, n_before = bench(engine_win_old, title)
    after, n_after = bench(engine_win, title)
    print(f"old: {before:.0f} ms ({n_before} elements)  new: {after:.0f} ms ({n_after} elements)")
    with open(out, "w") as f:
        json.dump([{"name": f"get_app_state('{title}')", "before": round(before), "after": round(after),
                    "unit": "ms", "better": "lower",
                    "context": f"{n_before} vs {n_after} elements, median of 5"}], f)
    if n_after < n_before * 0.9:
        sys.exit(f"new walk found fewer elements ({n_after} < {n_before})")


if __name__ == "__main__":
    main()

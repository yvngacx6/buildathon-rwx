"""Split selected tests across workers to minimise the scorer's cost.

Cost of a commit = largest worker load + 2 s per worker (all in whole ms).
We try every worker count k = 1..8 with longest-first (LPT) packing and keep the cheapest.
"""

from __future__ import annotations

import heapq

WORKER_COST_MS = 2_000
MAX_WORKERS = 8


def duration_ms(seconds: float) -> int:
    """The scorer's duration of a test: round(seconds * 1000)."""
    return round(seconds * 1000)


def lpt(tests: list[tuple[int, int]], k: int) -> tuple[list[list[int]], int]:
    """Pack (number, ms) pairs onto k workers, longest first. Returns (workers, max load)."""
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k
    heap = [(0, i) for i in range(k)]  # (load, worker): the least loaded, lowest index first
    for number, ms in sorted(tests, key=lambda t: (-t[1], t[0])):
        load, i = heap[0]
        workers[i].append(number)
        loads[i] = load + ms
        heapq.heapreplace(heap, (loads[i], i))
    workers = [w for w in workers if w]
    return workers, max(loads) if loads else 0


def cost_ms(workers: list[list[int]], ms_of: dict[int, int]) -> int:
    """The scorer's cost of one commit."""
    workers = [w for w in workers if w]
    if not workers:
        return 0
    return max(sum(ms_of[n] for n in w) for w in workers) + WORKER_COST_MS * len(workers)


def pack(numbers: set[int], ms_of: dict[int, int]) -> list[list[int]]:
    """Best packing of `numbers` over 1..8 workers. Empty set → no workers."""
    if not numbers:
        return []
    tests = [(n, ms_of[n]) for n in numbers]
    best: tuple[int, list[list[int]]] | None = None
    for k in range(1, min(MAX_WORKERS, len(tests)) + 1):
        workers, load = lpt(tests, k)
        cost = load + WORKER_COST_MS * len(workers)
        if best is None or cost < best[0]:
            best = (cost, workers)
    assert best is not None
    return best[1]

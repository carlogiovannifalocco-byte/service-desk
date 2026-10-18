"""End-to-end check against the real Tavily and Nebius Token Factory APIs.

    export NEBIUS_API_KEY=... TAVILY_API_KEY=...
    python scripts/smoke_test.py

Runs one Tavily search, one cited plan (search + model) and one letter explanation (model only),
then prints what came back and how long each step took. Exits 1 if anything fails.
Optional: NEBIUS_MODEL, NEBIUS_BASE_URL, NEBIUS_REASONING_EFFORT, NEBIUS_MAX_TOKENS (see README).
Uses a few Tavily credits (advanced search costs 2 per call) and a few thousand model tokens.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from service_desk import clients, core, demo  # noqa: E402


TIMINGS: list[str] = []


def timed(name, fn):
    """Wrap a client call so each step can report how long search and the model took."""
    def wrapper(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            TIMINGS.append(f"{name} {time.perf_counter() - t:.1f}s")
    return wrapper


def step(name, fn):
    print(f"\n== {name}")
    TIMINGS.clear()
    t = time.perf_counter()
    try:
        out = fn()
    except Exception as exc:  # report and keep going, so one run shows every problem
        print(f"   FAILED after {time.perf_counter() - t:.1f}s: {type(exc).__name__}: {exc}")
        return None, False
    parts = f" ({', '.join(TIMINGS)})" if TIMINGS else ""
    print(f"   ok in {time.perf_counter() - t:.1f}s{parts}")
    return out, True


def main() -> int:
    missing = [k for k in ("NEBIUS_API_KEY", "TAVILY_API_KEY") if not os.getenv(k)]
    if missing:
        print("Set " + " and ".join(missing) + " first.")
        return 2
    print(f"model:    {os.getenv('NEBIUS_MODEL', clients.DEFAULT_MODEL)}")
    print(f"endpoint: {os.getenv('NEBIUS_BASE_URL', clients.DEFAULT_BASE_URL)}")
    clients.search = timed("search", clients.search)
    clients.chat_json = timed("model", clients.chat_json)
    ok = True

    found, passed = step("Tavily search (official domains only)",
                         lambda: clients.search("codice fiscale stranieri Agenzia delle Entrate"))
    ok &= passed
    for s in found or []:
        print(f"   - {s['url']}")
    if passed and not found:
        print("   (no official results: the search worked but returned nothing usable)")

    plan, passed = step("Plan: codice fiscale, Milano, Brazil",
                        lambda: core.plan("codice_fiscale", "Milano", "Brazil"))
    ok &= passed
    if plan:
        print(f"   summary: {plan['summary'][:200]}")
        print(f"   {len(plan['steps'])} cited step(s), {len(plan['sources'])} source(s), {len(plan['gaps'])} gap(s)")
        for i, s in enumerate(plan["steps"], 1):
            print(f"   {i}. {s.get('title', '')}  [sources {s['sources']}]")
        for g in plan["gaps"]:
            print(f"   gap: {g}")
        if not plan["steps"]:
            print("   WARNING: no cited steps came back")
            ok = False

    letter, passed = step("Letter: fictional sample, English",
                          lambda: core.explain_letter(demo.SAMPLE_LETTER, "en"))
    ok &= passed
    if letter:
        print(f"   urgency: {letter.get('urgency')}  sender: {letter.get('sender')}")
        print(f"   summary: {str(letter.get('summary', ''))[:200]}")
        for d in letter["deadlines"]:
            print(f"   deadline {d['date']} ({d['what']}), written as \"{d['match']}\"")
        # The sample letter states two dates: 15 novembre 2026 and 30/11/2026.
        if {d["date"] for d in letter["deadlines"]} != {"2026-11-15", "2026-11-30"}:
            print("   WARNING: expected the deadlines 2026-11-15 and 2026-11-30")

    print("\nALL OK" if ok else "\nSOMETHING FAILED (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

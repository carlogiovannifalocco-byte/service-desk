"""Citation evaluation: run a fixed set of requests through the real pipeline and measure how well the plans are
backed by official sources.

    python scripts/eval_citations.py              # live: needs NEBIUS_API_KEY and TAVILY_API_KEY
    python scripts/eval_citations.py --demo       # offline, canned data: only checks that the script works
    python scripts/eval_citations.py --limit 3 --lang it

For each request it records, from the plan the API would return:
- claims: steps + documents + deadlines that survived the citation filter (each cites an official source);
- removed: items the model proposed without a valid citation (dropped in code);
- quoted: claims whose supporting quote was found word for word on the cited official page;
- pages read with Tavily Extract, pages that state a "last updated" date, and how many of those are over two years old;
- whether every source URL passes the official-domain check, and the wall-clock time.

Totals are micro-averages over all claims. Results go to eval/results-<date>[-demo].json and a Markdown table is
printed. Nothing is estimated: every number comes from a run you can repeat. Cost per live request: one advanced
search (2 credits), one basic extract (1 credit per 5 pages) and one model call.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service_desk import clients, core, demo  # noqa: E402

CASES = [
    ("codice_fiscale", "Milano", "Brazil", ""),
    ("residency", "Milano", "India", ""),
    ("residency", "Bologna", "EU", ""),
    ("health_card", "Torino", "Philippines", ""),
    ("permit_renewal", "Roma", "Morocco", ""),
    ("spid", "Napoli", "EU", ""),
    ("identity_card", "Firenze", "United States", ""),
    ("isee", "Roma", "EU", ""),
    ("driving_licence", "Milano", "Brazil", ""),
    ("lease_registration", "Bologna", "EU", ""),
    ("partita_iva", "Torino", "United Kingdom", ""),
    ("other", "Milano", "Ukraine", "How do I enrol my child in a public primary school?"),
]


def run_case(task: str, city: str, nationality: str, question: str, lang: str) -> dict:
    t0 = time.perf_counter()
    row = {"task": task, "city": city, "nationality": nationality, "question": question}
    try:
        p = core.plan(task, city, nationality, question=question, lang=lang)
    except (clients.ServiceError, ValueError) as exc:
        return {**row, "error": str(exc), "seconds": round(time.perf_counter() - t0, 1)}
    v = p.get("verification") or {}
    dated = [s["updated"] for s in p["sources"] if s.get("updated")]
    return {
        **row,
        "seconds": round(time.perf_counter() - t0, 1),
        "sources": len(p["sources"]),
        "all_official": all(clients.is_official(s["url"]) for s in p["sources"]),
        "steps": len(p["steps"]),
        "claims": v.get("claims", 0),
        "removed": v.get("removed_uncited", 0),
        "quoted": v.get("quoted", 0),
        "pages_read": v.get("pages_read", 0),
        "pages_dated": len(dated),
        "pages_stale": sum(1 for d in dated if d["stale"]),
        "page_check": v.get("page_check", "no sources"),
        "gaps": len(p["gaps"]),
    }


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if "error" not in r]
    claims = sum(r["claims"] for r in ok)
    proposed = claims + sum(r["removed"] for r in ok)
    pages = sum(r["sources"] for r in ok)
    return {
        "requests": len(rows),
        "errors": len(rows) - len(ok),
        "plans_with_steps": sum(1 for r in ok if r["steps"]),
        "claims": claims,
        "citation_kept_rate": round(claims / proposed, 3) if proposed else None,
        "quote_verified_rate": round(sum(r["quoted"] for r in ok) / claims, 3) if claims else None,
        "sources": pages,
        "all_sources_official": all(r["all_official"] for r in ok),
        "pages_read_rate": round(sum(r["pages_read"] for r in ok) / pages, 3) if pages else None,
        "pages_dated": sum(r["pages_dated"] for r in ok),
        "pages_stale": sum(r["pages_stale"] for r in ok),
        "median_seconds": sorted(r["seconds"] for r in ok)[len(ok) // 2] if ok else None,
    }


def table(rows: list[dict]) -> str:
    out = ["| task | city | sources | claims | removed | quoted | pages read | dated (stale) | s |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            out.append(f"| {r['task']} | {r['city']} | error: {r['error'][:60]} | | | | | | {r['seconds']} |")
        else:
            out.append(f"| {r['task']} | {r['city']} | {r['sources']} | {r['claims']} | {r['removed']} | {r['quoted']} "
                       f"| {r['pages_read']} | {r['pages_dated']} ({r['pages_stale']}) | {r['seconds']} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--demo", action="store_true", help="canned data, no API calls (checks the script only)")
    ap.add_argument("--limit", type=int, default=len(CASES))
    ap.add_argument("--lang", default="en", choices=sorted(core.LANGUAGES))
    ap.add_argument("--out", default="", help="output file (default eval/results-<date>[-demo].json)")
    args = ap.parse_args(argv)
    if args.demo:
        demo.install()
    rows = []
    for case in CASES[: args.limit]:
        rows.append(run_case(*case, lang=args.lang))
        print(f"{rows[-1]['task']:<20} {rows[-1]['city']:<9} "
              + (f"error: {rows[-1]['error']}" if "error" in rows[-1] else
                 f"{rows[-1]['claims']} claims, {rows[-1]['quoted']} quoted, {rows[-1]['seconds']} s"), flush=True)
    summary = summarize(rows)
    result = {"date": date.today().isoformat(), "mode": "demo (canned data)" if args.demo else "live",
              "model": None if args.demo else clients.os.getenv("NEBIUS_MODEL", clients.DEFAULT_MODEL),
              "lang": args.lang, "summary": summary, "rows": rows}
    out = Path(args.out or Path(__file__).resolve().parents[1] / "eval" /
               f"results-{result['date']}{'-demo' if args.demo else ''}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print("\n" + table(rows) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"\nwritten to {out}" + ("  (DEMO: canned data, not a measurement)" if args.demo else ""))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""The verification report: every claim in a plan checked against the exact text of the official page it cites.

Three checks, all deterministic (no model involved):
- quote: the passage the model says supports a step must appear word for word in a source it cites, either in
  the search excerpt or in the full page read with Tavily Extract;
- freshness: the date an official page says it was last updated ("Ultimo aggiornamento: 12/03/2025", the
  Designers Italia template most comuni and ministries use), and whether it is older than two years;
- provenance: when each page was retrieved.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone

MONTHS_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre",
             "ottobre", "novembre", "dicembre"]
STALE_DAYS = 730
MIN_QUOTE_CHARS = 12  # shorter "quotes" ("the fee", "SPID") would match almost anywhere

_DATE = (r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})|(\d{4})-(\d{2})-(\d{2})|(\d{1,2})\s+(" + "|".join(MONTHS_IT)
         + r")\s+(\d{4})")
_UPDATED = re.compile(
    r"(ultimo aggiornamento|ultima modifica|ultima revisione|aggiornat[oa] (?:il|al)|data (?:di )?aggiornamento"
    r"|data (?:di )?pubblicazione|pubblicat[oa] il|last updated|last modified|updated on)"
    r"\s*[:\-–]?\s*(?:il\s+|del\s+|al\s+)?(?:" + _DATE + ")",
    re.I,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm(text: str) -> str:
    """Compare text the way a reader would: same letters, any quotes, any whitespace, any case."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "´": "'", "`": "'", "“": '"',
                                         "”": '"', "«": '"', "»": '"', "–": "-", "—": "-",
                                         " ": " "}))
    return " ".join(text.lower().split())


def quote_in(quote: str, text: str) -> bool:
    """True if the quote appears verbatim in the text. An ellipsis may join parts that appear in order."""
    parts = [norm(p).strip(" .,;:\"'") for p in re.split(r"\.\.\.|…|\[\.\.\.\]", quote or "")]
    parts = [p for p in parts if p]
    if not parts or sum(len(p) for p in parts) < MIN_QUOTE_CHARS:
        return False
    hay, pos = norm(text), 0
    for p in parts:
        at = hay.find(p, pos)
        if at < 0:
            return False
        pos = at + len(p)
    return True


def check_quotes(items: list[dict], texts: dict[int, list[str]]) -> list[dict]:
    """Mark each item verified when its quote is in one of the sources it cites; drop quotes that are not."""
    out = []
    for item in items:
        quote = str(item.get("quote") or "").strip()
        found = next((n for n in item.get("sources", []) if any(quote_in(quote, t) for t in texts.get(n, []))), None)
        if found is not None:
            out.append({**item, "quote": quote, "quote_source": found, "verified": True})
        else:
            # A quote we could not find on the page is not shown: it may be paraphrased or made up.
            out.append({**{k: v for k, v in item.items() if k not in ("quote", "quote_source")}, "quote": "",
                        "verified": False})
    return out


def _to_date(m: re.Match) -> date | None:
    g = m.groups()[1:]
    try:
        if g[0]:
            return date(int(g[2]), int(g[1]), int(g[0]))
        if g[3]:
            return date(int(g[3]), int(g[4]), int(g[5]))
        return date(int(g[8]), MONTHS_IT.index(g[7].lower()) + 1, int(g[6]))
    except ValueError:
        return None


def page_updated(text: str, today: date | None = None) -> dict | None:
    """The 'last updated' date a page states about itself, with the words it uses, or None if it states none."""
    today = today or date.today()
    best = None
    for m in _UPDATED.finditer(text or ""):
        d = _to_date(m)
        if d and d <= today and (best is None or d > best[0]):
            best = (d, " ".join(m.group(0).split()))
    if not best:
        return None
    return {"date": best[0].isoformat(), "text": best[1], "stale": (today - best[0]).days > STALE_DAYS}


def report(groups: dict[str, list[dict]], sources: list[dict], dropped: int, page_status: str) -> dict:
    """Totals for the 'Verification report' card. Counts only; every number is recomputable from the plan."""
    items = [i for g in groups.values() for i in g]
    quoted = sum(1 for i in items if i.get("verified"))
    return {
        "checked_at": now_iso(),
        "claims": len(items),
        "quoted": quoted,
        "quote_coverage": round(quoted / len(items), 2) if items else 0.0,
        "removed_uncited": dropped,
        "pages_read": sum(1 for s in sources if s.get("page_read")),
        "pages_total": len(sources),
        "pages_dated": sum(1 for s in sources if s.get("updated")),
        "stale_sources": [s["n"] for s in sources if (s.get("updated") or {}).get("stale")],
        "page_check": page_status,
    }

"""Service Desk's two jobs: a cited step-by-step plan for a bureaucratic task, and a plain explanation of an official letter."""

from __future__ import annotations

import io
import re
import uuid
from datetime import date, datetime, timezone

from pypdf import PdfReader

from . import clients

TASKS = {
    "codice_fiscale": "getting an Italian tax code (codice fiscale)",
    "residency": "registering residency at the comune (iscrizione anagrafica / residenza)",
    "health_card": "registering with the national health service (SSN) and choosing a family doctor",
    "permit_renewal": "renewing a residence permit (permesso di soggiorno)",
    "spid": "getting a SPID digital identity",
    "identity_card": "getting an electronic identity card (CIE)",
    "isee": "getting an ISEE certificate",
}

PLAN_SYSTEM = """You help people in Italy (often foreigners) with public administration tasks.
You receive the person's situation and numbered SOURCES from official Italian websites.
Write a practical plan in plain English. Rules:
- Use ONLY facts stated in the SOURCES. Every step must cite at least one source number in "sources".
- If the sources do not cover something important, say so in "gaps" instead of guessing.
- Fees, deadlines and documents must come from the sources; never invent amounts.
- Note when rules differ by comune or nationality.
Reply with JSON:
{"summary": str, "steps": [{"title": str, "detail": str, "where": str, "sources": [int]}],
 "documents": [{"name": str, "sources": [int]}], "deadlines": [{"what": str, "rule": str, "sources": [int]}],
 "gaps": [str]}"""


def _sources_block(sources: list[dict]) -> str:
    return "\n\n".join(f"[{i}] {s['title']} ({s['url']})\n{s['content']}" for i, s in enumerate(sources, 1))


def _keep_cited(items: list[dict], n_sources: int) -> list[dict]:
    """Drop citations to sources that do not exist, and any item left with no valid citation."""
    kept = []
    for item in items or []:
        refs = [r for r in item.get("sources", []) if isinstance(r, int) and 1 <= r <= n_sources]
        if refs:
            kept.append({**item, "sources": refs})
    return kept


def plan(task: str, city: str, nationality: str, situation: str = "") -> dict:
    if task not in TASKS:
        raise ValueError(f"unknown task, choose one of {sorted(TASKS)}")
    is_eu = nationality.strip().lower() in ("eu", "eu citizen")
    eu = "EU citizen" if is_eu else f"citizen of {nationality}"
    who = "EU citizens" if is_eu else "non-EU foreigners"
    query = f"{TASKS[task]} {city} requirements documents procedure {who}"
    sources = clients.search(query)
    if not sources:
        return {"summary": "", "steps": [], "documents": [], "deadlines": [], "sources": [],
                "gaps": ["No official source was found for this request. Check your comune's website directly."]}
    user = (
        f"Task: {TASKS[task]}\nCity: {city}\nPerson: {eu}\nSituation: {situation or 'not specified'}\n\n"
        f"SOURCES:\n{_sources_block(sources)}"
    )
    raw = clients.chat_json(PLAN_SYSTEM, user)
    n = len(sources)
    steps = _keep_cited(raw.get("steps"), n)
    dropped = len(raw.get("steps") or []) - len(steps)
    gaps = list(raw.get("gaps") or [])
    if dropped:
        gaps.append(f"{dropped} step(s) were removed because they were not backed by an official source.")
    return {
        "summary": raw.get("summary", ""),
        "steps": steps,
        "documents": _keep_cited(raw.get("documents"), n),
        "deadlines": _keep_cited(raw.get("deadlines"), n),
        "gaps": gaps,
        "sources": [{"n": i, "title": s["title"], "url": s["url"]} for i, s in enumerate(sources, 1)],
    }


LETTER_SYSTEM = """You explain official Italian letters (Agenzia delle Entrate, INPS, comune, prefettura, questura...)
to people who may not read Italian well. Use only what the letter says. Reply with JSON:
{"sender": str, "kind": str, "summary": str (plain English, max 5 sentences),
 "actions": [str], "deadlines": [{"what": str, "date": "YYYY-MM-DD"}],
 "amounts": [{"what": str, "euro": number}], "urgency": "low"|"medium"|"high",
 "words_to_know": [{"italian": str, "english": str}]}
Only put a date in deadlines if the letter states an exact date. If the letter is not an official letter, say so in summary."""

MAX_LETTER_CHARS = 15000


def letter_text(filename: str, data: bytes) -> str:
    if filename.lower().endswith(".pdf"):
        text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages)
    else:
        text = data.decode("utf-8", errors="replace")
    text = text.strip()
    if not text:
        raise ValueError("No text found. Scanned letters need a PDF with a text layer, or paste the text.")
    return text[:MAX_LETTER_CHARS]


def explain_letter(text: str) -> dict:
    raw = clients.chat_json(LETTER_SYSTEM, text)
    deadlines = []
    for d in raw.get("deadlines") or []:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d.get("date", ""))):
            try:
                date.fromisoformat(d["date"])
            except ValueError:
                continue
            if any(form in text for form in _date_forms(d["date"])):
                deadlines.append(d)
    raw["deadlines"] = deadlines  # only dates that actually appear in the letter
    return raw


MONTHS_IT = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
             "settembre", "ottobre", "novembre", "dicembre"]


def _date_forms(iso: str) -> list[str]:
    """How an ISO date may be written in an Italian letter."""
    d = date.fromisoformat(iso)
    return [iso, f"{d:%d/%m/%Y}", f"{d:%d.%m.%Y}", f"{d:%d-%m-%Y}", f"{d.day}/{d.month}/{d.year}",
            f"{d.day} {MONTHS_IT[d.month - 1]} {d.year}", f"{d:%d} {MONTHS_IT[d.month - 1]} {d.year}"]


def ics(events: list[dict]) -> str:
    """An iCalendar file with one all-day event per deadline ({'what', 'date'})."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Service Desk//EN", "CALSCALE:GREGORIAN"]
    for e in events:
        day = date.fromisoformat(e["date"]).strftime("%Y%m%d")
        summary = re.sub(r"[\r\n]+", " ", e["what"]).replace(",", "\\,").replace(";", "\\;")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uuid.uuid4()}@service-desk",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day}",
            f"SUMMARY:{summary}",
            "BEGIN:VALARM", "TRIGGER:-P3D", "ACTION:DISPLAY", f"DESCRIPTION:{summary}", "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"

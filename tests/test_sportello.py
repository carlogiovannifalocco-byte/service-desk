import pytest
from fastapi.testclient import TestClient

from service_desk import clients, core, demo
from service_desk.main import app

SOURCES = [
    {"title": "Codice fiscale - Agenzia Entrate", "url": "https://www.agenziaentrate.gov.it/portale/codice-fiscale", "content": "Bring passport. Free of charge."},
    {"title": "Anagrafe - Comune di Milano", "url": "https://www.comune.milano.it/servizi/residenza", "content": "Declare residency within 20 days."},
]


def test_official_domain_filter():
    assert clients.is_official("https://www.agenziaentrate.gov.it/x")
    assert clients.is_official("https://www.comune.bergamo.it/page")
    assert clients.is_official("https://comune.castel-san-pietro-terme.bo.it/")
    assert not clients.is_official("https://agenziaentrate.gov.it.evil.com/")
    assert not clients.is_official("https://www.expat-blog.com/codice-fiscale")


def test_search_drops_unofficial_results(monkeypatch):
    sent = {}

    def fake_post(url, headers, json, timeout):
        sent.update(json)
        return clients.httpx.Response(200, json={"results": SOURCES + [
            {"title": "blog", "url": "https://blog.example.com", "content": "x"}]})

    monkeypatch.setenv("TAVILY_API_KEY", "k")
    monkeypatch.setattr(clients.httpx, "post", fake_post)
    out = clients.search("codice fiscale")
    assert [r["url"] for r in out] == [s["url"] for s in SOURCES]
    assert "agenziaentrate.gov.it" in sent["include_domains"]


def test_plan_keeps_only_cited_steps(monkeypatch):
    monkeypatch.setattr(clients, "search", lambda q: SOURCES)
    seen = {}

    def fake_chat(system, user, max_tokens=2000):
        seen["user"] = user
        return {
            "summary": "Get the code at the tax office.",
            "steps": [
                {"title": "Go to Agenzia delle Entrate", "detail": "Bring passport", "where": "any office", "sources": [1]},
                {"title": "Pay 50 euro", "detail": "invented", "where": "", "sources": []},
                {"title": "Fake citation", "detail": "", "where": "", "sources": [9]},
            ],
            "documents": [{"name": "Passport", "sources": [1]}],
            "deadlines": [{"what": "Declare residency", "rule": "within 20 days", "sources": [2, 7]}],
            "gaps": [],
        }

    monkeypatch.setattr(clients, "chat_json", fake_chat)
    res = core.plan("codice_fiscale", "Milano", "Brazil")
    assert [s["title"] for s in res["steps"]] == ["Go to Agenzia delle Entrate"]
    assert res["deadlines"][0]["sources"] == [2]
    assert "2 step(s) were removed" in res["gaps"][-1]
    assert "[2] Anagrafe - Comune di Milano" in seen["user"]
    assert "citizen of Brazil" in seen["user"]


def test_plan_without_sources_does_not_call_model(monkeypatch):
    monkeypatch.setattr(clients, "search", lambda q: [])
    monkeypatch.setattr(clients, "chat_json", lambda *a, **k: pytest.fail("model called"))
    assert core.plan("spid", "Roma", "EU")["gaps"]


def test_unknown_task():
    with pytest.raises(ValueError):
        core.plan("passport", "Roma", "EU")


LETTER = """AGENZIA DELLE ENTRATE - Comunicazione di irregolarità
Importo dovuto: euro 312,50. Il pagamento deve essere effettuato entro il 30/11/2026."""


def test_letter_keeps_only_dates_in_text(monkeypatch):
    monkeypatch.setattr(
        clients,
        "chat_json",
        lambda s, u, max_tokens=2000: {
            "summary": "You owe 312.50 euro.",
            "deadlines": [{"what": "Pay", "date": "2026-11-30"}, {"what": "Invented", "date": "2026-12-15"},
                          {"what": "Bad", "date": "2026-02-30"}],
        },
    )
    res = core.explain_letter(LETTER)
    assert [(d["what"], d["date"]) for d in res["deadlines"]] == [("Pay", "2026-11-30")]
    # The UI shows where the date is written, so the reader can check it.
    assert res["deadlines"][0]["match"] == "30/11/2026"
    assert "entro il 30/11/2026" in res["deadlines"][0]["quote"]
    assert res["deadlines"][0]["quote"].startswith("…") and "312,50." in res["deadlines"][0]["quote"]


def test_italian_month_names_count():
    assert "30 novembre 2026" in core._date_forms("2026-11-30")


def test_ics_escapes_and_alarms():
    body = core.ics([{"what": "Pay tax, INPS; now\nplease", "date": "2026-11-30"}])
    assert "DTSTART;VALUE=DATE:20261130" in body
    assert "SUMMARY:Pay tax\\, INPS\\; now please" in body
    assert "TRIGGER:-P3D" in body
    assert body.endswith("END:VCALENDAR\r\n")


def test_api_errors_and_calendar(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    c = TestClient(app)
    assert c.post("/api/plan", json={"task": "spid", "city": "Roma", "nationality": "EU"}).status_code == 503
    assert c.post("/api/plan", json={"task": "nope", "city": "Roma", "nationality": "EU"}).status_code == 400
    assert c.post("/api/letter").status_code == 400
    r = c.post("/api/calendar.ics", json={"events": [{"what": "x", "date": "2026-11-30"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert c.post("/api/calendar.ics", json={"events": [{"what": "x"}]}).status_code == 400


def test_api_letter_from_pasted_text(monkeypatch):
    monkeypatch.setattr(clients, "chat_json", lambda s, u, max_tokens=2000: {"summary": u[:20], "deadlines": []})
    r = TestClient(app).post("/api/letter", data={"text": LETTER})
    assert r.status_code == 200 and r.json()["summary"] == LETTER[:20]


def test_letter_date_must_not_match_inside_another_date(monkeypatch):
    monkeypatch.setattr(clients, "chat_json", lambda s, u, max_tokens=2000: {
        "deadlines": [{"what": "Invented", "date": "2026-11-05"}, {"what": "Real", "date": "2026-11-25"}]})
    res = core.explain_letter("Pagare entro il 25/11/2026 oppure il 15 novembre 2026.")
    assert [d["what"] for d in res["deadlines"]] == ["Real"]


def test_letter_language(monkeypatch):
    seen = {}
    monkeypatch.setattr(clients, "chat_json", lambda s, u, max_tokens=2000: seen.update(system=s) or {"deadlines": []})
    assert core.explain_letter(LETTER, "it")["lang"] == "it"
    assert "Write in simple, plain Italian" in seen["system"]
    core.explain_letter(LETTER)
    assert "Write in plain English" in seen["system"]
    with pytest.raises(ValueError):
        core.explain_letter(LETTER, "fr")
    r = TestClient(app).post("/api/letter", data={"text": LETTER, "lang": "fr"})
    assert r.status_code == 400


def test_plan_free_text_question(monkeypatch):
    seen = {}
    monkeypatch.setattr(clients, "search", lambda q: seen.update(query=q) or SOURCES)

    def fake_chat(system, user, max_tokens=2000):
        seen["user"] = user
        return {"summary": "s", "steps": [{"title": "t", "detail": "d", "where": "", "cost": "Free",
                                           "time": "Same day", "sources": [1]}]}

    monkeypatch.setattr(clients, "chat_json", fake_chat)
    c = TestClient(app)
    body = {"task": "other", "city": "Torino", "nationality": "EU", "question": "convert my  driving licence"}
    r = c.post("/api/plan", json=body)
    assert r.status_code == 200
    assert r.json()["steps"][0]["cost"] == "Free" and r.json()["steps"][0]["time"] == "Same day"
    assert seen["query"].startswith("convert my driving licence Torino")
    assert "Task: convert my driving licence" in seen["user"]
    assert c.post("/api/plan", json={**body, "question": " "}).status_code == 400


def test_config_reports_keys_without_values(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "secret-nebius")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    c = TestClient(app)
    r = c.get("/api/config")
    cfg = r.json()
    assert cfg["nebius_configured"] is True and cfg["tavily_configured"] is False
    assert "secret-nebius" not in r.text
    assert "agenziaentrate.gov.it" in cfg["official_domains"]
    assert {t["id"] for t in cfg["tasks"]} == set(core.TASKS)
    assert all(t["title"] and t["italian"] for t in cfg["tasks"])
    assert c.get("/api/health").json() == {"status": "ok"}


def _pdf_with_text(text: str) -> bytes:
    """A minimal one-page PDF with a text layer."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def test_api_letter_from_pdf_upload(monkeypatch):
    monkeypatch.setattr(clients, "chat_json", lambda s, u, max_tokens=2000: {
        "summary": "ok", "deadlines": [{"what": "Pay", "date": "2026-11-30"}]})
    pdf = _pdf_with_text("Pagamento entro il 30/11/2026")
    r = TestClient(app).post("/api/letter", files={"file": ("lettera.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["deadlines"][0]["match"] == "30/11/2026"


def test_sample_letter_and_demo_mode(monkeypatch):
    monkeypatch.setattr(clients, "search", demo.search)
    monkeypatch.setattr(clients, "chat_json", demo.chat_json)
    c = TestClient(app)
    sample = c.get("/api/sample-letter").json()
    assert sample["fictional"] is True
    # The canned answers go through the real checks: every deadline is found in the letter.
    for lang in ("en", "it"):
        res = c.post("/api/letter", data={"text": sample["text"], "lang": lang}).json()
        assert [d["date"] for d in res["deadlines"]] == ["2026-11-15", "2026-11-30"]
        assert [d["match"] for d in res["deadlines"]] == ["15 novembre 2026", "30/11/2026"]
    plan = c.post("/api/plan", json={"task": "residency", "city": "Milano", "nationality": "Brazil"}).json()
    assert len(plan["steps"]) == 5 and all(s["sources"] for s in plan["steps"])
    assert all(clients.is_official(s["url"]) for s in plan["sources"])
    # No example for this task: the honest "no official source" answer is shown.
    assert c.post("/api/plan", json={"task": "isee", "city": "Roma", "nationality": "EU"}).json()["gaps"]


def test_calendar_rejects_malformed_events():
    c = TestClient(app)
    assert c.post("/api/calendar.ics", json={"events": [{"what": 5, "date": "2026-11-30"}]}).status_code == 400
    assert c.post("/api/calendar.ics", json={"events": [{"what": "x", "date": "30/11/2026"}]}).status_code == 400


def test_public_demo_guard_caches_and_limits(monkeypatch):
    from service_desk import main

    monkeypatch.setattr(main, "guard", main.Guard())
    calls = []
    monkeypatch.setattr(clients, "chat_json", lambda s, u, max_tokens=2000: calls.append(u) or {"summary": "ok", "deadlines": []})
    c = TestClient(app)

    # Limits off by default: a local run is never throttled or cached.
    for _ in range(3):
        assert c.post("/api/letter", data={"text": LETTER}).status_code == 200
    assert len(calls) == 3

    monkeypatch.setenv("CACHE_SECONDS", "3600")
    monkeypatch.setenv("PER_IP_HOURLY_LIMIT", "2")
    assert c.post("/api/letter", data={"text": LETTER}).status_code == 200
    assert c.post("/api/letter", data={"text": LETTER}).status_code == 200  # same letter: from memory
    assert len(calls) == 4
    assert c.post("/api/letter", data={"text": LETTER + " bis"}).status_code == 200
    r = c.post("/api/letter", data={"text": LETTER + " ter"})
    assert r.status_code == 429 and "per hour" in r.json()["detail"]
    other = {"X-Forwarded-For": "203.0.113.9"}
    assert c.post("/api/letter", data={"text": LETTER + " ter"}, headers=other).status_code == 200

    monkeypatch.setenv("DAILY_LIMIT", "1")
    r = c.post("/api/letter", data={"text": LETTER + " quater"}, headers={"X-Forwarded-For": "198.51.100.7"})
    assert r.status_code == 429 and "today's quota" in r.json()["detail"]
    assert c.post("/api/letter", data={"text": LETTER}).status_code == 200  # cached answers still work

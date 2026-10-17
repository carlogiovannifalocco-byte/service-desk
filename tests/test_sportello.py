import pytest
from fastapi.testclient import TestClient

from service_desk import clients, core
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
    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": SOURCES + [{"title": "blog", "url": "https://blog.example.com", "content": "x"}]}

    sent = {}
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    monkeypatch.setattr(clients.httpx, "post", lambda url, json, timeout: sent.update(json) or R())
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
    assert res["deadlines"] == [{"what": "Pay", "date": "2026-11-30"}]


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

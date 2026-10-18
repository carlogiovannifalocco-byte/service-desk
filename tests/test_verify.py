"""The verification report: quotes checked word for word against the cited official page, page update dates,
Tavily Extract request shape, and the plan in Italian."""

from datetime import date

import httpx
import pytest

from service_desk import clients, core, verify

TODAY = date(2026, 10, 18)

PAGE = """Codice fiscale - Agenzia delle Entrate
Il codice fiscale viene rilasciato gratuitamente dagli uffici dell’Agenzia delle Entrate, presentando il modello AA4/8
e un documento di riconoscimento valido.
Per i cittadini stranieri è necessario il passaporto.
Ultimo aggiornamento: 12/03/2025"""

SOURCES = [
    {"title": "Codice fiscale", "url": "https://www.agenziaentrate.gov.it/portale/cf",
     "content": "Il codice fiscale viene rilasciato gratuitamente dagli uffici", "retrieved_at": "2026-10-18T08:00:00Z"},
    {"title": "Residenza - Comune di Milano", "url": "https://www.comune.milano.it/residenza",
     "content": "Dichiara la residenza entro 20 giorni.", "retrieved_at": "2026-10-18T08:00:00Z"},
]


def test_quote_must_appear_verbatim():
    assert verify.quote_in("rilasciato gratuitamente dagli uffici dell'Agenzia", PAGE)  # curly vs straight apostrophe
    assert verify.quote_in("Il codice fiscale viene rilasciato ... il modello AA4/8", PAGE)  # ellipsis, in order
    assert not verify.quote_in("il modello AA4/8 ... Il codice fiscale viene rilasciato", PAGE)  # wrong order
    assert not verify.quote_in("rilasciato a pagamento dagli uffici", PAGE)  # reworded
    assert not verify.quote_in("passaporto", PAGE)  # too short to prove anything
    assert not verify.quote_in("", PAGE)


def test_page_update_date_and_staleness():
    found = verify.page_updated(PAGE, TODAY)
    assert found == {"date": "2025-03-12", "text": "Ultimo aggiornamento: 12/03/2025", "stale": False}
    assert verify.page_updated("Pagina aggiornata il 5 maggio 2025", TODAY)["stale"] is False
    assert verify.page_updated("Ultimo aggiornamento: 12/03/2024", TODAY)["stale"] is True  # over two years old
    old = verify.page_updated("Data di pubblicazione: 01.02.2021\nUltima modifica 2022-06-30", TODAY)
    assert old["date"] == "2022-06-30" and old["stale"] is True  # the latest stated date wins
    assert verify.page_updated("Ultimo aggiornamento: 31/02/2024", TODAY) is None  # not a real date
    assert verify.page_updated("Ultimo aggiornamento: 01/01/2030", TODAY) is None  # in the future
    assert verify.page_updated("Nessuna data qui.", TODAY) is None


def test_plan_carries_a_verification_report(monkeypatch):
    monkeypatch.setattr(clients, "search", lambda q: SOURCES)
    monkeypatch.setattr(clients, "extract", lambda urls: {SOURCES[0]["url"] + "/": PAGE})  # trailing slash still matches
    monkeypatch.setattr(clients, "chat_json", lambda system, user, max_tokens=None: {
        "summary": "s",
        "steps": [
            {"title": "Go to the office", "sources": [1], "quote": "presentando il modello AA4/8"},  # only on the full page
            {"title": "Invented fee", "sources": [1], "quote": "costa 50 euro"},  # not on the page: quote dropped
            {"title": "Declare residency", "sources": [2], "quote": "Dichiara la residenza entro 20 giorni."},
            {"title": "Wrong source", "sources": [2], "quote": "presentando il modello AA4/8"},  # quote is in [1], not [2]
            {"title": "Uncited", "sources": []},
        ],
        "documents": [{"name": "Passport", "sources": [1], "quote": "Per i cittadini stranieri è necessario il passaporto"}],
        "deadlines": [], "gaps": []})
    p = core.plan("codice_fiscale", "Milano", "Brazil")
    steps = {s["title"]: s for s in p["steps"]}
    assert steps["Go to the office"]["verified"] and steps["Go to the office"]["quote_source"] == 1
    assert steps["Invented fee"]["verified"] is False and steps["Invented fee"]["quote"] == ""
    assert steps["Declare residency"]["verified"] and steps["Wrong source"]["verified"] is False
    assert "Uncited" not in steps
    assert p["documents"][0]["verified"]
    assert p["sources"][0]["page_read"] and p["sources"][0]["updated"]["date"] == "2025-03-12"
    assert p["sources"][0]["retrieved_at"] == "2026-10-18T08:00:00Z"
    assert p["sources"][1]["page_read"] is False and p["sources"][1]["updated"] is None
    v = p["verification"]
    assert (v["claims"], v["quoted"], v["quote_coverage"], v["removed_uncited"]) == (5, 3, 0.6, 1)
    assert (v["pages_read"], v["pages_total"], v["pages_dated"], v["page_check"]) == (1, 2, 1, "ok")


def test_plan_still_checks_excerpts_when_extract_fails(monkeypatch):
    monkeypatch.setattr(clients, "search", lambda q: SOURCES)

    def broken(urls):
        raise clients.ServiceError("Tavily plan or key credit limit reached (432)")

    monkeypatch.setattr(clients, "extract", broken)
    monkeypatch.setattr(clients, "chat_json", lambda *a, **k: {
        "summary": "", "steps": [{"title": "t", "sources": [1], "quote": "Il codice fiscale viene rilasciato gratuitamente"}],
        "documents": [], "deadlines": [], "gaps": []})
    p = core.plan("codice_fiscale", "Milano", "EU")
    assert p["steps"][0]["verified"] is True
    assert p["verification"]["page_check"].startswith("unavailable: Tavily plan or key credit limit")


def test_plan_in_italian(monkeypatch):
    monkeypatch.setattr(clients, "search", lambda q: SOURCES)
    monkeypatch.setattr(clients, "extract", lambda urls: {})
    seen = {}

    def chat(system, user, max_tokens=None):
        seen["system"] = system
        return {"summary": "", "steps": [{"title": "x", "sources": []}], "documents": [], "deadlines": [], "gaps": []}

    monkeypatch.setattr(clients, "chat_json", chat)
    p = core.plan("residency", "Milano", "EU", lang="it")
    assert "Write a practical plan in simple, plain Italian." in seen["system"]
    assert p["lang"] == "it" and p["gaps"] == ["1 passaggi sono stati tolti perché nessuna fonte ufficiale li confermava."]
    with pytest.raises(ValueError):
        core.plan("residency", "Milano", "EU", lang="fr")


def test_new_tasks_are_offered():
    assert {"driving_licence", "lease_registration", "partita_iva"} <= set(core.TASKS) <= set(core.TASK_DETAILS)
    assert clients.is_official("https://www.ilportaledellautomobilista.it/web/portale-automobilista/patenti")


def test_tavily_extract_request_shape(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json})
        return httpx.Response(200, json={
            "results": [{"url": SOURCES[0]["url"], "raw_content": PAGE},
                        {"url": "https://evil.example.com/", "raw_content": "x"}],
            "failed_results": [{"url": SOURCES[1]["url"], "error": "timeout"}], "request_id": "r"})

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(clients.httpx, "post", fake_post)
    urls = [SOURCES[0]["url"], SOURCES[1]["url"], SOURCES[0]["url"], "https://blog.example.com/x"]
    assert clients.extract(urls) == {SOURCES[0]["url"]: PAGE}
    call = calls[0]
    assert call["url"] == "https://api.tavily.com/extract"
    assert call["headers"] == {"Authorization": "Bearer tvly-test"}
    assert call["json"] == {"urls": [SOURCES[0]["url"], SOURCES[1]["url"]], "extract_depth": "basic", "format": "text"}


def test_tavily_extract_errors_are_readable(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setattr(clients.httpx, "post", lambda *a, **k: httpx.Response(432, json={"detail": {"error": "limit"}}))
    with pytest.raises(clients.ServiceError, match="credit limit"):
        clients.extract([SOURCES[0]["url"]])
    monkeypatch.delenv("TAVILY_API_KEY")
    with pytest.raises(clients.ServiceError, match="TAVILY_API_KEY"):
        clients.extract([SOURCES[0]["url"]])


def test_api_plan_accepts_lang(monkeypatch):
    from fastapi.testclient import TestClient

    from service_desk.main import app

    monkeypatch.setattr(clients, "search", lambda q: [])
    r = TestClient(app).post("/api/plan", json={"task": "spid", "city": "Roma", "nationality": "EU", "lang": "it"})
    assert r.status_code == 200 and r.json()["gaps"][0].startswith("Non è stata trovata")
    r = TestClient(app).post("/api/plan", json={"task": "spid", "city": "Roma", "nationality": "EU", "lang": "de"})
    assert r.status_code == 400


def test_eval_script_runs_offline(tmp_path):
    import importlib.util
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "eval_citations.py"
    spec = importlib.util.spec_from_file_location("eval_citations", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    saved = (clients.search, clients.extract, clients.chat_json)
    try:
        assert mod.main(["--demo", "--limit", "2", "--out", str(tmp_path / "r.json")]) == 0
    finally:
        clients.search, clients.extract, clients.chat_json = saved
    result = json.loads((tmp_path / "r.json").read_text())
    assert result["mode"] == "demo (canned data)" and result["summary"]["requests"] == 2
    assert result["summary"]["claims"] == 18 and result["summary"]["pages_read_rate"] == 0 and result["summary"]["all_sources_official"] is True

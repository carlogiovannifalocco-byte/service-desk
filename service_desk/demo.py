"""Demo mode: canned search results and model answers, for screenshots and offline demos.

Enabled with SERVICE_DESK_DEMO=1. Nothing here calls Tavily or Nebius. The UI shows an
"Example data" banner whenever demo mode is on, so the output is never mistaken for a live answer.
The canned answers still go through the real code paths: the citation filter and the
deadline check run on them exactly as they would on a model's reply.
"""

from __future__ import annotations

from . import clients, core

# A fictional letter, used by the "Try a sample letter" button in every mode.
SAMPLE_LETTER = """AGENZIA DELLE ENTRATE
Direzione Provinciale di Milano - Ufficio Territoriale

Prot. n. ESEMPIO-2026-000123                              Milano, 12 ottobre 2026

COMUNICAZIONE DI IRREGOLARITA'
ai sensi dell'art. 36-bis del D.P.R. n. 600/1973 - Dichiarazione dei redditi 2025

Gentile contribuente,
dal controllo automatizzato della Sua dichiarazione dei redditi presentata per l'anno d'imposta 2024
e' emerso un minor versamento dell'imposta dovuta.

Importo dovuto (imposta, sanzione ridotta e interessi): euro 312,50

Se ritiene che la comunicazione non sia corretta, puo' fornire chiarimenti o documenti entro il
15 novembre 2026 tramite il servizio CIVIS, in ufficio o tramite PEC.

Se riconosce l'importo, il pagamento deve essere effettuato con il modello F24 allegato entro il 30/11/2026.
In tal caso la sanzione e' ridotta a un terzo. In assenza di pagamento, le somme saranno iscritte a ruolo.

Cordiali saluti
Il Direttore dell'Ufficio
"""

_CF_SOURCES = [
    {"title": "Codice fiscale e tessera sanitaria - Agenzia delle Entrate",
     "url": "https://www.agenziaentrate.gov.it/portale/web/english",
     "content": "Foreign citizens can request the codice fiscale at any office of the Agenzia delle Entrate "
                "with form AA4/8 and a valid identity document. The service is free."},
    {"title": "Prenota un appuntamento - Agenzia delle Entrate",
     "url": "https://www.agenziaentrate.gov.it/portale/web/guest/contatta",
     "content": "Appointments at the offices can be booked online, by phone or through the app."},
    {"title": "Tessera sanitaria - Ministero della Salute",
     "url": "https://www.salute.gov.it/",
     "content": "The tessera sanitaria, which also shows the codice fiscale, is sent after registration with the SSN."},
]

_RES_SOURCES = [
    {"title": "Cambio di residenza online - ANPR",
     "url": "https://www.anagrafenazionale.interno.it/",
     "content": "The residency declaration can be submitted online with SPID or CIE, or at the comune's anagrafe."},
    {"title": "Iscrizione anagrafica e cambio di residenza - Comune di Milano",
     "url": "https://www.comune.milano.it/",
     "content": "Declare residency within 20 days of moving in. The municipal police check that you live at "
                "the address. If the comune does not reply within 45 days, the registration is confirmed. "
                "Non-EU citizens attach their passport and residence permit (or the renewal receipt)."},
    {"title": "Carta d'identita' elettronica - Ministero dell'Interno",
     "url": "https://www.cartaidentita.interno.gov.it/",
     "content": "Once resident, book the CIE at your comune. Fee: about 22 euro, set by each comune."},
]

_PLANS = {
    "tax code": {
        "summary": "You can get your codice fiscale for free at any Agenzia delle Entrate office: book an "
                   "appointment, bring your passport and form AA4/8, and you leave with the number on paper.",
        "steps": [
            {"title": "Fill in form AA4/8", "detail": "It is the request form for a codice fiscale. Print it and fill it "
             "in before the appointment to save time.", "where": "Online, then print", "cost": "Free", "time": "",
             "sources": [1], "quote": "with form AA4/8 and a valid identity document"},
            {"title": "Book an appointment", "detail": "Book a slot at the office closest to you, online, by phone or "
             "in the app.", "where": "Agenzia delle Entrate website", "cost": "", "time": "", "sources": [2],
             "quote": "Appointments at the offices can be booked online, by phone or through the app."},
            {"title": "Go to the office with your documents", "detail": "Bring your passport and, as a non-EU "
             "citizen, your visa or residence permit. The officer prints a certificate with your codice fiscale.",
             "where": "Any Agenzia delle Entrate office", "cost": "Free", "time": "Same day", "sources": [1],
             "quote": "request the codice fiscale at any office of the Agenzia delle Entrate"},
            {"title": "Expect the tessera sanitaria later", "detail": "The plastic card that shows your codice fiscale "
             "arrives after you register with the national health service.", "where": "By post", "cost": "",
             "time": "", "sources": [3], "quote": "is sent after registration with the SSN"},
        ],
        "documents": [{"name": "Valid passport", "sources": [1], "quote": "a valid identity document"},
                      {"name": "Form AA4/8, filled in", "sources": [1], "quote": "with form AA4/8"},
                      # Not stated in the excerpt: shown as cited but not quoted, as a live answer would be.
                      {"name": "Visa or residence permit (non-EU citizens)", "sources": [1], "quote": ""}],
        "deadlines": [],
        "gaps": ["How long the wait for an appointment in Milano is today is not stated in the sources."],
    },
    "residency": {
        "summary": "Declare your new address to the Comune di Milano within 20 days of moving in, online or at the "
                   "anagrafe. The police check the address, and after 45 days without a reply you are registered.",
        "steps": [
            {"title": "Gather your documents", "detail": "Passport, residence permit or renewal receipt, codice fiscale, "
             "and your rental contract or the owner's consent.", "where": "", "cost": "", "time": "",
             "sources": [2], "quote": "Non-EU citizens attach their passport and residence permit (or the renewal receipt)."},
            {"title": "Submit the residency declaration", "detail": "Online on ANPR with SPID or CIE, or in person at "
             "the anagrafe of the Comune di Milano.", "where": "ANPR or Comune di Milano, Anagrafe", "cost": "Free",
             "time": "Within 20 days of moving in", "sources": [1, 2],
             "quote": "The residency declaration can be submitted online with SPID or CIE, or at the comune's anagrafe."},
            {"title": "Be at home for the police check", "detail": "The municipal police visit to confirm that you "
             "live at the address. Put your name on the doorbell and the mailbox.", "where": "Your home",
             "cost": "", "time": "Within 45 days", "sources": [2],
             "quote": "The municipal police check that you live at the address."},
            {"title": "Registration confirmed", "detail": "If the comune does not reply within 45 days, your "
             "registration counts as accepted.", "where": "", "cost": "", "time": "45 days", "sources": [2],
             "quote": "If the comune does not reply within 45 days, the registration is confirmed."},
            {"title": "Book your electronic ID card", "detail": "Once resident you can book the CIE at the comune.",
             "where": "Comune di Milano", "cost": "About €22", "time": "", "sources": [3],
             "quote": "Once resident, book the CIE at your comune."},
        ],
        "documents": [{"name": "Valid passport", "sources": [2], "quote": "attach their passport"},
                      {"name": "Residence permit, or the receipt of its renewal", "sources": [2],
                       "quote": "residence permit (or the renewal receipt)"},
                      {"name": "Rental contract or owner's consent declaration", "sources": [2], "quote": ""},
                      {"name": "SPID or CIE, to apply online", "sources": [1], "quote": "submitted online with SPID or CIE"}],
        "deadlines": [{"what": "Declare your residency", "rule": "Within 20 days of moving in", "sources": [2],
                       "quote": "Declare residency within 20 days of moving in."},
                      {"what": "Comune's checks completed", "rule": "45 days after you apply; silence means yes",
                       "sources": [2], "quote": "If the comune does not reply within 45 days"}],
        "gaps": ["The sources do not say how long the wait for an appointment at the Milano anagrafe is."],
    },
}

_LETTER = {
    "en": {
        "sender": "Agenzia delle Entrate, Milano",
        "kind": "Comunicazione di irregolarità (notice of a tax discrepancy)",
        "summary": "The tax office checked your 2025 tax return automatically and found that you paid less tax than "
                   "you owed for 2024. They ask for €312.50, which already includes a reduced penalty and interest. "
                   "If you think it is wrong you can explain by 15 November 2026. If you agree, pay with the attached "
                   "F24 form by 30 November 2026 to keep the reduced penalty.",
        "actions": ["Check the figures against your 2025 tax return, or ask your accountant or CAF to check them.",
                    "If something is wrong, send your explanation and documents through CIVIS, at the office or by PEC.",
                    "If it is right, pay €312.50 with the attached F24 form at your bank, post office or home banking.",
                    "Keep the payment receipt together with this letter."],
        "deadlines": [{"what": "Send clarifications if you disagree", "date": "2026-11-15"},
                      {"what": "Pay €312.50 with the F24 form", "date": "2026-11-30"}],
        "amounts": [{"what": "Tax, reduced penalty and interest", "euro": 312.5}],
        "urgency": "high",
        "words_to_know": [{"italian": "comunicazione di irregolarità", "english": "notice that your tax return "
                           "does not match what you paid"},
                          {"italian": "sanzione ridotta", "english": "reduced penalty (one third, if you pay in time)"},
                          {"italian": "modello F24", "english": "the standard form to pay taxes"},
                          {"italian": "iscritte a ruolo", "english": "sent to collection, with extra costs"}],
    },
    "it": {
        "sender": "Agenzia delle Entrate, Milano",
        "kind": "Comunicazione di irregolarità",
        "summary": "L'Agenzia ha controllato in automatico la dichiarazione dei redditi 2025 e ha trovato che hai "
                   "pagato meno imposte del dovuto per il 2024. Chiede 312,50 euro, già con sanzione ridotta e "
                   "interessi. Se non sei d'accordo puoi spiegare entro il 15 novembre 2026. Se sei d'accordo, paga "
                   "con l'F24 allegato entro il 30 novembre 2026 per mantenere la sanzione ridotta.",
        "actions": ["Confronta gli importi con la tua dichiarazione, oppure chiedi al commercialista o al CAF.",
                    "Se qualcosa non torna, invia chiarimenti e documenti tramite CIVIS, in ufficio o via PEC.",
                    "Se è corretto, paga 312,50 euro con l'F24 allegato in banca, alla posta o con l'home banking.",
                    "Conserva la ricevuta di pagamento insieme alla lettera."],
        "deadlines": [{"what": "Inviare chiarimenti se non sei d'accordo", "date": "2026-11-15"},
                      {"what": "Pagare 312,50 euro con l'F24", "date": "2026-11-30"}],
        "amounts": [{"what": "Imposta, sanzione ridotta e interessi", "euro": 312.5}],
        "urgency": "high",
        "words_to_know": [{"italian": "comunicazione di irregolarità", "english": "notice of a tax discrepancy"},
                          {"italian": "sanzione ridotta", "english": "reduced penalty"},
                          {"italian": "modello F24", "english": "tax payment form"},
                          {"italian": "iscritte a ruolo", "english": "sent to collection"}],
    },
}


def search(query: str, max_results: int = 6) -> list[dict]:
    q = query.lower()
    if "tax code" in q or "codice fiscale" in q:
        found = _CF_SOURCES
    elif "residency" in q or "residenza" in q:
        found = _RES_SOURCES
    else:
        found = []  # no example for other tasks, so the "no official source" path is shown
    from .verify import now_iso

    return [{**s, "retrieved_at": now_iso()} for s in found if clients.is_official(s["url"])][:max_results]


def chat_json(system: str, user: str | list, max_tokens: int = 2000) -> dict:
    text = user if isinstance(user, str) else str(user)
    if "SOURCES:" in text:
        task_line = text.split("\n", 1)[0].lower()
        key = "tax code" if "tax code" in task_line or "codice fiscale" in task_line else "residency"
        return _PLANS[key]
    lang = "it" if f"Write in {core.LANGUAGES['it']}" in system else "en"
    if "COMUNICAZIONE DI IRREGOLARITA" in text:
        return dict(_LETTER[lang])
    return {"sender": "", "kind": "", "summary": "Demo mode only explains the sample letter. Run Service Desk with "
            "NEBIUS_API_KEY set to explain your own letters.", "actions": [], "deadlines": [], "amounts": [],
            "urgency": "", "words_to_know": []}


def extract(urls: list[str]) -> dict[str, str]:
    """Demo mode reads no pages: quotes are checked against the canned excerpts only, and no page date is shown."""
    return {}


def install() -> None:
    clients.search = search
    clients.extract = extract
    clients.chat_json = chat_json

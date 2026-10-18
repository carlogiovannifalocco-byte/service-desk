"""Service Desk HTTP API and web UI. Nothing the user uploads is stored."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import clients, core, demo
from .clients import ServiceError

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 5 * 1024 * 1024
DEMO = os.getenv("SERVICE_DESK_DEMO", "").lower() in ("1", "true", "yes")

if DEMO:
    demo.install()

app = FastAPI(title="Service Desk")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    """What the UI needs to know about this instance. Reports whether keys are set, never their values."""
    return {
        "demo": DEMO,
        "nebius_configured": bool(os.getenv("NEBIUS_API_KEY")),
        "tavily_configured": bool(os.getenv("TAVILY_API_KEY")),
        "model": os.getenv("NEBIUS_MODEL", clients.DEFAULT_MODEL),
        "official_domains": clients.OFFICIAL_DOMAINS,
        "tasks": [{"id": k, "description": v, **core.TASK_DETAILS.get(k, {})} for k, v in core.TASKS.items()],
        "languages": list(core.LANGUAGES),
    }


@app.get("/api/tasks")
def tasks():
    return core.TASKS


@app.get("/api/sample-letter")
def sample_letter():
    """A fictional letter for trying the letter explainer."""
    return {"text": demo.SAMPLE_LETTER, "fictional": True}


class PlanRequest(BaseModel):
    task: str
    city: str = Field(min_length=2, max_length=60)
    nationality: str = Field(min_length=2, max_length=60)
    situation: str = Field(default="", max_length=1000)
    question: str = Field(default="", max_length=300)  # used when task is "other"
    lang: str = "en"  # "en" or "it"


@app.post("/api/plan")
def make_plan(req: PlanRequest):
    try:
        return core.plan(req.task, req.city, req.nationality, req.situation, req.question, req.lang)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except ServiceError as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/letter")
async def letter(file: UploadFile | None = None, text: str | None = Form(None), lang: str = Form("en")):
    try:
        if file is not None:
            data = await file.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "file larger than 5 MB")
            content = core.letter_text(file.filename or "letter.txt", data)
        elif text and text.strip():
            content = text.strip()[: core.MAX_LETTER_CHARS]
        else:
            raise HTTPException(400, "send a file or text")
        return core.explain_letter(content, lang)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except ServiceError as exc:
        raise HTTPException(503, str(exc))


class CalendarRequest(BaseModel):
    events: list[dict]


@app.post("/api/calendar.ics")
def calendar(req: CalendarRequest):
    try:
        body = core.ics(req.events)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, f"each event needs 'what' and an ISO 'date': {exc}")
    return Response(body, media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=service-desk.ics"})

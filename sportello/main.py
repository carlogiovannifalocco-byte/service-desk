"""Sportello HTTP API and web UI. Nothing the user uploads is stored."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import core
from .clients import ServiceError

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 5 * 1024 * 1024

app = FastAPI(title="Sportello")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/tasks")
def tasks():
    return core.TASKS


class PlanRequest(BaseModel):
    task: str
    city: str = Field(min_length=2, max_length=60)
    nationality: str = Field(min_length=2, max_length=60)
    situation: str = Field(default="", max_length=1000)


@app.post("/api/plan")
def make_plan(req: PlanRequest):
    try:
        return core.plan(req.task, req.city, req.nationality, req.situation)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except ServiceError as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/letter")
async def letter(file: UploadFile | None = None, text: str | None = Form(None)):
    try:
        if file is not None:
            data = await file.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                raise HTTPException(413, "file larger than 5 MB")
            content = core.letter_text(file.filename or "letter.txt", data)
        elif text:
            content = text.strip()[: core.MAX_LETTER_CHARS]
        else:
            raise HTTPException(400, "send a file or text")
        return core.explain_letter(content)
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
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, f"each event needs 'what' and an ISO 'date': {exc}")
    return Response(body, media_type="text/calendar", headers={"Content-Disposition": "attachment; filename=sportello.ics"})

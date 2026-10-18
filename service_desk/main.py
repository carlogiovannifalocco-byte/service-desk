"""Service Desk HTTP API and web UI. Nothing the user uploads is stored."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
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


class Guard:
    """Keeps a public demo inside its API quota. Identical requests are answered from memory for a few hours
    (official pages do not change by the minute), and fresh ones are capped per visitor per hour and per day in
    total. Limits come from the environment and default to off, so a local run behaves exactly as before."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float, dict]] = {}
        self.per_ip: dict[str, deque] = defaultdict(deque)
        self.day, self.today_count = "", 0

    @staticmethod
    def _limit(name: str) -> int:
        try:
            return max(0, int(os.getenv(name, "0")))
        except ValueError:
            return 0

    def key(self, *parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def cached(self, key: str) -> dict | None:
        ttl = self._limit("CACHE_SECONDS")
        with self.lock:
            hit = self.cache.get(key)
            return hit[1] if hit and ttl and time.time() - hit[0] < ttl else None

    def store(self, key: str, result: dict) -> None:
        if self._limit("CACHE_SECONDS"):
            with self.lock:
                if len(self.cache) > 500:
                    self.cache.clear()
                self.cache[key] = (time.time(), result)

    def admit(self, ip: str) -> None:
        """Count one fresh request, or raise 429 with a sentence the UI can show as is."""
        hourly, daily = self._limit("PER_IP_HOURLY_LIMIT"), self._limit("DAILY_LIMIT")
        now = time.time()
        with self.lock:
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            if day != self.day:
                self.day, self.today_count = day, 0
            if daily and self.today_count >= daily:
                raise HTTPException(429, "this public demo has used today's quota; try again tomorrow or run it with your own keys")
            if hourly:
                seen = self.per_ip[ip]
                while seen and now - seen[0] > 3600:
                    seen.popleft()
                if len(seen) >= hourly:
                    raise HTTPException(429, f"this public demo allows {hourly} new requests per hour per visitor; try again later")
                seen.append(now)
            self.today_count += 1


guard = Guard()


def client_ip(request: Request) -> str:
    # Behind Render's proxy the visitor is the first X-Forwarded-For entry.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "")


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
def make_plan(req: PlanRequest, request: Request):
    key = guard.key("plan", req.model_dump())
    if (hit := guard.cached(key)) is not None:
        return hit
    guard.admit(client_ip(request))
    try:
        result = core.plan(req.task, req.city, req.nationality, req.situation, req.question, req.lang)
        guard.store(key, result)
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except ServiceError as exc:
        raise HTTPException(503, str(exc))


@app.post("/api/letter")
async def letter(request: Request, file: UploadFile | None = None, text: str | None = Form(None),
                 lang: str = Form("en")):
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
        key = guard.key("letter", content, lang)
        if (hit := guard.cached(key)) is not None:
            return hit
        guard.admit(client_ip(request))
        result = core.explain_letter(content, lang)
        guard.store(key, result)
        return result
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

"""Thin clients for the two external services: Nebius Token Factory (NVIDIA open models) and Tavily search."""

from __future__ import annotations

import json
import os
import re

import httpx

# Official sources only. Answers may cite nothing else.
OFFICIAL_DOMAINS = [
    "gov.it",
    "agenziaentrate.gov.it",
    "inps.it",
    "interno.gov.it",
    "poliziadistato.it",
    "portaleimmigrazione.it",
    "salute.gov.it",
    "esteri.it",
    "anagrafenazionale.interno.it",
    "spid.gov.it",
    "comune.milano.it",
    "comune.roma.it",
    "comune.torino.it",
    "comune.napoli.it",
    "comune.bologna.it",
    "comune.fi.it",
]


class ServiceError(RuntimeError):
    pass


def is_official(url: str) -> bool:
    host = re.sub(r"^https?://", "", url).split("/", 1)[0].lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS) or bool(
        re.fullmatch(r"(www\.)?comune\.[a-z\-]+\.[a-z]{2}\.it|(www\.)?comune\.[a-z\-]+\.it", host)
    )


def search(query: str, max_results: int = 6) -> list[dict]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise ServiceError("TAVILY_API_KEY is not set")
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "search_depth": "advanced",
                "include_domains": OFFICIAL_DOMAINS,
                "max_results": max_results,
            },
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ServiceError(f"search failed: {exc}") from exc
    results = resp.json().get("results", [])
    # include_domains is a filter on Tavily's side; we check again so nothing unofficial slips through.
    return [
        {"title": r.get("title", ""), "url": r["url"], "content": r.get("content", "")[:1500]}
        for r in results
        if r.get("url") and is_official(r["url"])
    ]


def chat_json(system: str, user: str | list, max_tokens: int = 2000) -> dict:
    key = os.getenv("NEBIUS_API_KEY")
    if not key:
        raise ServiceError("NEBIUS_API_KEY is not set")
    base = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1").rstrip("/")
    model = os.getenv("NEBIUS_MODEL", "nvidia/Llama-3_3-Nemotron-Super-49B-v1_5")
    try:
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0.1,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
            timeout=120,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ServiceError(f"model call failed: {exc}") from exc
    content = resp.json()["choices"][0]["message"]["content"]
    # Reasoning models may prepend a <think> block; keep only the JSON object.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise ServiceError("model did not return JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ServiceError("model returned invalid JSON") from exc

"""Thin clients for the two external services: Nebius Token Factory (NVIDIA open models) and Tavily search.

Request shapes follow the official docs (checked October 2026):
- Token Factory chat completions (OpenAI-compatible):
  https://docs.tokenfactory.nebius.com/api-reference/inference/create-chat-completion
- Token Factory JSON mode: https://docs.tokenfactory.nebius.com/ai-models-inference/json
- Token Factory rate limits (429 + Retry-After): https://docs.tokenfactory.nebius.com/ai-models-inference/rate-limits
- Tavily search: https://docs.tavily.com/documentation/api-reference/endpoint/search
- Tavily extract: https://docs.tavily.com/documentation/api-reference/endpoint/extract
"""

from __future__ import annotations

import json
import os
import re
import time

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
    "ilportaledellautomobilista.it",
    "comune.milano.it",
    "comune.roma.it",
    "comune.torino.it",
    "comune.napoli.it",
    "comune.bologna.it",
    "comune.fi.it",
]

# NVIDIA Nemotron 3 Super on Token Factory. The id is the one in Nebius's own Nemotron code sample
# (https://nebius.com/services/token-factory/nemotron) and the replacement Nebius recommends for the
# Nemotron models it retired in August 2026 (https://docs.tokenfactory.nebius.com/august-2026-deprecation-notice).
DEFAULT_MODEL = "nvidia/nemotron-3-super-120b-a12b"
DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
MAX_PAGE_CHARS = 60000  # enough for any PA page body; only used for checks, never sent to the model
TAVILY_MAX_QUERY = 400  # Tavily rejects longer queries ("Max query length is 400 characters").

RETRY_STATUS = {429, 500, 502, 503, 504, 529}  # 529 = Token Factory "inference service overloaded"
ATTEMPTS = 3
_sleep = time.sleep  # replaced in tests

# Request options a given model rejected with 400/422; not sent again for that model.
_unsupported: set[tuple[str, str]] = set()


class ServiceError(RuntimeError):
    pass


def is_official(url: str) -> bool:
    host = re.sub(r"^https?://", "", url).split("/", 1)[0].lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS) or bool(
        re.fullmatch(r"(www\.)?comune\.[a-z\-]+\.[a-z]{2}\.it|(www\.)?comune\.[a-z\-]+\.it", host)
    )


def _error_detail(resp: httpx.Response) -> str:
    """The error message from a Token Factory or Tavily error body, without echoing anything secret."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200].strip()
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:300]
        if isinstance(err, str):
            return err[:300]
        detail = body.get("detail")
        if isinstance(detail, list):  # 422 validation errors: [{"loc", "msg", ...}]
            return "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)[:300]
        if isinstance(detail, dict):
            return str(detail.get("error") or detail.get("message") or detail)[:300]
        if detail:
            return str(detail)[:300]
    return str(body)[:200]


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("retry-after", "")), 10.0)
    except ValueError:
        return 1.5 * 2 ** attempt


def _post(service: str, url: str, *, headers: dict, payload: dict, timeout: httpx.Timeout) -> httpx.Response:
    """POST with retries on rate limits, overload and dropped connections. Returns the last response."""
    for attempt in range(ATTEMPTS):
        try:
            resp = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        except httpx.TimeoutException as exc:
            # Not retried: a second full wait would leave the person staring at a spinner.
            raise ServiceError(f"{service} did not answer in time ({type(exc).__name__}); try again") from exc
        except httpx.TransportError as exc:
            if attempt + 1 < ATTEMPTS:
                _sleep(1.5 * 2 ** attempt)
                continue
            raise ServiceError(f"could not reach {service}: {exc}") from exc
        if resp.status_code in RETRY_STATUS and attempt + 1 < ATTEMPTS:
            _sleep(_retry_after(resp, attempt))
            continue
        return resp
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- Tavily

TAVILY_ERRORS = {
    401: "Tavily rejected the API key (401): check TAVILY_API_KEY",
    429: "Tavily rate limit reached (429): wait a minute and try again",
    432: "Tavily plan or key credit limit reached (432)",
    433: "Tavily pay-as-you-go limit reached (433)",
}


def search(query: str, max_results: int = 6) -> list[dict]:
    """Official pages for the query. Raises ServiceError with a readable message when Tavily fails."""
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise ServiceError("TAVILY_API_KEY is not set")
    payload = {
        "query": " ".join(query.split())[:TAVILY_MAX_QUERY],
        "search_depth": "advanced",  # 2 credits; returns the most relevant chunks of each page
        "topic": "general",
        "country": "italy",  # a boost, not a filter; only valid with topic "general"
        "include_domains": OFFICIAL_DOMAINS,
        "max_results": max_results,
    }
    resp = _post(
        "Tavily",
        TAVILY_URL,
        headers={"Authorization": f"Bearer {key}"},  # the documented auth; the old body "api_key" is not needed
        payload=payload,
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    if resp.status_code != 200:
        msg = TAVILY_ERRORS.get(resp.status_code, f"Tavily search failed ({resp.status_code})")
        raise ServiceError(f"{msg}: {_error_detail(resp)}")
    try:
        results = resp.json().get("results") or []
    except (ValueError, AttributeError) as exc:
        raise ServiceError("Tavily returned an unreadable response") from exc
    out, seen = [], set()
    for r in results:
        if not isinstance(r, dict):
            continue
        url = r.get("url") or ""
        # include_domains is a filter on Tavily's side; we check again so nothing unofficial slips through.
        if not url or url in seen or not is_official(url):
            continue
        seen.add(url)
        out.append({"title": (r.get("title") or "").strip() or url, "url": url,
                    "content": (r.get("content") or "")[:1500], "retrieved_at": _now()})
    return out


def _now() -> str:
    from .verify import now_iso

    return now_iso()


def extract(urls: list[str]) -> dict[str, str]:
    """Full text of official pages (Tavily Extract, basic depth: 1 credit per 5 pages). {url: text}.

    Used to check quotes and page dates against the whole page, not just the search excerpt. Pages Tavily could
    not read are simply missing from the result; a failed call raises ServiceError."""
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise ServiceError("TAVILY_API_KEY is not set")
    urls = [u for u in dict.fromkeys(urls) if is_official(u)][:20]  # Tavily accepts at most 20 URLs per call
    if not urls:
        return {}
    resp = _post("Tavily", TAVILY_EXTRACT_URL, headers={"Authorization": f"Bearer {key}"},
                 payload={"urls": urls, "extract_depth": "basic", "format": "text"},
                 timeout=httpx.Timeout(30.0, connect=10.0))
    if resp.status_code != 200:
        msg = TAVILY_ERRORS.get(resp.status_code, f"Tavily extract failed ({resp.status_code})")
        raise ServiceError(f"{msg}: {_error_detail(resp)}")
    try:
        results = resp.json().get("results") or []
    except (ValueError, AttributeError) as exc:
        raise ServiceError("Tavily returned an unreadable response") from exc
    return {r["url"]: (r.get("raw_content") or "")[:MAX_PAGE_CHARS] for r in results
            if isinstance(r, dict) and is_official(r.get("url") or "") and r.get("raw_content")}


# ---------------------------------------------------------------- Nebius Token Factory

def extract_json(text: str) -> dict | None:
    """The JSON object in a model reply, tolerating <think> blocks, ```json fences and prose around it."""
    if not text:
        return None
    # Reasoning models may inline their thinking. Drop closed blocks, and anything before a stray closing tag
    # (some chat templates open <think> in the prompt, so only "</think>" shows up in the output).
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    if re.search(r"</think>", text, re.I):
        text = re.split(r"</think>", text, flags=re.I)[-1]
    text = text.strip()
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)] + [text]
    for c in candidates:
        try:
            obj = json.loads(c.strip())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    # Prose around the object: decode from every "{" and keep the largest object found.
    best, best_len, decoder = None, 0, json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, end = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and end - m.start() > best_len:
            best, best_len = obj, end - m.start()
    return best


def _content_text(content) -> str:
    if isinstance(content, list):  # content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


NEBIUS_ERRORS = {
    401: "Nebius rejected the API key (401): check NEBIUS_API_KEY",
    402: "Nebius budget exhausted (402): add credits in the Token Factory console",
    403: "Nebius refused the request (403)",
    404: "model not found on Token Factory (404): check NEBIUS_MODEL against the model list",
    429: "Nebius rate limit reached (429): wait a moment and try again",
}


def chat_json(system: str, user: str | list, max_tokens: int | None = None) -> dict:
    """Ask the model for a JSON object. Raises ServiceError with a readable message on any failure."""
    key = os.getenv("NEBIUS_API_KEY")
    if not key:
        raise ServiceError("NEBIUS_API_KEY is not set")
    base = os.getenv("NEBIUS_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.getenv("NEBIUS_MODEL", DEFAULT_MODEL)
    # Reasoning tokens count against this limit, so leave room for thinking before the answer.
    max_tokens = max_tokens or int(os.getenv("NEBIUS_MAX_TOKENS", "8192"))
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    # Optional extras. If the model rejects one (400/422), retry without it and stop sending it.
    extras = {"response_format": {"type": "json_object"}}
    effort = os.getenv("NEBIUS_REASONING_EFFORT", "").strip()
    if effort:
        extras["reasoning_effort"] = effort  # documented values: none, minimal, low, medium, high, xhigh, max
    timeout = httpx.Timeout(float(os.getenv("NEBIUS_TIMEOUT", "180")), connect=10.0)
    while True:
        sent = {k: v for k, v in extras.items() if (model, k) not in _unsupported}
        resp = _post("Nebius Token Factory", f"{base}/chat/completions",
                     headers={"Authorization": f"Bearer {key}"}, payload={**payload, **sent}, timeout=timeout)
        if resp.status_code in (400, 422) and sent:
            detail = _error_detail(resp).lower()
            blamed = [k for k in sent if k in detail] or list(sent)
            _unsupported.update((model, k) for k in blamed)
            continue
        break
    if resp.status_code != 200:
        msg = NEBIUS_ERRORS.get(resp.status_code, f"model call failed ({resp.status_code})")
        raise ServiceError(f"{msg}: {_error_detail(resp)}")
    try:
        choice = resp.json()["choices"][0]
        message = choice.get("message") or {}
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ServiceError("model returned an unreadable response") from exc
    content = _content_text(message.get("content"))
    found = extract_json(content)
    if found is None:
        # Some servers put everything in the reasoning field when their reasoning parser misfires.
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        found = extract_json(reasoning if isinstance(reasoning, str) else "")
    if found is None:
        if choice.get("finish_reason") == "length":
            raise ServiceError(f"the model ran out of tokens before answering (max_tokens={max_tokens}); "
                               "raise NEBIUS_MAX_TOKENS or set NEBIUS_REASONING_EFFORT=low")
        raise ServiceError("model did not return JSON" if content.strip() else "model returned an empty answer")
    return found

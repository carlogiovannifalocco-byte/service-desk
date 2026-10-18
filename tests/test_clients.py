"""Request shapes and response parsing for Tavily and Nebius Token Factory, with HTTP mocked."""

import re

import httpx
import pytest

from service_desk import clients

OFFICIAL = {"title": "Codice fiscale", "url": "https://www.agenziaentrate.gov.it/portale/cf", "content": "Free."}


class FakeHTTP:
    """Replaces httpx.post: records each call and plays back the given responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(body, status=200, headers=None):
    return httpx.Response(status, json=body, headers=headers)


def chat(content, finish="stop", **message):
    return ok({"choices": [{"message": {"role": "assistant", "content": content, **message},
                            "finish_reason": finish}]})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-test")
    for k in ("NEBIUS_MODEL", "NEBIUS_BASE_URL", "NEBIUS_REASONING_EFFORT", "NEBIUS_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(clients, "_sleep", lambda s: None)
    clients._unsupported.clear()


def strip_time(result: dict) -> dict:
    return {k: v for k, v in result.items() if k != "retrieved_at"}


def install(monkeypatch, *responses):
    fake = FakeHTTP(*responses)
    monkeypatch.setattr(clients.httpx, "post", fake)
    return fake


# ---------------------------------------------------------------- Tavily

def test_tavily_request_shape(monkeypatch):
    fake = install(monkeypatch, ok({"results": [OFFICIAL]}))
    found = clients.search("codice fiscale  stranieri")
    assert [strip_time(r) for r in found] == [OFFICIAL]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", found[0]["retrieved_at"])
    call = fake.calls[0]
    assert call["url"] == "https://api.tavily.com/search"
    assert call["headers"] == {"Authorization": "Bearer tvly-test"}
    body = call["json"]
    assert "api_key" not in body  # the key travels in the header only
    assert body["query"] == "codice fiscale stranieri"
    assert body["search_depth"] == "advanced" and body["topic"] == "general" and body["country"] == "italy"
    assert body["max_results"] == 6 and body["include_domains"] == clients.OFFICIAL_DOMAINS
    assert len(body["include_domains"]) <= 300


def test_tavily_query_is_capped_at_400_chars(monkeypatch):
    fake = install(monkeypatch, ok({"results": []}))
    clients.search("x" * 1000)
    assert len(fake.calls[0]["json"]["query"]) == 400


def test_tavily_tolerates_odd_results(monkeypatch):
    install(monkeypatch, ok({"results": [
        OFFICIAL, OFFICIAL,  # duplicate
        {"url": "https://www.inps.it/x", "title": None, "content": None},
        {"title": "no url"}, "not a dict",
        {"url": "https://agenziaentrate.gov.it.evil.com/", "title": "look-alike", "content": "x"},
    ]}))
    out = clients.search("q")
    assert [r["url"] for r in out] == [OFFICIAL["url"], "https://www.inps.it/x"]
    assert strip_time(out[1]) == {"title": "https://www.inps.it/x", "url": "https://www.inps.it/x", "content": ""}


def test_tavily_missing_results_key(monkeypatch):
    install(monkeypatch, ok({"query": "q", "response_time": 1.2}))
    assert clients.search("q") == []


def test_tavily_retries_rate_limit_then_succeeds(monkeypatch):
    waits = []
    monkeypatch.setattr(clients, "_sleep", waits.append)
    fake = install(monkeypatch, ok({"detail": {"error": "slow down"}}, 429, {"Retry-After": "2"}),
                   ok({"results": [OFFICIAL]}))
    assert [strip_time(r) for r in clients.search("q")] == [OFFICIAL]
    assert len(fake.calls) == 2 and waits == [2.0]


@pytest.mark.parametrize("status,text", [(401, "TAVILY_API_KEY"), (432, "credit limit"), (433, "pay-as-you-go"),
                                         (400, r"Tavily search failed \(400\)")])
def test_tavily_errors_are_readable(monkeypatch, status, text):
    install(monkeypatch, ok({"detail": {"error": "nope"}}, status))
    with pytest.raises(clients.ServiceError, match=text) as exc:
        clients.search("q")
    assert "nope" in str(exc.value) and "tvly-test" not in str(exc.value)


def test_tavily_gives_up_after_retries(monkeypatch):
    fake = install(monkeypatch, *[ok({"detail": "busy"}, 503)] * 3)
    with pytest.raises(clients.ServiceError, match="503"):
        clients.search("q")
    assert len(fake.calls) == clients.ATTEMPTS


def test_tavily_timeout_and_unreachable(monkeypatch):
    install(monkeypatch, httpx.ReadTimeout("slow"))
    with pytest.raises(clients.ServiceError, match="did not answer in time"):
        clients.search("q")
    fake = install(monkeypatch, *[httpx.ConnectError("dns")] * 3)
    with pytest.raises(clients.ServiceError, match="could not reach Tavily"):
        clients.search("q")
    assert len(fake.calls) == 3


def test_tavily_non_json_body(monkeypatch):
    install(monkeypatch, httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(clients.ServiceError, match="unreadable"):
        clients.search("q")


def test_tavily_failure_reaches_the_api_as_503(monkeypatch):
    from fastapi.testclient import TestClient

    from service_desk.main import app

    install(monkeypatch, ok({"detail": {"error": "Unauthorized: missing or invalid API key."}}, 401))
    r = TestClient(app).post("/api/plan", json={"task": "spid", "city": "Roma", "nationality": "EU"})
    assert r.status_code == 503 and "TAVILY_API_KEY" in r.json()["detail"]


# ---------------------------------------------------------------- Nebius Token Factory

def test_nebius_request_shape(monkeypatch):
    fake = install(monkeypatch, chat('{"summary": "ok"}'))
    assert clients.chat_json("sys JSON", "hello") == {"summary": "ok"}
    call = fake.calls[0]
    assert call["url"] == "https://api.tokenfactory.nebius.com/v1/chat/completions"
    assert call["headers"] == {"Authorization": "Bearer nb-test"}
    body = call["json"]
    assert body["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "system", "content": "sys JSON"}, {"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 8192 and "reasoning_effort" not in body
    assert call["timeout"].read == 180


def test_nebius_env_overrides(monkeypatch):
    monkeypatch.setenv("NEBIUS_BASE_URL", "https://api.tokenfactory.us-central1.nebius.com/v1/")
    monkeypatch.setenv("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning")
    monkeypatch.setenv("NEBIUS_REASONING_EFFORT", "low")
    monkeypatch.setenv("NEBIUS_MAX_TOKENS", "4000")
    fake = install(monkeypatch, chat("{}"))
    clients.chat_json("s", "u")
    call = fake.calls[0]
    assert call["url"] == "https://api.tokenfactory.us-central1.nebius.com/v1/chat/completions"
    assert call["json"]["model"] == "nvidia/Nemotron-3_5-Lightning"
    assert call["json"]["reasoning_effort"] == "low" and call["json"]["max_tokens"] == 4000


@pytest.mark.parametrize("content", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    'Here you go:\n```\n{"a": 1}\n```\nAnything else?',
    '<think>The user wants {"b": 2}? No, {"a": 1}.</think>\n{"a": 1}',
    'reasoning that the template opened\n</think>\n\n```json\n{"a": 1}\n```',
    'Sure! The plan is {"a": 1} as requested.',
    'Example {"x": 0} then the answer {"a": 1, "z": [1, 2, {"y": "}"}]}',
])
def test_json_is_found_in_messy_replies(content):
    out = clients.extract_json(content)
    assert out is not None and out.get("a") == 1


def test_extract_json_gives_up_cleanly():
    assert clients.extract_json("") is None
    assert clients.extract_json("no json here") is None
    assert clients.extract_json('[1, 2]') is None
    assert clients.extract_json('{"a": 1') is None


def test_nebius_reply_with_thinking_and_fences(monkeypatch):
    install(monkeypatch, chat('<think>let me check the sources</think>\n```json\n{"steps": []}\n```'))
    assert clients.chat_json("s", "u") == {"steps": []}


def test_nebius_reasoning_field_is_ignored_when_content_has_json(monkeypatch):
    install(monkeypatch, chat('{"a": 1}', reasoning_content='thinking {"a": 2}'))
    assert clients.chat_json("s", "u") == {"a": 1}


def test_nebius_json_only_in_reasoning_field(monkeypatch):
    install(monkeypatch, chat(None, reasoning_content='...so the answer is {"a": 1}'))
    assert clients.chat_json("s", "u") == {"a": 1}


def test_nebius_content_parts(monkeypatch):
    install(monkeypatch, chat([{"type": "text", "text": '{"a":'}, {"type": "text", "text": " 1}"}]))
    assert clients.chat_json("s", "u") == {"a": 1}


def test_nebius_out_of_tokens_while_thinking(monkeypatch):
    install(monkeypatch, chat(None, finish="length", reasoning_content="hmm, first I should"))
    with pytest.raises(clients.ServiceError, match="ran out of tokens.*NEBIUS_MAX_TOKENS"):
        clients.chat_json("s", "u")


def test_nebius_no_json(monkeypatch):
    install(monkeypatch, chat("I cannot help with that."))
    with pytest.raises(clients.ServiceError, match="did not return JSON"):
        clients.chat_json("s", "u")
    install(monkeypatch, chat(""))
    with pytest.raises(clients.ServiceError, match="empty answer"):
        clients.chat_json("s", "u")


def test_nebius_unexpected_body(monkeypatch):
    install(monkeypatch, ok({"choices": []}))
    with pytest.raises(clients.ServiceError, match="unreadable"):
        clients.chat_json("s", "u")


def test_nebius_retries_without_unsupported_response_format(monkeypatch):
    fake = install(monkeypatch,
                   ok({"error": {"message": "response_format json_object is not supported for this model"}}, 400),
                   chat('{"a": 1}'), chat('{"a": 2}'))
    assert clients.chat_json("s", "u") == {"a": 1}
    assert "response_format" in fake.calls[0]["json"] and "response_format" not in fake.calls[1]["json"]
    # Remembered for this model: the next call goes straight to the working shape.
    assert clients.chat_json("s", "u") == {"a": 2}
    assert "response_format" not in fake.calls[2]["json"] and len(fake.calls) == 3


def test_nebius_drops_only_the_rejected_extra(monkeypatch):
    monkeypatch.setenv("NEBIUS_REASONING_EFFORT", "low")
    fake = install(monkeypatch, ok({"detail": [{"loc": ["body", "reasoning_effort"], "msg": "bad reasoning_effort"}]}, 422),
                   chat("{}"))
    clients.chat_json("s", "u")
    assert "reasoning_effort" not in fake.calls[1]["json"]
    assert fake.calls[1]["json"]["response_format"] == {"type": "json_object"}


def test_nebius_bad_request_without_extras_is_an_error(monkeypatch):
    fake = install(monkeypatch, *[ok({"detail": "messages: field required"}, 400)] * 2)
    with pytest.raises(clients.ServiceError, match="model call failed \\(400\\).*field required"):
        clients.chat_json("s", "u")
    assert len(fake.calls) == 2  # one with response_format, one without, then stop


def test_nebius_retries_overload(monkeypatch):
    waits = []
    monkeypatch.setattr(clients, "_sleep", waits.append)
    fake = install(monkeypatch, ok({"detail": "overloaded"}, 529), ok({"detail": "busy"}, 503), chat("{}"))
    assert clients.chat_json("s", "u") == {}
    assert len(fake.calls) == 3 and waits == [1.5, 3.0]


@pytest.mark.parametrize("status,text", [(401, "NEBIUS_API_KEY"), (402, "budget"), (404, "NEBIUS_MODEL"),
                                         (429, "rate limit")])
def test_nebius_errors_are_readable(monkeypatch, status, text):
    install(monkeypatch, *[ok({"detail": "Couldn't authenticate. Reason: token is not present"}, status)] * 3)
    with pytest.raises(clients.ServiceError, match=text) as exc:
        clients.chat_json("s", "u")
    assert "nb-test" not in str(exc.value)


def test_nebius_timeout(monkeypatch):
    install(monkeypatch, httpx.ReadTimeout("slow"))
    with pytest.raises(clients.ServiceError, match="did not answer in time"):
        clients.chat_json("s", "u")


def test_missing_keys(monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY")
    monkeypatch.delenv("TAVILY_API_KEY")
    with pytest.raises(clients.ServiceError, match="NEBIUS_API_KEY is not set"):
        clients.chat_json("s", "u")
    with pytest.raises(clients.ServiceError, match="TAVILY_API_KEY is not set"):
        clients.search("q")

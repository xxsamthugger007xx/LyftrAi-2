"""Backend tests. LiteLLM (call path + built-in cache) is real; only the provider behind it is a fake."""
import asyncio
import json
import logging

import litellm
import pytest
from fastapi.testclient import TestClient
from litellm import CustomLLM

from app.config import Settings
from app.main import create_app

KEYS = ["sk-secret-one", "sk-secret-two"]
BEHAVIOUR: dict[str, str] = {}   # api_key -> "ok" | "429" | "500" | "slow"
CALLS: list[dict] = []           # every provider call: {"key": ..., "messages": [...]}
DELAY = {"live": 0.0}


class FakeProvider(CustomLLM):
    async def acompletion(self, model, messages, api_key=None, **kw):
        CALLS.append({"key": api_key, "messages": messages})
        mode = BEHAVIOUR.get(api_key, "ok")
        if mode == "429":
            raise litellm.RateLimitError(message="quota", llm_provider="fake", model=model)
        if mode == "500":
            raise litellm.InternalServerError(message="boom", llm_provider="fake", model=model)
        if mode == "slow":
            await asyncio.sleep(30)
        await asyncio.sleep(DELAY["live"])
        prompt_tokens = sum(len(m["content"].split()) for m in messages)
        return litellm.ModelResponse(
            model=model,
            choices=[{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": f"echo: {messages[-1]['content']}"}}],
            usage=litellm.Usage(prompt_tokens=prompt_tokens, completion_tokens=4, total_tokens=prompt_tokens + 4),
        )


litellm.custom_provider_map = [{"provider": "fake", "custom_handler": FakeProvider()}]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    BEHAVIOUR.clear(); CALLS.clear(); DELAY["live"] = 0.0
    monkeypatch.setenv("LLM_API_KEYS", ",".join(KEYS))
    monkeypatch.setenv("LLM_MODEL", "fake/test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("ENABLE_FAILURE_SIMULATION", "true")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.3")
    return TestClient(create_app(Settings()))


def chat(c, sid, msg, **kw):
    headers = kw.pop("headers", None)
    return c.post("/chat", json={"session_id": sid, "message": msg, **kw}, headers=headers)


def meta(r):
    return r.json()["metadata"]


# ------------------------------------------------------------------ health / root
def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}
    assert CALLS == []  # health never performs inference


def test_health_reflects_real_key_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEYS", "a, b ,c")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/h.db")
    assert TestClient(create_app(Settings())).get("/health").json()["keys_configured"] == 3


def test_root_json_and_html(client):
    assert client.get("/").json()["health"] == "/health"
    html = client.get("/", headers={"accept": "text/html"})
    assert html.status_code == 200 and "Lyftr Chat API" in html.text


# ------------------------------------------------------------------ chat basics / multi-turn
def test_live_inference_contract(client):
    r = chat(client, "a", "hello there")
    assert r.status_code == 200
    body, m = r.json(), meta(r)
    assert body["session_id"] == "a" and body["reply"] == "echo: hello there"
    assert m["resolution_source"] == "LIVE_INFERENCE" and m["key_id"] == "key_1" and m["fallback_attempts"] == 0
    assert isinstance(m["latency_ms"], int) and m["model"] == "fake/test"
    assert set(m) == {"resolution_source", "model", "latency_ms", "input_tokens", "output_tokens", "key_id", "fallback_attempts"}


def test_multi_turn_sends_full_history_and_sessions_are_isolated(client):
    chat(client, "s1", "first")
    chat(client, "s1", "second")
    chat(client, "other", "unrelated")
    sent = [c["messages"] for c in CALLS if c["messages"][-1]["content"] == "second"][0]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "first" and sent[2]["content"] == "echo: first"
    h1 = client.get("/sessions/s1/messages").json()["messages"]
    assert [m["role"] for m in h1] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in client.get("/sessions/other/messages").json()["messages"]][0] == "unrelated"
    assert all(m["role"] != "system" for m in h1)
    assert "metadata" in h1[1] and "metadata" not in h1[0]


def test_system_prompt_is_used_and_persisted_for_session(client):
    chat(client, "sp", "hi", system_prompt="Be a pirate")
    chat(client, "sp", "again")
    assert CALLS[0]["messages"][0] == {"role": "system", "content": "Be a pirate"}
    assert CALLS[1]["messages"][0] == {"role": "system", "content": "Be a pirate"}


# ------------------------------------------------------------------ caching
def test_cache_hit_same_first_message_in_new_session(client):
    live = chat(client, "s1", "capital of France?")
    hit = chat(client, "s2", "capital of France?")
    assert hit.status_code == 200
    assert meta(hit)["resolution_source"] == "LLM_CACHE"
    assert meta(hit)["key_id"] is None and meta(hit)["fallback_attempts"] == 0
    assert hit.json()["reply"] == live.json()["reply"]                      # identical text
    assert meta(hit)["input_tokens"] == meta(live)["input_tokens"]          # usage of the cached request
    assert meta(hit)["output_tokens"] == meta(live)["output_tokens"]
    assert len(CALLS) == 1                                                  # provider hit once only


def test_cache_miss_for_different_message(client):
    chat(client, "s1", "alpha")
    r = chat(client, "s2", "beta")
    assert meta(r)["resolution_source"] == "LIVE_INFERENCE" and r.json()["reply"] == "echo: beta"


def test_cache_key_includes_system_prompt(client):
    chat(client, "s1", "same text", system_prompt="Persona A")
    assert meta(chat(client, "s2", "same text", system_prompt="Persona B"))["resolution_source"] == "LIVE_INFERENCE"
    assert meta(chat(client, "s3", "same text", system_prompt="Persona A"))["resolution_source"] == "LLM_CACHE"


def test_cache_key_includes_history(client):
    chat(client, "x", "context one"); chat(client, "y", "context two")
    a = chat(client, "x", "and then?")           # history: context one
    b = chat(client, "y", "and then?")           # same last message, different history -> must NOT reuse
    assert meta(a)["resolution_source"] == "LIVE_INFERENCE" and meta(b)["resolution_source"] == "LIVE_INFERENCE"
    assert [c for c in CALLS if c["messages"][-1]["content"] == "and then?"].__len__() == 2


def test_cache_hit_faster_than_live(client):
    DELAY["live"] = 0.15
    live = chat(client, "s1", "slow question")
    hit = chat(client, "s2", "slow question")
    assert meta(hit)["latency_ms"] < meta(live)["latency_ms"]


# ------------------------------------------------------------------ token tracking
def test_tokens_are_per_request_not_cumulative(client):
    a = meta(chat(client, "t1", "one two three"))
    b = meta(chat(client, "t2", "four five six"))          # equal length, new session
    assert a["input_tokens"] == b["input_tokens"] > 0 and a["output_tokens"] == 4
    c = meta(chat(client, "t1", "next"))                    # grows with history, but never a running total
    expected = sum(len(m["content"].split()) for m in CALLS[-1]["messages"])
    assert c["input_tokens"] == expected


# ------------------------------------------------------------------ failover (real provider errors)
@pytest.mark.parametrize("mode", ["429", "500"])
def test_real_provider_error_fails_over(client, mode):
    BEHAVIOUR[KEYS[0]] = mode
    r = chat(client, "f", f"hello {mode}")
    assert r.status_code == 200
    assert meta(r)["key_id"] == "key_2" and meta(r)["fallback_attempts"] == 1
    assert [c["key"] for c in CALLS] == [KEYS[0], KEYS[1]]


def test_provider_timeout_fails_over_and_is_bounded(client):
    BEHAVIOUR[KEYS[0]] = "slow"
    r = chat(client, "slow", "will time out on key 1")
    assert r.status_code == 200 and meta(r)["key_id"] == "key_2" and meta(r)["fallback_attempts"] == 1
    assert meta(r)["latency_ms"] < 3000   # bounded by the per-attempt timeout, not by the 30 s stall


def test_all_providers_timing_out_returns_503_quickly(client):
    BEHAVIOUR.update({KEYS[0]: "slow", KEYS[1]: "slow"})
    r = chat(client, "slow2", "everything stalls")
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}
    assert "Server-Timing" in r.headers


def test_all_keys_failing_503_and_nothing_saved(client):
    BEHAVIOUR.update({KEYS[0]: "429", KEYS[1]: "500"})
    r = chat(client, "dead", "hi")
    assert r.status_code == 503 and r.json() == {"detail": "all providers unavailable"}
    assert client.get("/sessions/dead/messages").status_code == 404
    BEHAVIOUR.clear()
    ok = chat(client, "dead", "hi again")
    assert [m["role"] for m in client.get("/sessions/dead/messages").json()["messages"]] == ["user", "assistant"]
    assert ok.status_code == 200


# ------------------------------------------------------------------ failure simulation header
@pytest.mark.parametrize("hdr", ["key_1=429", "key_1=500", "key_1=502", "key_1=503"])
def test_simulated_failure_fails_over_with_real_answer(client, hdr):
    r = chat(client, "sim", "simulate me", headers={"X-Simulate-Failure": hdr})
    assert r.status_code == 200 and r.json()["reply"] == "echo: simulate me"
    assert meta(r)["key_id"] == "key_2" and meta(r)["fallback_attempts"] == 1
    assert [c["key"] for c in CALLS] == [KEYS[1]]  # key_1 never reached the provider


def test_simulation_multiple_pairs_and_wildcard(client):
    r = chat(client, "w", "x", headers={"X-Simulate-Failure": "key_1=429,key_2=500"})
    assert r.status_code == 503
    r = chat(client, "w", "x", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 503 and client.get("/sessions/w/messages").status_code == 404


def test_simulation_only_affects_that_request(client):
    chat(client, "one", "a", headers={"X-Simulate-Failure": "*=429"})
    assert meta(chat(client, "one", "a"))["key_id"] == "key_1"


def test_simulation_ignored_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEYS", ",".join(KEYS)); monkeypatch.setenv("LLM_MODEL", "fake/test")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/d.db"); monkeypatch.setenv("ENABLE_FAILURE_SIMULATION", "false")
    c = TestClient(create_app(Settings()))
    r = chat(c, "d", "hi", headers={"X-Simulate-Failure": "*=429"})
    assert r.status_code == 200 and meta(r)["key_id"] == "key_1"


def test_simulated_request_can_fail_over_even_if_message_is_cached(client):
    chat(client, "c1", "cached text")
    r = chat(client, "c2", "cached text", headers={"X-Simulate-Failure": "key_1=429"})
    assert meta(r)["key_id"] == "key_2"


# ------------------------------------------------------------------ validation and error shape
@pytest.mark.parametrize("body", [{}, {"session_id": "", "message": "x"}, {"session_id": "a", "message": "   "},
                                  {"session_id": "a", "message": "x" * 4001}, {"session_id": "s" * 129, "message": "x"},
                                  {"session_id": "a"}, {"message": "x"}])
def test_invalid_input_422(client, body):
    r = client.post("/chat", json=body)
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert CALLS == []


def test_message_is_trimmed_and_4000_chars_allowed(client):
    assert chat(client, "t", "  padded  ").json()["reply"] == "echo: padded"
    assert chat(client, "t2", "y" * 4000).status_code == 200


def test_malformed_json_422(client):
    r = client.post("/chat", content="{bad", headers={"content-type": "application/json"})
    assert r.status_code == 422 and "detail" in r.json()


def test_unknown_session_404(client):
    assert client.get("/sessions/nope/messages").json() == {"detail": "session not found"}


def test_unexpected_error_is_structured_json_without_trace(client, monkeypatch):
    def boom(*a, **k): raise RuntimeError("secret internal detail")
    monkeypatch.setattr(client.app.state.db, "load_context", boom)
    c = TestClient(client.app, raise_server_exceptions=False)
    r = c.post("/chat", json={"session_id": "e", "message": "hi"})
    assert r.status_code == 500 and r.json() == {"detail": "internal server error"}
    assert "secret internal detail" not in r.text and "Traceback" not in r.text


# ------------------------------------------------------------------ instrumentation / secrecy
def test_timing_header_and_structured_log_without_secrets(client, caplog):
    caplog.set_level(logging.INFO)
    BEHAVIOUR[KEYS[0]] = "429"
    r = chat(client, "log", "please log this")
    st = r.headers["Server-Timing"]
    for part in ("total", "history", "cache_lookup", "inference", "persist"):
        assert f"{part};dur=" in st
    lines = [json.loads(rec.message) for rec in caplog.records if rec.name == "lyftr.timing"]
    assert lines and lines[-1]["status"] == 200 and lines[-1]["key_id"] == "key_2"
    for k in ("total_ms", "history_ms", "cache_lookup_ms", "inference_ms", "persist_ms"):
        assert k in lines[-1]
    everything = caplog.text + r.text + st
    assert not any(k in everything for k in KEYS)
    assert "please log this" not in caplog.text


def test_503_body_and_logs_never_contain_keys(client, caplog):
    caplog.set_level(logging.INFO)
    BEHAVIOUR.update({KEYS[0]: "429", KEYS[1]: "500"})
    r = chat(client, "k", "hi")
    assert not any(k in r.text + caplog.text + str(r.headers) for k in KEYS)

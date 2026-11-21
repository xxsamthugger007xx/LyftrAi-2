# LLM Chatbot with Key Failover and Response Caching

React + FastAPI + LiteLLM + SQLite.

## 1. Live URLs
- Frontend: https://lyftr-ai-2.vercel.app
- Backend: https://lyftr-chat-backend-2.onrender.com ([`/health`](https://lyftr-chat-backend-2.onrender.com/health))

> The backend runs on Render's free tier: it sleeps after ~15 min idle (first request can take ~50 s) and, without a persistent disk, SQLite history is reset on restart/redeploy. Locally with Docker the `/data` volume persists history.

## 2. Run locally
**With Docker** (frontend :3000, backend :8000):
```bash
cp .env.example .env   # put 2+ real keys in LLM_API_KEYS
make up                # make down to stop
```
**Without Docker**:
```bash
cd backend && pip install -r requirements.txt
export LLM_API_KEYS=k1,k2 DATABASE_URL=sqlite:///./app.db ALLOWED_ORIGINS=http://localhost:3000 ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:app --port 8000
cd frontend && npm install && npm run dev      # http://localhost:3000
```
Backend tests: `make test` (provider is mocked).

## 3. Design decisions
- **Key fallback**: `LLM_API_KEYS` becomes ordered ids `key_1..key_n`. Each request tries keys in order via `litellm.acompletion(api_key=...)`; any provider error (429, quota, 5xx, timeout) moves to the next key and increments `fallback_attempts`. If all fail → `503 {"detail":"all providers unavailable"}` and nothing is written to history.
- **Caching**: LiteLLM's built-in in-memory cache (`litellm.cache = Cache(type="local")`). Its key is derived from model + full message list (system + history + new message), and excludes the API key, so a hit is shared regardless of which key would answer. Hits are detected from `_hidden_params["cache_hit"]`, report `LLM_CACHE`, `key_id: null`, and the usage stored with the cached response. Cache is lost on restart (history is not).
- **Session storage**: SQLite (`messages` table per the spec, plus a `sessions` table for the per-session system prompt). File lives on the `appdata` volume at `/data/app.db`. Schema is created on startup. User + assistant rows are written together only after success.
- **Failure simulation**: `X-Simulate-Failure` is honored only if `ENABLE_FAILURE_SIMULATION=true`. A simulated request skips cache reads so failover can be shown on an already-cached message.
- Errors are always JSON `{"detail": ...}`; unhandled exceptions return a generic 500 without a trace.

## Performance and reliability notes
- **Bounded requests**: each key attempt has a hard timeout (`LLM_TIMEOUT_SECONDS`, default 12 s, enforced by LiteLLM and again by `asyncio.wait_for`), so a stalled provider can delay failover by at most ~12 s and a request can never hang (worst case ≈ 12 s × number of keys, then a clean 503).
- **Timing instrumentation**: every `/chat` writes one JSON log line (`lyftr.timing`) with `total_ms`, `history_ms`, `cache_lookup_ms`, `inference_ms`, `cache_store_ms`, `persist_ms`, plus the same data in a `Server-Timing` response header. Logs never contain keys, prompts or replies.
- **Frontend**: a single `/health` call on load shows "Connecting to AI service… / AI service ready"; each reply shows the backend `latency_ms` and, separately, the client-observed round trip. Requests time out after 90 s with a Retry option.

## 4. Verification
```bash
B=http://localhost:8000
curl $B/health
# cache: same message, two sessions -> second is LLM_CACHE, key_id null, faster
curl -s $B/chat -H 'content-type: application/json' -d '{"session_id":"a","message":"What is the capital of France?"}'
curl -s $B/chat -H 'content-type: application/json' -d '{"session_id":"b","message":"What is the capital of France?"}'
# failover: answered by key_2, fallback_attempts 1
curl -s $B/chat -H 'content-type: application/json' -H 'X-Simulate-Failure: key_1=429' -d '{"session_id":"c","message":"hello"}'
# all keys fail -> 503, not stored
curl -si $B/chat -H 'content-type: application/json' -H 'X-Simulate-Failure: *=429' -d '{"session_id":"d","message":"hello"}'
curl -s $B/sessions/c/messages
```
In the UI every assistant message shows source, model, latency and per-request input/output tokens.

## 5. Setup Used
- Editor: VS Code
- AI tools: Claude Code (Claude Sonnet 5.5) for scaffolding, implementation and debugging
- Stack: React 18 + Vite, FastAPI, LiteLLM 1.74, SQLite, Docker Compose

## 6. Bonus features
- Optional per-session system prompt field, persisted per session in SQLite.
- Polished UI: starter prompts, light/dark theme, markdown/code rendering, loading indicator, per-reply badges showing which key answered and how many failovers occurred (useful for demoing failover).
- Backend tests (`make test`) covering caching, failover, 503 and 422 handling with a mocked provider.
- API keys are never logged (HTTP client logs are silenced because they include key-bearing URLs).

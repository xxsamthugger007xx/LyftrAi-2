import asyncio
import json
import logging
import time

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from .config import Settings
from .db import Database
from .llm import AllProvidersUnavailable, LLMGateway, parse_simulation

logging.basicConfig(level=logging.INFO)
for _n in ("httpx", "LiteLLM", "litellm"):
    logging.getLogger(_n).setLevel(logging.WARNING)  # httpx logs URLs that contain the API key


timing_log = logging.getLogger("lyftr.timing")


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _server_timing(t: dict) -> str:
    return ", ".join(f"{k[:-3]};dur={v}" for k, v in t.items())


def _log_timing(event, status, session_id, source, key_id, attempts, timings, reasons=None) -> None:
    """One JSON line per request. Never contains keys, prompts or replies."""
    timing_log.info(json.dumps({
        "event": event, "status": status, "session_id": session_id[:16], "source": source,
        "key_id": key_id, "fallback_attempts": attempts, **timings, **({"failures": reasons} if reasons else {}),
    }))


LANDING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lyftr Chat API</title><style>
:root{--bg:#f6f7fb;--card:#fff;--text:#121826;--muted:#667089;--bd:#e2e6f0;--p:#4f46e5}
@media(prefers-color-scheme:dark){:root{--bg:#0c0f17;--card:#151a26;--text:#e8ebf5;--muted:#8b95b0;--bd:#262d3f;--p:#8b83ff}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;background:var(--bg);color:var(--text);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif}
.card{width:100%;max-width:560px;background:var(--card);border:1px solid var(--bd);border-radius:18px;padding:32px;box-shadow:0 8px 30px rgba(16,24,40,.08)}
.logo{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;color:#fff;background:linear-gradient(135deg,#4f46e5,#7c3aed);margin-bottom:14px}
h1{margin:0 0 4px;font-size:1.5rem;letter-spacing:-.02em}p{margin:0 0 18px;color:var(--muted)}
.status{display:inline-flex;align-items:center;gap:8px;padding:4px 12px;border-radius:999px;background:#e4f6ec;color:#166a3b;font-weight:600;font-size:.82rem;margin-bottom:18px}
.dot{width:8px;height:8px;border-radius:50%;background:#22c55e}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}
.btn{padding:9px 16px;border-radius:10px;border:1px solid var(--bd);text-decoration:none;color:var(--text);font-weight:600;font-size:.9rem}
.btn.primary{background:var(--p);border-color:var(--p);color:#fff}
table{width:100%;border-collapse:collapse;font-size:.88rem}td{padding:8px 0;border-top:1px solid var(--bd)}
td:first-child{width:70px;font-weight:700;color:var(--p)}code{font:.85em ui-monospace,Menlo,monospace}
.foot{margin-top:18px;font-size:.78rem;color:var(--muted)}
</style></head><body><div class="card">
<div class="logo"><svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z"/></svg></div>
<h1>Lyftr Chat API</h1><p>LLM chatbot backend with API key failover and response caching.</p>
<div class="status"><span class="dot"></span>Operational &middot; {{KEYS}} keys in pool</div>
<div class="row">{{FRONTEND}}<a class="btn" href="/docs">API docs</a><a class="btn" href="/health">/health</a></div>
<table>
<tr><td>GET</td><td><code>/health</code> &mdash; service status</td></tr>
<tr><td>POST</td><td><code>/chat</code> &mdash; send a message in a session</td></tr>
<tr><td>GET</td><td><code>/sessions/{session_id}/messages</code> &mdash; stored history</td></tr>
</table>
<div class="foot">FastAPI &middot; LiteLLM &middot; SQLite</div>
</div></body></html>"""


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str
    system_prompt: str | None = None

    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= 4000:
            raise ValueError("message must be 1-4000 characters")
        return v

    @field_validator("system_prompt")
    @classmethod
    def _sp(cls, v):
        v = v.strip() if v else None
        return v or None


def create_app(settings: Settings | None = None, gateway: LLMGateway | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Lyftr Chatbot")
    db = Database(settings.db_path)
    gateway = gateway or LLMGateway(settings)
    app.state.db, app.state.gateway = db, gateway

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        msg = "; ".join(f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors())
        return JSONResponse(status_code=422, content={"detail": msg or "invalid request"})

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        logging.exception("unhandled error")
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        info = {
            "service": "Lyftr Chat API",
            "status": "ok",
            "health": "/health",
            "docs": "/docs",
            "endpoints": ["GET /health", "POST /chat", "GET /sessions/{session_id}/messages"],
        }
        if "text/html" not in request.headers.get("accept", ""):
            return info
        public = [o for o in settings.allowed_origins if "localhost" not in o and "127.0.0.1" not in o]
        frontend = f'<a class="btn primary" href="{public[0]}">Open the chat app</a>' if public else ""
        return HTMLResponse(LANDING.replace("{{FRONTEND}}", frontend).replace("{{KEYS}}", str(len(gateway.keys))))

    @app.get("/health")
    async def health():
        await asyncio.to_thread(db.ping)
        return {"status": "ok", "keys_configured": len(gateway.keys), "cache_enabled": settings.cache_enabled}

    @app.post("/chat")
    async def chat(req: ChatRequest, response: Response, x_simulate_failure: str | None = Header(default=None)):
        t_start = time.perf_counter()
        sid = req.session_id

        t = time.perf_counter()
        stored_prompt, history = await asyncio.to_thread(db.load_context, sid)  # one thread hop, one connection
        history_ms = _ms(t)

        system_prompt = req.system_prompt or stored_prompt or settings.default_system_prompt
        messages = (
            [{"role": "system", "content": system_prompt}]
            + [{"role": m["role"], "content": m["content"]} for m in history]
            + [{"role": "user", "content": req.message}]
        )
        simulate = parse_simulation(x_simulate_failure) if settings.simulation_enabled else {}

        try:
            result = await gateway.complete(messages, simulate)
        except AllProvidersUnavailable as exc:
            timings = {"total_ms": _ms(t_start), "history_ms": history_ms, **exc.timings}
            _log_timing("chat", 503, sid, None, None, None, timings, exc.reasons)
            raise HTTPException(
                status_code=503,
                detail="all providers unavailable",
                headers={"Server-Timing": _server_timing(timings)},
            )

        # Server-measured time for this request (history load + cache lookup + inference), before persistence.
        latency_ms = int((time.perf_counter() - t_start) * 1000)
        metadata = {
            "resolution_source": result.resolution_source,
            "model": result.model,
            "latency_ms": latency_ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "key_id": result.key_id,
            "fallback_attempts": result.fallback_attempts,
        }
        t = time.perf_counter()
        await asyncio.to_thread(db.add_exchange, sid, req.system_prompt, req.message, result.reply, metadata)
        persist_ms = _ms(t)

        timings = {"total_ms": _ms(t_start), "history_ms": history_ms, **result.timings, "persist_ms": persist_ms}
        _log_timing("chat", 200, sid, result.resolution_source, result.key_id, result.fallback_attempts, timings)
        response.headers["Server-Timing"] = _server_timing(timings)
        return {"session_id": sid, "reply": result.reply, "metadata": metadata}

    @app.get("/sessions/{session_id}/messages")
    async def messages(session_id: str):
        history = await asyncio.to_thread(db.history, session_id)
        if not history:
            raise HTTPException(status_code=404, detail="session not found")
        return {"session_id": session_id, "messages": history}

    return app


app = create_app()

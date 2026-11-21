"""LiteLLM gateway: ordered key pool with failover, built-in response caching, failure simulation.

Every attempt is bounded (per-attempt timeout enforced twice: by LiteLLM and by asyncio), so a request
always ends with a result or AllProvidersUnavailable. Timings are returned for structured logging.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field

import litellm
from litellm.caching.caching import Cache

from .config import Settings

log = logging.getLogger("llm")
litellm.drop_params = True
litellm.suppress_debug_info = True


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed (rate limit, quota, 5xx, timeout, ...)."""

    def __init__(self, timings: dict | None = None, reasons: list[str] | None = None) -> None:
        super().__init__("all providers unavailable")
        self.timings = timings or {}
        self.reasons = reasons or []


@dataclass
class LLMResult:
    reply: str
    resolution_source: str
    model: str
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int
    timings: dict = field(default_factory=dict)


def parse_simulation(header: str | None) -> dict[str, int]:
    """'key_1=429,key_2=500' / '*=429' -> {'key_1': 429, ...}. Invalid pairs are ignored."""
    out: dict[str, int] = {}
    if not header:
        return out
    for part in header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.isdigit() and int(v) in (429, 500, 502, 503):
            out[k] = int(v)
    return out


_EXC = {
    429: litellm.RateLimitError,
    500: litellm.InternalServerError,
    502: litellm.InternalServerError,
    503: litellm.ServiceUnavailableError,
}


def _simulated_error(status: int, model: str):
    return _EXC[status](message=f"simulated {status}", llm_provider="simulated", model=model)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _describe(exc: Exception) -> str:
    """Safe, key-free description of a provider failure (class name + status only, never the message)."""
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f"({status})" if status else "")


class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keys = {f"key_{i}": k for i, k in enumerate(settings.api_keys, start=1)}
        if settings.cache_enabled:
            # Fresh in-memory LiteLLM cache. Its key is derived from the model and the full message list
            # (system prompt + history + new message); the API key and timeout are not part of it.
            litellm.cache = Cache(type="local")

    async def _call(self, key: str, messages: list[dict], caching: bool):
        timeout = self.settings.request_timeout
        # LiteLLM enforces `timeout`; asyncio.wait_for is a hard backstop so nothing can hang a request.
        return await asyncio.wait_for(
            litellm.acompletion(
                model=self.settings.model,
                messages=messages,
                api_key=key,
                caching=caching,
                timeout=timeout,
                num_retries=0,
            ),
            timeout=timeout + 0.5,
        )

    async def _cache_lookup(self, messages: list[dict]):
        try:
            return await litellm.cache.async_get_cache(model=self.settings.model, messages=messages)
        except Exception:  # a cache problem must never break a request
            return None

    async def _ensure_stored(self, messages: list[dict]) -> None:
        """LiteLLM writes cache entries in a background task; wait (briefly) so an immediate repeat hits."""
        for _ in range(10):
            if await self._cache_lookup(messages) is not None:
                return
            await asyncio.sleep(0.002)

    async def complete(self, messages: list[dict], simulate: dict[str, int] | None = None) -> LLMResult:
        simulate = simulate or {}
        timings = {"cache_lookup_ms": 0.0, "inference_ms": 0.0, "cache_store_ms": 0.0}
        # A simulated-failure request skips cache reads so failover can be demonstrated on any message.
        use_cache = self.settings.cache_enabled and not simulate

        if use_cache:
            t = time.perf_counter()
            await self._cache_lookup(messages)
            timings["cache_lookup_ms"] = _ms(t)

        attempts = 0
        reasons: list[str] = []
        for key_id, key in self.keys.items():
            status = simulate.get(key_id, simulate.get("*"))
            t = time.perf_counter()
            try:
                if status:
                    raise _simulated_error(status, self.settings.model)
                resp = await self._call(key, messages, caching=use_cache)
                text = (resp.choices[0].message.content or "").strip()
                if not text:
                    raise ValueError("empty completion")
                usage = resp.usage
            except Exception as exc:  # any provider failure (incl. timeout) -> next key
                timings["inference_ms"] += _ms(t)
                reasons.append(f"{key_id}:{_describe(exc) if not isinstance(exc, asyncio.TimeoutError) else 'Timeout'}")
                log.warning("%s failed (%s)", key_id, reasons[-1].split(":", 1)[1])
                attempts += 1
                continue

            hit = bool(use_cache and (getattr(resp, "_hidden_params", None) or {}).get("cache_hit"))
            elapsed = _ms(t)
            if hit:
                # Whole call was served from LiteLLM's cache: that is the lookup, there was no inference.
                timings["cache_lookup_ms"] = elapsed
            else:
                timings["inference_ms"] += elapsed
                if use_cache:
                    t2 = time.perf_counter()
                    await self._ensure_stored(messages)
                    timings["cache_store_ms"] = _ms(t2)
            return LLMResult(
                reply=text,
                resolution_source="LLM_CACHE" if hit else "LIVE_INFERENCE",
                model=self.settings.model,
                input_tokens=int(usage.prompt_tokens),
                output_tokens=int(usage.completion_tokens),
                key_id=None if hit else key_id,
                fallback_attempts=0 if hit else attempts,
                timings=timings,
            )
        raise AllProvidersUnavailable(timings, reasons)

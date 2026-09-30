"""Single Gemini client used everywhere.

Concurrency limit, retries with exponential backoff on 429/5xx, Redis response cache,
per-call logging to `llm_calls`, structured output validated against Pydantic models.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, overload

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.config import AppConfig, ConfigError, get_config, require_env
from core.ratelimit import RateLimiter

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
CACHE_PREFIX = "llmcache:v2:"  # v2: fallback answers are no longer cached under the requested model
DEFAULT_THINKING_LEVEL = "low"  # spec 6.7
_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


class LLMError(RuntimeError):
    pass


class LLMInvalidResponse(LLMError):
    """Structured call returned JSON that failed validation twice."""


class ModelUnavailableError(ConfigError):
    pass


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    text: str
    parsed: T | None
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int
    cache_hit: bool


@dataclass(frozen=True)
class CallRecord:
    run_id: uuid.UUID | None
    purpose: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: int
    cache_hit: bool


CallLogger = Callable[[CallRecord], Awaitable[None]]


def db_call_logger(sessionmaker: async_sessionmaker[AsyncSession]) -> CallLogger:
    async def _log(rec: CallRecord) -> None:
        async with sessionmaker() as s:
            await s.execute(
                text(
                    "INSERT INTO llm_calls (run_id, purpose, model, input_tokens, output_tokens,"
                    " cost_usd, latency_ms, cache_hit)"
                    " VALUES (:run_id, :purpose, :model, :input_tokens, :output_tokens,"
                    " :cost_usd, :latency_ms, :cache_hit)"
                ),
                rec.__dict__,
            )
            await s.commit()

    return _log


def _strip_prefix(name: str) -> str:
    return name.removeprefix("models/")


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.APIError):
        return exc.code == 429 or (exc.code or 0) >= 500
    return isinstance(exc, httpx.TransportError | asyncio.TimeoutError)


def _server_retry_delay(exc: BaseException) -> float | None:
    """The retry delay a 429/503 response asks for (google.rpc.RetryInfo "retryDelay": "7s"), if any."""
    details = getattr(exc, "details", None)
    items = details.get("error", {}).get("details", []) if isinstance(details, dict) else []
    for item in items if isinstance(items, list) else []:
        m = _RETRY_DELAY.match(str(item.get("retryDelay", ""))) if isinstance(item, dict) else None
        if m:
            return float(m.group(1))
    return None


def _is_overloaded(exc: BaseException) -> bool:
    return isinstance(exc, genai_errors.APIError) and exc.code == 503


def _is_daily_quota(exc: BaseException) -> bool:
    """429 because a per-day quota is used up (retrying within the day cannot succeed)."""
    return isinstance(exc, genai_errors.APIError) and exc.code == 429 and "PerDay" in str(exc)


_QUOTA_VALUE = re.compile(r"quotaValue['\"]?\s*[:=]\s*['\"]?(\d+)")


def quota_value(exc: BaseException) -> int | None:
    """The limit Google reports in a 429 (e.g. the requests-per-day quota), if the error carries it."""
    m = _QUOTA_VALUE.search(str(exc))
    return int(m.group(1)) if m else None


def _is_thinking_rejection(exc: BaseException) -> bool:
    return isinstance(exc, genai_errors.ClientError) and exc.code == 400 and "thinking" in str(exc).lower()


class LLMClient:
    def __init__(
        self,
        cfg: AppConfig,
        *,
        client: genai.Client | None = None,
        redis: Redis | None = None,
        call_logger: CallLogger | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cfg = cfg
        self._client = client or genai.Client(api_key=require_env("GEMINI_API_KEY"))
        self._redis = redis
        self._call_logger = call_logger
        self._sleep = sleep
        self._sem = asyncio.Semaphore(cfg.llm_client.max_concurrency)
        self.limiter = RateLimiter(cfg.llm_client.rpm_limits, redis, sleep=sleep)
        self._no_thinking: set[str] = set()
        self._warned_no_price: set[str] = set()

    # ---- startup check ----------------------------------------------------

    def configured_models(self) -> list[str]:
        return sorted({self.cfg.models.llm_main, self.cfg.models.llm_light, self.cfg.models.write_model})

    async def check_models(self) -> list[str]:
        """Call models.list() once; fail clearly if a configured model id is unavailable."""
        pager = await asyncio.to_thread(self._client.models.list)
        available = {_strip_prefix(m.name or "") for m in pager}
        missing = [m for m in self.configured_models() if m not in available]
        if missing:
            gemini = sorted(a for a in available if a.startswith("gemini"))
            raise ModelUnavailableError(
                f"Configured Gemini model(s) not available for this API key: {', '.join(missing)}. "
                f"Fix models.llm_main / models.llm_light in config.yaml. "
                f"Available gemini models: {', '.join(gemini) or '(none)'}"
            )
        log.info("llm models available", extra={"models": self.configured_models()})
        return self.configured_models()

    # ---- calls --------------------------------------------------------------

    @overload
    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        purpose: str,
        schema: type[T],
        run_id: uuid.UUID | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        thinking_level: str | None = DEFAULT_THINKING_LEVEL,
    ) -> LLMResult[T]: ...

    @overload
    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        purpose: str,
        schema: None = None,
        run_id: uuid.UUID | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        thinking_level: str | None = DEFAULT_THINKING_LEVEL,
    ) -> LLMResult[Any]: ...

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        purpose: str,
        schema: type[T] | None = None,
        run_id: uuid.UUID | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
        thinking_level: str | None = DEFAULT_THINKING_LEVEL,
    ) -> LLMResult[Any]:
        params: dict[str, Any] = {
            "temperature": temperature, "max_output_tokens": max_output_tokens, "thinking_level": thinking_level,
        }  # fmt: skip
        if schema is not None:
            params["schema"] = schema.model_json_schema()
        key = self._cache_key(model, prompt, params)

        cached = await self._cache_get(key)
        if cached is not None:
            parsed = schema.model_validate_json(cached) if schema is not None else None
            result: LLMResult[Any] = LLMResult(
                cached, parsed, model, 0, 0, 0.0, 0, True
            )  # cached only if model answered
            await self._record(run_id, purpose, result)
            return result

        attempts = 2 if schema is not None else 1
        for attempt in range(attempts):
            result = await self._call(prompt, model, temperature, max_output_tokens, schema, thinking_level)
            await self._record(run_id, purpose, result)
            if schema is None:
                if result.model == model:  # never cache a fallback's answer under the requested model
                    await self._cache_set(key, result.text)
                return result
            try:
                parsed = schema.model_validate_json(result.text)
            except ValidationError as e:
                log.warning(
                    "llm invalid json",
                    extra={"model": model, "purpose": purpose, "attempt": attempt + 1, "error": str(e)[:500]},
                )
                continue
            if result.model == model:
                await self._cache_set(key, result.text)
            return LLMResult(
                result.text,
                parsed,
                result.model,
                result.input_tokens,
                result.output_tokens,
                result.cost_usd,
                result.latency_ms,
                False,
            )
        raise LLMInvalidResponse(f"{purpose}: {model} returned invalid JSON twice")

    async def _call(
        self,
        prompt: str,
        model: str,
        temperature: float,
        max_output_tokens: int,
        schema: type[BaseModel] | None,
        thinking_level: str | None = DEFAULT_THINKING_LEVEL,
    ) -> LLMResult[Any]:
        lc = self.cfg.llm_client
        retry = 0
        overloaded = 0
        while True:
            config = self._build_config(model, temperature, max_output_tokens, schema, thinking_level)
            waited = await self.limiter.acquire(model)  # stay under the free-tier requests per minute
            if waited > 1:
                log.info("llm rate limit wait", extra={"model": model, "waited_s": round(waited, 1)})
            start = time.perf_counter()
            try:
                async with self._sem:
                    resp = await self._client.aio.models.generate_content(model=model, contents=prompt, config=config)
            except Exception as e:
                if model not in self._no_thinking and _is_thinking_rejection(e):
                    self._no_thinking.add(model)
                    log.warning("thinking_level rejected, dropping thinking_config", extra={"model": model})
                    continue
                overloaded += _is_overloaded(e)
                fallback = self.cfg.models.llm_light
                exhausted = _is_daily_quota(e)
                if exhausted:
                    log.warning(
                        "llm daily quota exhausted",
                        extra={"model": model, "quota_per_day": quota_value(e),
                               "requests_counted_today": await self.limiter.used_today(model)},
                    )  # fmt: skip
                if (
                    overloaded > lc.fallback_after_503_retries or exhausted
                ) and model == self.cfg.models.llm_main != fallback:
                    # the main model keeps answering 503 "high demand": finish this call on the light model
                    log.warning(
                        "llm model unavailable, falling back for this call",
                        extra={
                            "model": model,
                            "fallback": fallback,
                            "after_503s": overloaded,
                            "daily_quota": exhausted,
                        },
                    )
                    model, overloaded = fallback, 0
                    continue
                if _is_retryable(e) and retry < lc.retries:
                    delay = min(lc.backoff_max_s, lc.backoff_base_s * 2**retry) * (0.5 + random.random() / 2)
                    server_delay = _server_retry_delay(e)
                    if server_delay is not None:
                        delay = max(delay, min(server_delay, lc.backoff_max_s * 4))
                    retry += 1
                    log.warning(
                        "llm call failed, retrying",
                        extra={"model": model, "retry": retry, "delay_s": round(delay, 2), "error": str(e)[:300]},
                    )
                    await self._sleep(delay)
                    continue
                raise LLMError(f"{model} call failed: {e}") from e
            latency_ms = int((time.perf_counter() - start) * 1000)
            usage = resp.usage_metadata
            input_tokens = (usage.prompt_token_count or 0) if usage else 0
            output_tokens = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0) if usage else 0
            return LLMResult(
                resp.text or "",
                None,
                model,
                input_tokens,
                output_tokens,
                self._cost(model, input_tokens, output_tokens),
                latency_ms,
                False,
            )

    def _build_config(
        self,
        model: str,
        temperature: float,
        max_output_tokens: int,
        schema: type[BaseModel] | None,
        thinking_level: str | None = DEFAULT_THINKING_LEVEL,
    ) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {"temperature": temperature, "max_output_tokens": max_output_tokens}
        if schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = schema
        if thinking_level and model not in self._no_thinking:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
        return types.GenerateContentConfig(**kwargs)

    def _cost(self, model: str, input_tokens: int, output_tokens: int) -> float | None:
        price = self.cfg.llm_prices.get(model)
        if price is None or price.input is None or price.output is None:
            if model not in self._warned_no_price:
                self._warned_no_price.add(model)
                log.warning("no price configured for model, cost logged as null", extra={"model": model})
            return None
        return (input_tokens * price.input + output_tokens * price.output) / 1_000_000

    # ---- cache & logging ------------------------------------------------------

    @staticmethod
    def _cache_key(model: str, prompt: str, params: dict[str, Any]) -> str:
        blob = json.dumps({"model": model, "prompt": prompt, "params": params}, sort_keys=True)
        return CACHE_PREFIX + hashlib.sha256(blob.encode()).hexdigest()

    async def _cache_get(self, key: str) -> str | None:
        if self._redis is None:
            return None
        try:
            value = await self._redis.get(key)
        except Exception:
            log.exception("llm cache read failed")
            return None
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def _cache_set(self, key: str, value: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(key, value, ex=self.cfg.llm_client.cache_ttl_days * 86400)
        except Exception:
            log.exception("llm cache write failed")

    async def _record(self, run_id: uuid.UUID | None, purpose: str, r: LLMResult[Any]) -> None:
        rec = CallRecord(
            run_id, purpose, r.model, r.input_tokens, r.output_tokens, r.cost_usd, r.latency_ms, r.cache_hit
        )
        log.info("llm call", extra={k: v for k, v in rec.__dict__.items() if k != "run_id"})
        if self._call_logger is None:
            return
        try:
            await self._call_logger(rec)
        except Exception:
            log.exception("failed to log llm call")


async def _check_cli() -> int:
    from core.logging import setup_logging

    setup_logging()
    try:
        await LLMClient(get_config()).check_models()
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("OK: configured Gemini models are available")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]:
        print("usage: python -m core.llm check", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_check_cli()))

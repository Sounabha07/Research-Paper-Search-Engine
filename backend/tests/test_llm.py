from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from core.config import AppConfig, ModelPrice
from core.llm import CallRecord, LLMClient, LLMError, LLMInvalidResponse, ModelUnavailableError


class Answer(BaseModel):
    answer: str


def _resp(text: str, inp: int = 10, out: int = 5, thoughts: int | None = 2) -> Any:
    usage = SimpleNamespace(prompt_token_count=inp, candidates_token_count=out, thoughts_token_count=thoughts)
    return SimpleNamespace(text=text, usage_metadata=usage)


def _api_error(code: int, message: str) -> genai_errors.APIError:
    cls = genai_errors.ClientError if code < 500 else genai_errors.ServerError
    return cls(code, {"error": {"code": code, "message": message, "status": "X"}})


@dataclass
class FakeGenai:
    outcomes: list[Any]
    model_names: list[str] = field(default_factory=list)
    configs: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))
        self.models = SimpleNamespace(list=lambda: [SimpleNamespace(name=n) for n in self.model_names])

    async def _generate(self, *, model: str, contents: str, config: Any) -> Any:
        self.configs.append(config)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value


async def _no_sleep(_: float) -> None:
    return None


def _client(cfg: AppConfig, fake: FakeGenai, **kw: Any) -> tuple[LLMClient, list[CallRecord]]:
    records: list[CallRecord] = []

    async def logger(rec: CallRecord) -> None:
        records.append(rec)

    client = LLMClient(cfg, client=fake, call_logger=logger, sleep=_no_sleep, **kw)  # type: ignore[arg-type]
    return client, records


async def test_check_models_ok(cfg: AppConfig) -> None:
    fake = FakeGenai([], model_names=[f"models/{cfg.models.llm_main}", f"models/{cfg.models.llm_light}"])
    client, _ = _client(cfg, fake)
    assert await client.check_models() == sorted([cfg.models.llm_main, cfg.models.llm_light])


async def test_check_models_missing_lists_available(cfg: AppConfig) -> None:
    fake = FakeGenai([], model_names=["models/gemini-other", f"models/{cfg.models.llm_light}"])
    client, _ = _client(cfg, fake)
    with pytest.raises(ModelUnavailableError, match=cfg.models.llm_main) as ei:
        await client.check_models()
    assert "gemini-other" in str(ei.value)


async def test_plain_call_logs_and_caches(cfg: AppConfig) -> None:
    fake = FakeGenai([_resp("hello")])
    client, records = _client(cfg, fake, redis=FakeRedis())
    r1 = await client.generate("hi", model="m", purpose="test")
    r2 = await client.generate("hi", model="m", purpose="test")
    assert r1.text == r2.text == "hello"
    assert (r1.cache_hit, r2.cache_hit) == (False, True)
    assert r1.input_tokens == 10 and r1.output_tokens == 7  # candidates + thoughts
    assert [r.cache_hit for r in records] == [False, True]
    assert len(fake.configs) == 1


async def test_retries_on_429_and_5xx_then_succeeds(cfg: AppConfig) -> None:
    fake = FakeGenai([_api_error(429, "quota"), _api_error(503, "busy"), _resp("ok")])
    client, records = _client(cfg, fake)
    assert (await client.generate("p", model="m", purpose="t")).text == "ok"
    assert len(records) == 1


async def test_gives_up_after_configured_retries(cfg: AppConfig) -> None:
    fake = FakeGenai([_api_error(500, "boom")] * (cfg.llm_client.retries + 1))
    client, _ = _client(cfg, fake)
    with pytest.raises(LLMError):
        await client.generate("p", model="m", purpose="t")
    assert fake.outcomes == []


async def test_non_retryable_error_raises_immediately(cfg: AppConfig) -> None:
    fake = FakeGenai([_api_error(403, "denied"), _resp("never")])
    client, _ = _client(cfg, fake)
    with pytest.raises(LLMError):
        await client.generate("p", model="m", purpose="t")
    assert len(fake.outcomes) == 1


async def test_thinking_rejection_drops_config_for_model(cfg: AppConfig) -> None:
    rejection = _api_error(400, "thinking_level is not supported for this model")
    fake = FakeGenai([rejection, _resp("a"), _resp("b")])
    client, _ = _client(cfg, fake)
    await client.generate("p1", model="m", purpose="t")
    await client.generate("p2", model="m", purpose="t")
    assert fake.configs[0].thinking_config is not None
    assert fake.configs[1].thinking_config is None
    assert fake.configs[2].thinking_config is None


async def test_structured_retries_once_on_invalid_json(cfg: AppConfig) -> None:
    fake = FakeGenai([_resp("not json"), _resp('{"answer": "42"}')])
    client, records = _client(cfg, fake, redis=FakeRedis())
    r = await client.generate("q", model="m", purpose="t", schema=Answer)
    assert r.parsed == Answer(answer="42")
    assert fake.configs[0].response_mime_type == "application/json"
    assert len(records) == 2
    cached = await client.generate("q", model="m", purpose="t", schema=Answer)
    assert cached.cache_hit
    assert cached.parsed == Answer(answer="42")


async def test_structured_fails_after_second_invalid(cfg: AppConfig) -> None:
    fake = FakeGenai([_resp("{}"), _resp("nope")])
    client, _ = _client(cfg, fake, redis=FakeRedis())
    with pytest.raises(LLMInvalidResponse):
        await client.generate("q", model="m", purpose="t", schema=Answer)


async def test_cost_from_price_table(cfg: AppConfig) -> None:
    priced = cfg.model_copy(update={"llm_prices": {"m": ModelPrice(input=1.0, output=10.0)}})
    fake = FakeGenai([_resp("x", inp=1_000_000, out=100_000, thoughts=0), _resp("y")])
    client, _ = _client(priced, fake)
    assert (await client.generate("p", model="m", purpose="t")).cost_usd == pytest.approx(2.0)
    assert (await client.generate("p", model="unpriced", purpose="t")).cost_usd is None


async def test_log_failure_does_not_break_call(cfg: AppConfig) -> None:
    async def broken(_: CallRecord) -> None:
        raise RuntimeError("db down")

    fake = FakeGenai([_resp("ok")])
    client = LLMClient(cfg, client=fake, call_logger=broken, sleep=_no_sleep)  # type: ignore[arg-type]
    assert (await client.generate("p", model="m", purpose="t")).text == "ok"

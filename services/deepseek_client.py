"""Единственное место, где вызывается API DeepSeek."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import openai
from openai import AsyncOpenAI

from config import Config

log = logging.getLogger("deepseek")


class DeepSeekError(Exception):
    """kind: 'temporary' (повторы исчерпаны) | 'fatal' (ключ/баланс/модель) | 'length' (лимит токенов)."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(f"{kind} status={status}")
        self.kind = kind
        self.status = status


@dataclass
class Completion:
    content: str
    model: str
    seconds: float


class DeepSeekClient:
    def __init__(self, cfg: Config, system_prompt: str, client: AsyncOpenAI | None = None, backoff: float = 2.0):
        self.cfg = cfg
        self.system_prompt = system_prompt  # без изменений — префикс кэшируется
        self.backoff = backoff
        self.client = client or AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url,
                                            timeout=cfg.timeout, max_retries=0)

    async def analyze(self, notes: str, model: str) -> Completion:
        kwargs: dict = dict(model=model, max_tokens=self.cfg.max_tokens,
                            messages=[{"role": "system", "content": self.system_prompt},
                                      {"role": "user", "content": notes}])
        if self.cfg.temperature is not None:
            kwargs["temperature"] = self.cfg.temperature
        attempts = max(1, self.cfg.max_retries)
        for i in range(attempts):
            t0 = time.monotonic()
            status = None
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError) as e:
                status = getattr(e, "status_code", None)
            except openai.APIStatusError as e:
                if e.status_code < 500:  # 400/401/402/404 — повтор не поможет
                    log.error("deepseek fatal status=%s model=%s", e.status_code, model)
                    raise DeepSeekError("fatal", e.status_code) from None
                status = e.status_code
            else:
                dur = time.monotonic() - t0
                self._log_metrics(resp, model, dur)
                text = (resp.choices[0].message.content or "").strip()
                if not text:
                    # у «думающих» моделей рассуждение входит в max_tokens и может съесть весь лимит
                    reason = resp.choices[0].finish_reason
                    log.error("deepseek empty content finish_reason=%s model=%s", reason, model)
                    raise DeepSeekError("length" if reason == "length" else "temporary")
                return Completion(text, model, dur)
            log.warning("deepseek temporary error status=%s attempt=%d/%d", status, i + 1, attempts)
            if i < attempts - 1:
                await asyncio.sleep(self.backoff * 2 ** i)
        raise DeepSeekError("temporary")

    def _log_metrics(self, resp, model: str, dur: float) -> None:
        u = getattr(resp, "usage", None)
        log.info("deepseek ok model=%s prompt=%s dur=%.1fs cache_hit=%s cache_miss=%s out=%s",
                 model, self.cfg.prompt_version, dur,
                 getattr(u, "prompt_cache_hit_tokens", None), getattr(u, "prompt_cache_miss_tokens", None),
                 getattr(u, "completion_tokens", None))
        if self.cfg.debug_reasoning:
            rc = getattr(resp.choices[0].message, "reasoning_content", None)
            if rc:
                log.debug("reasoning_content: %s", rc)

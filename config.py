"""Чтение настроек из окружения. Названия моделей и параметры — только здесь."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(x) for x in re.split(r"[,\s]+", raw.strip()) if x)


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_key: str
    allowed_user_ids: frozenset[int]
    base_url: str
    model_fast: str
    model_deep: str
    temperature: float | None
    max_tokens: int
    timeout: float
    max_retries: int
    db_path: str
    prompt_path: Path
    debug_reasoning: bool

    @property
    def prompt_version(self) -> str:
        m = re.search(r"v\d+(?:\.\d+)*", self.prompt_path.name)
        return m.group(0) if m else "unknown"

    def load_prompt(self) -> str:
        # Промпт читается как есть, без правок и пересказа.
        return self.prompt_path.read_text(encoding="utf-8")


def load_config(env: dict[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    missing = [k for k in ("BOT_TOKEN", "DEEPSEEK_API_KEY", "ALLOWED_USER_IDS", "MODEL_FAST", "MODEL_DEEP") if not e.get(k)]
    if missing:
        raise RuntimeError(f"Не заданы переменные окружения: {', '.join(missing)}")
    temp = e.get("TEMPERATURE", "0.25").strip()
    prompt = Path(e.get("PROMPT_PATH", "prompts/demo_video_audit_v1.0.md"))
    if not prompt.is_absolute():
        prompt = BASE_DIR / prompt
    return Config(
        bot_token=e["BOT_TOKEN"],
        api_key=e["DEEPSEEK_API_KEY"],
        allowed_user_ids=_ids(e["ALLOWED_USER_IDS"]),
        base_url=e.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        model_fast=e["MODEL_FAST"],
        model_deep=e["MODEL_DEEP"],
        temperature=float(temp) if temp else None,  # пусто = не передавать (для «думающих» моделей)
        max_tokens=int(e.get("MAX_TOKENS", "32000")),
        timeout=float(e.get("REQUEST_TIMEOUT", "120")),
        max_retries=int(e.get("MAX_RETRIES", "3")),
        db_path=e.get("DB_PATH", "./data/audit.db"),
        prompt_path=prompt,
        debug_reasoning=e.get("DEBUG_REASONING", "0") == "1",
    )

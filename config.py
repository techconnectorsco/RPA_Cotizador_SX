"""Configuracion central. Lee .env y expone las variables tipadas."""

from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # IA
    ia_provider: str = os.getenv("IA_PROVIDER", "gemini").lower()

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    # Scraper
    scraper_headless: bool = os.getenv("SCRAPER_HEADLESS", "false").lower() == "true"
    scraper_timeout_ms: int = int(os.getenv("SCRAPER_TIMEOUT_MS", "30000"))
    scraper_max_resultados: int = int(os.getenv("SCRAPER_MAX_RESULTADOS", "20"))


config = Config()

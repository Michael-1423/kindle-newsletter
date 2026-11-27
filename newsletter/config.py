"""Configuration loading: YAML for content, .env for secrets and runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Source:
    name: str
    type: str = "rss"
    url: str | None = None
    query: str | None = None
    max_results: int = 8
    count: int = 6
    lookback_hours: int | None = None
    # higher weight = more likely to make the final cut when slots are scarce.
    # Use it to push the feeds that match the reader's actual interest to the top.
    weight: float = 1.0


@dataclass
class Section:
    id: str
    title: str
    kind: str
    words: int
    sources: list[Source] = field(default_factory=list)
    max_stories: int = 4
    focus: str = ""
    prompt: str = ""
    exclude: list[str] = field(default_factory=list)


@dataclass
class Settings:
    title: str
    subtitle: str
    author: str
    target_words: int
    timezone: str
    lookback_hours: int
    sections: list[Section]
    ollama_host: str
    ollama_model: str
    ollama_embed_model: str
    ollama_think: bool
    ollama_num_ctx: int
    ollama_compare_models: list[str]
    kindle_email: str | None
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_pass: str | None
    sender_name: str


def _source(raw: dict) -> Source:
    return Source(
        name=raw["name"],
        type=raw.get("type", "rss"),
        url=raw.get("url"),
        query=raw.get("query"),
        max_results=int(raw.get("max_results", 8)),
        count=int(raw.get("count", 6)),
        lookback_hours=int(raw["lookback_hours"]) if raw.get("lookback_hours") else None,
        weight=float(raw.get("weight", 1.0)),
    )


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or ROOT / ".env")

    path = config_path or ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    nl = raw.get("newsletter", {})
    sections = [
        Section(
            id=s["id"],
            title=s["title"],
            kind=s.get("kind", "news"),
            words=int(s.get("words", 500)),
            sources=[_source(x) for x in s.get("sources", [])],
            max_stories=int(s.get("max_stories", 4)),
            focus=s.get("focus", ""),
            prompt=s.get("prompt", ""),
            exclude=[str(x) for x in s.get("exclude", [])],
        )
        for s in raw.get("sections", [])
    ]

    return Settings(
        title=nl.get("title", "The Daily Signal"),
        subtitle=nl.get("subtitle", "Your interests. No agenda."),
        author=nl.get("author", "Personal Edition"),
        target_words=int(nl.get("target_words", 3300)),
        timezone=nl.get("timezone", "Asia/Kolkata"),
        lookback_hours=int(nl.get("lookback_hours", 36)),
        sections=sections,
        ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11435").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "gemma4:12b-mlx"),
        ollama_embed_model=os.getenv("OLLAMA_EMBED_MODEL", "qwen3-embedding:latest"),
        ollama_think=_env_bool("OLLAMA_THINK", False),
        ollama_num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        ollama_compare_models=[
            m.strip()
            for m in os.getenv("OLLAMA_COMPARE_MODELS", "llama3.1:8b").split(",")
            if m.strip()
        ],
        kindle_email=os.getenv("KINDLE_EMAIL") or None,
        smtp_host=os.getenv("SMTP_HOST") or None,
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER") or None,
        smtp_pass=os.getenv("SMTP_PASS") or None,
        sender_name=os.getenv("SENDER_NAME", "The Daily Signal"),
    )

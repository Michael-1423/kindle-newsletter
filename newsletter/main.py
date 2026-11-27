"""CLI entry point: python -m newsletter.main [--dry-run] [--no-send]"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from .compose import compose
from .config import ROOT, load_settings
from .deliver import send_to_kindle
from .epub import build_epub
from .neutralise import build_briefs
from .ollama import Ollama, OllamaError
from .sources import collect_section

log = logging.getLogger("newsletter")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "chardet", "feedparser"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run(
    dry_run: bool = False,
    no_send: bool = False,
    keep_raw: bool = False,
    model: str | None = None,
) -> int:
    settings = load_settings()
    ollama = Ollama(
        settings.ollama_host,
        model or settings.ollama_model,
        settings.ollama_embed_model,
        think=settings.ollama_think,
        num_ctx=settings.ollama_num_ctx,
    )
    today = datetime.now()
    stamp = today.strftime("%Y-%m-%d")

    log.info("building %s for %s", settings.title, stamp)

    section_material: dict[str, list] = {}
    for section in settings.sections:
        log.info("fetching %s", section.title)
        stories = collect_section(
            section,
            settings.lookback_hours,
            enrich=not dry_run,
            exclude=section.exclude,
        )
        if not stories:
            log.warning("  no stories found for %s", section.title)
            continue
        if section.kind == "news" and not dry_run:
            log.info("  neutralising %d candidates", len(stories))
            briefs = build_briefs(section, stories, ollama)
            log.info("  kept %d briefs (%s)", len(briefs), ", ".join(b.story.source for b in briefs))
            section_material[section.id] = briefs
        else:
            section_material[section.id] = stories

    if not section_material:
        log.error("nothing fetched; aborting")
        return 1

    if dry_run:
        for sid, items in section_material.items():
            log.info("dry-run %s: %d items", sid, len(items))
        return 0

    newsletter = compose(settings, section_material, ollama, today)
    if not newsletter.sections:
        log.error("composer produced no sections; aborting")
        return 1

    out_dir = ROOT / "output"
    epub_path = out_dir / f"daily-signal-{stamp}.epub"
    build_epub(newsletter, epub_path)
    text_path = out_dir / f"daily-signal-{stamp}.txt"
    text_path.write_text(newsletter.plain_text(), encoding="utf-8")

    log.info(
        "wrote %s (%d words, ~%d min)",
        epub_path.name,
        newsletter.word_count,
        max(1, newsletter.word_count // 220),
    )
    stats = ollama.stats()
    log.info(
        "llm: %d calls, %d out tokens, %s tok/s",
        stats["calls"],
        stats["output_tokens"],
        stats["tokens_per_second"],
    )

    if no_send:
        log.info("--no-send set; skipping Kindle delivery")
        return 0

    ok = send_to_kindle(
        settings,
        epub_path,
        subject=f"{settings.title} - {today.strftime('%a %d %b')}",
    )
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build and deliver your personal daily newsletter.")
    parser.add_argument("--dry-run", action="store_true", help="fetch only, no LLM, no EPUB")
    parser.add_argument("--no-send", action="store_true", help="build the EPUB but do not email it")
    parser.add_argument(
        "--compare",
        nargs="*",
        metavar="MODEL",
        help="A/B build with these models over identical material "
        "(default: config default plus any in OLLAMA_COMPARE_MODELS)",
    )
    parser.add_argument("--model", help="override the writer model for this run")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    try:
        if args.compare is not None:
            from .compare import compare

            settings = load_settings()
            models = [m for m in args.compare if m]
            if not models:
                models = [settings.ollama_model, *settings.ollama_compare_models]
            # de-duplicate while keeping order
            models = list(dict.fromkeys(models))
            if len(models) < 2:
                log.warning(
                    "only one model given; add more via --compare a b or OLLAMA_COMPARE_MODELS"
                )
            return compare(settings, models)
        return run(dry_run=args.dry_run, no_send=args.no_send, model=args.model)
    except OllamaError as exc:
        log.error("Ollama error: %s", exc)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

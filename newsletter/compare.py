"""A/B compare newsletter models over identical source material.

Fetching is the slow, shared part: the same stories are fed to every model so the
only variable is the model itself. Outputs land in output/<date>/<model>/ with a
comparison report alongside.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .compose import Newsletter, compose
from .config import ROOT, Settings
from .epub import build_epub
from .neutralise import build_briefs
from .ollama import Ollama, OllamaError
from .sources import Story, collect_section

log = logging.getLogger(__name__)


@dataclass
class ModelRun:
    model: str
    slug: str
    seconds: float = 0.0
    words: int = 0
    sections: dict[str, int] = field(default_factory=dict)
    briefs: dict[str, int] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    output_dir: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def gather_material(settings: Settings, enrich: bool = True) -> dict[str, list[Story]]:
    """Fetch every section once, so all models see byte-identical input."""
    material: dict[str, list[Story]] = {}
    for section in settings.sections:
        log.info("fetching %s", section.title)
        stories = collect_section(
            section,
            settings.lookback_hours,
            enrich=enrich,
            exclude=section.exclude,
        )
        if stories:
            material[section.id] = stories
        else:
            log.warning("  no stories for %s", section.title)
    return material


def run_model(
    settings: Settings,
    model: str,
    material: dict[str, list[Story]],
    today: datetime,
    out_root: Path,
) -> ModelRun:
    """Build one complete newsletter with a single model."""
    slug = _slug(model)
    run = ModelRun(model=model, slug=slug)
    out_dir = out_root / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    run.output_dir = str(out_dir)

    ollama = Ollama(
        settings.ollama_host,
        model,
        settings.ollama_embed_model,
        think=settings.ollama_think,
        num_ctx=settings.ollama_num_ctx,
    )

    started = time.time()
    try:
        section_material: dict[str, list] = {}
        for section in settings.sections:
            stories = material.get(section.id) or []
            if not stories:
                continue
            if section.kind == "news":
                briefs = build_briefs(section, stories, ollama)
                run.briefs[section.id] = len(briefs)
                section_material[section.id] = briefs
            else:
                section_material[section.id] = stories

        newsletter = compose(settings, section_material, ollama, today)
        if not newsletter.sections:
            raise OllamaError("composer produced no sections")
    except (OllamaError, Exception) as exc:  # noqa: BLE001 - one model must not kill the comparison
        run.error = f"{type(exc).__name__}: {exc}"
        run.seconds = round(time.time() - started, 1)
        run.stats = ollama.stats()
        log.error("%s failed: %s", model, run.error)
        return run

    run.seconds = round(time.time() - started, 1)
    run.words = newsletter.word_count
    run.stats = ollama.stats()
    run.sections = {d.title: _words(d.body) for d in newsletter.sections}

    epub_path = out_dir / f"daily-signal-{today:%Y-%m-%d}.epub"
    build_epub(newsletter, epub_path)
    (out_dir / f"daily-signal-{today:%Y-%m-%d}.txt").write_text(
        newsletter.plain_text(), encoding="utf-8"
    )
    (out_dir / "run.json").write_text(
        json.dumps(asdict(run), indent=2), encoding="utf-8"
    )
    log.info(
        "%s done: %d words in %.0fs (%.1f tok/s)",
        model,
        run.words,
        run.seconds,
        run.stats.get("tokens_per_second", 0),
    )
    return run


def _words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _table(runs: list[ModelRun]) -> str:
    lines = [
        "| Model | Status | Time | Words | ~min | tok/s | LLM calls | Out tokens |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        status = "ok" if r.ok else "**FAILED**"
        lines.append(
            f"| `{r.model}` | {status} | {r.seconds:.0f}s | {r.words} | "
            f"{max(1, r.words // 220)} | {r.stats.get('tokens_per_second', 0)} | "
            f"{r.stats.get('calls', 0)} | {r.stats.get('output_tokens', 0)} |"
        )
    return "\n".join(lines)


def write_report(runs: list[ModelRun], today: datetime, out_root: Path) -> Path:
    parts = [
        f"# Model comparison - {today:%A, %d %B %Y}",
        "",
        "Identical source material was fed to each model. Only the model varies.",
        "",
        _table(runs),
        "",
    ]

    ok = [r for r in runs if r.ok]
    if ok:
        best_speed = max(ok, key=lambda r: r.stats.get("tokens_per_second", 0))
        best_length = max(ok, key=lambda r: r.words)
        parts += [
            "## Headline",
            "",
            f"- Fastest: `{best_speed.model}` "
            f"({best_speed.stats.get('tokens_per_second', 0)} tok/s, {best_speed.seconds:.0f}s total)",
            f"- Longest: `{best_length.model}` ({best_length.words} words)",
            "",
            "## Words per section",
            "",
        ]
        titles = list(ok[0].sections)
        parts.append("| Section | " + " | ".join(f"`{r.model}`" for r in ok) + " |")
        parts.append("|---" * (len(ok) + 1) + "|")
        for title in titles:
            cells = " | ".join(str(r.sections.get(title, "-")) for r in ok)
            parts.append(f"| {title} | {cells} |")
        parts.append("")

    failed = [r for r in runs if not r.ok]
    if failed:
        parts += ["## Failures", ""]
        parts += [f"- `{r.model}`: {r.error}" for r in failed]
        parts.append("")

    parts += [
        "## How to judge",
        "",
        "Read the `.txt` file from each model folder side by side. Look for:",
        "",
        "1. **Fabrication** - any fact, number or name not in the source material.",
        "2. **Length** - did it approach the per-section target, or stall short?",
        "3. **Structure** - does each section read as one story, or a list of items?",
        "4. **Neutrality** - editorial adjectives, unattributed motive, spin.",
        "5. **Repetition** - the same sentence or idea twice within a section.",
        "",
        "Keep the folders from several days before deciding; a single run is noisy.",
        "",
    ]

    path = out_root / "comparison.md"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def compare(
    settings: Settings,
    models: list[str],
    today: datetime | None = None,
    out_root: Path | None = None,
) -> int:
    today = today or datetime.now()
    out_root = out_root or ROOT / "output" / f"{today:%Y-%m-%d}"
    out_root.mkdir(parents=True, exist_ok=True)

    log.info("fetching shared material once for %d model(s)", len(models))
    material = gather_material(settings)
    if not material:
        log.error("nothing fetched; aborting")
        return 1

    runs: list[ModelRun] = []
    for model in models:
        log.info("=== building with %s ===", model)
        runs.append(run_model(settings, model, material, today, out_root))

    report = write_report(runs, today, out_root)
    log.info("comparison written to %s", report)

    for r in runs:
        if r.ok:
            log.info("  %-22s %4d words  %5.0fs  %s", r.model, r.words, r.seconds, r.output_dir)
        else:
            log.info("  %-22s FAILED: %s", r.model, r.error)
    return 0 if any(r.ok for r in runs) else 1

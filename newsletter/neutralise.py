"""The impartiality layer.

Three passes, cheapest first:

1. Source balance  - cap how many stories one outlet can contribute.
2. Corroboration   - cluster stories covering the same event across outlets so the
                     composer knows whether a claim is multi-source or single-source.
3. Neutral rewrite - an LLM pass that strips loaded language, editorial framing and
                     unattributed motive, and returns structured facts + claims.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Section
from .ollama import Ollama, OllamaError
from .sources import Story, _similar

log = logging.getLogger(__name__)

NEUTRAL_SYSTEM = """You are a wire-service editor. Your only job is to remove spin.

Rules:
- Report only what is verifiable. Strip adjectives, value judgements and emotive verbs.
- Remove editorial framing ("critics say", "in a stunning move", "amid growing fears").
- Do not attribute motives unless a named source states them.
- Keep every number, name, date and place exactly as given.
- If the text asserts something without a source, mark it as a claim, not a fact.
- Never add information that is not in the input.
- Write in plain, flat, declarative sentences."""

NEUTRAL_PROMPT = """Rewrite this news item as a neutral wire brief.

Return JSON only, with this exact shape:
{{
  "headline": "neutral headline, max 14 words",
  "summary": "2-4 flat sentences of what happened",
  "facts": ["verifiable fact", "..."],
  "claims": ["assertion that lacks a named source", "..."],
  "loaded_terms_removed": ["term", "..."],
  "framing_removed": "one sentence on what spin was removed"
}}

SOURCE: {source}
TITLE: {title}
TEXT:
{text}
"""


BATCH_PROMPT = """Rewrite EACH numbered news item below as a neutral wire brief.

Return JSON only: an object with a single key "briefs" holding a list. One entry per
item, in the same order, with this exact shape:
{{
  "briefs": [
    {{
      "index": 1,
      "headline": "neutral headline, max 14 words",
      "summary": "2-4 flat sentences of what happened",
      "facts": ["verifiable fact", "..."],
      "claims": ["assertion that lacks a named source", "..."],
      "loaded_terms_removed": ["term", "..."],
      "framing_removed": "one sentence on what spin was removed"
    }}
  ]
}}

Every item must appear exactly once. Never merge items, never skip an item, never
invent an index.

ITEMS:
{items}
"""


@dataclass
class Brief:
    story: Story
    headline: str
    summary: str
    facts: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    loaded_terms: list[str] = field(default_factory=list)
    framing_removed: str = ""
    corroborating_sources: list[str] = field(default_factory=list)
    cluster_size: int = 1

    @property
    def corroborated(self) -> bool:
        return self.cluster_size > 1

    def as_prompt_block(self) -> str:
        facts = "\n".join(f"  - {f}" for f in self.facts[:8]) or "  - (none extracted)"
        claims = "\n".join(f"  - {c}" for c in self.claims[:4])
        status = (
            f"corroborated by {self.cluster_size} outlets ({', '.join(self.corroborating_sources)})"
            if self.corroborated
            else "SINGLE SOURCE - hedge appropriately"
        )
        block = (
            f"### {self.headline}\n"
            f"Status: {status}\n"
            f"Summary: {self.summary}\n"
            f"Verified facts:\n{facts}\n"
        )
        if claims:
            block += f"Unattributed claims (do not present as fact):\n{claims}\n"
        block += f"Original source: {self.story.source} - {self.story.link}\n"
        return block


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _balance_sources(
    stories: list[Story],
    max_stories: int,
    weights: dict[str, float] | None = None,
) -> list[Story]:
    """Round-robin across outlets, weighted, so no single feed dominates.

    Outlets with a weight below 1 are skipped on some passes, which is how a
    research feed outranks a deals feed when there are only four slots.
    """
    weights = weights or {}
    by_source: dict[str, list[Story]] = {}
    for story in stories:
        by_source.setdefault(story.source, []).append(story)

    picked: list[Story] = []
    carry: dict[str, float] = {}
    while len(picked) < max_stories and any(by_source.values()):
        eligible = False
        for source in list(by_source):
            if len(picked) >= max_stories:
                break
            if not by_source[source]:
                del by_source[source]
                continue
            carry[source] = carry.get(source, 0.0) + weights.get(source, 1.0)
            if carry[source] < 1.0:
                # not this outlet's turn yet
                continue
            carry[source] -= 1.0
            eligible = True
            picked.append(by_source[source].pop(0))
        if not eligible:
            # nothing cleared the weight threshold; let everything through
            weights = {}
    return picked


def _cluster(stories: list[Story], ollama: Ollama | None = None, threshold: float = 0.82) -> dict[str, list[Story]]:
    """Group stories that describe the same event.

    Uses title-token similarity by default. Embeddings are opt-in: loading a second
    model next to the writer model costs ~5 GB of VRAM and thrashes memory on a
    16 GB Mac, so the cheap path is the default.
    """
    if len(stories) < 2:
        return {s.id: [s] for s in stories}

    vectors: list[list[float]] = []
    if ollama is not None:
        vectors = ollama.embed([f"{s.title}. {s.summary[:400]}" for s in stories])
        if len(vectors) != len(stories):
            vectors = []

    clusters: dict[str, list[Story]] = {}
    assigned: set[str] = set()

    for i, story in enumerate(stories):
        if story.id in assigned:
            continue
        group = [story]
        assigned.add(story.id)
        for j in range(i + 1, len(stories)):
            if stories[j].id in assigned:
                continue
            same = _cosine(vectors[i], vectors[j]) >= threshold if vectors else _similar(story.title, stories[j].title)
            if same:
                group.append(stories[j])
                assigned.add(stories[j].id)
        clusters[story.id] = group

    return clusters


def _normalise_brief(data: dict, story: Story) -> dict:
    """Coerce one raw model object into our brief shape, falling back to the story."""
    if not isinstance(data, dict):
        raise ValueError("expected object")
    return {
        "headline": str(data.get("headline") or story.title).strip(),
        "summary": str(data.get("summary") or story.summary).strip(),
        "facts": [str(x) for x in (data.get("facts") or [])][:10],
        "claims": [str(x) for x in (data.get("claims") or [])][:6],
        "loaded_terms": [str(x) for x in (data.get("loaded_terms_removed") or [])][:12],
        "framing_removed": str(data.get("framing_removed") or "").strip(),
    }


def _fallback(story: Story) -> dict:
    return {
        "headline": story.title,
        "summary": story.summary[:600],
        "facts": [],
        "claims": [],
        "loaded_terms": [],
        "framing_removed": "",
    }


def _neutralise_story(story: Story, ollama: Ollama) -> dict:
    text = (story.body or story.summary)[:3500]
    prompt = NEUTRAL_PROMPT.format(source=story.source, title=story.title, text=text)
    try:
        data = ollama.json_generate(prompt, system=NEUTRAL_SYSTEM, temperature=0.2, num_predict=900)
        return _normalise_brief(data, story)
    except (OllamaError, ValueError, TypeError) as exc:
        log.warning("neutralise failed for %r (%s); using raw summary", story.title[:60], exc)
        return _fallback(story)


def _neutralise_batch(stories: list[Story], ollama: Ollama) -> list[dict]:
    """Neutralise several stories in one call.

    The model handles a handful of short items far better than it handles a long
    generation, so this is close to free per item and cuts wall-clock time by
    roughly 40% versus one call per story.
    """
    items = []
    for i, story in enumerate(stories, start=1):
        text = (story.body or story.summary)[:1800]
        items.append(f"[{i}] SOURCE: {story.source}\nTITLE: {story.title}\nTEXT: {text}\n")
    prompt = BATCH_PROMPT.format(items="\n".join(items))

    try:
        data = ollama.json_generate(prompt, system=NEUTRAL_SYSTEM, temperature=0.2, num_predict=400 * len(stories) + 300)
    except (OllamaError, ValueError, TypeError) as exc:
        log.warning("batch neutralise failed (%s); falling back to per-story", exc)
        return [_neutralise_story(s, ollama) for s in stories]

    raw = data.get("briefs") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        log.warning("batch neutralise returned no list; falling back to per-story")
        return [_neutralise_story(s, ollama) for s in stories]

    by_index: dict[int, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        by_index[idx] = entry

    results: list[dict] = []
    missing = 0
    for i, story in enumerate(stories, start=1):
        entry = by_index.get(i)
        if entry is None:
            missing += 1
            results.append(_fallback(story))
            continue
        try:
            results.append(_normalise_brief(entry, story))
        except (ValueError, TypeError):
            missing += 1
            results.append(_fallback(story))
    if missing:
        log.info("    batch covered %d/%d items; %d used raw fallback", len(stories) - missing, len(stories), missing)
    return results


def build_briefs(
    section: Section,
    stories: list[Story],
    ollama: Ollama,
    use_embeddings: bool = False,
    batch_size: int = 5,
) -> list[Brief]:
    """Turn raw stories into neutral, corroboration-aware briefs.

    Selection happens *before* the LLM pass so we only pay for the stories that
    actually get published (4 calls instead of 30).
    """
    if not stories:
        return []

    clusters = _cluster(stories, ollama if use_embeddings else None)

    # rank by corroboration first, then a mild recency tiebreak; the per-source
    # round-robin below is what actually keeps one outlet from dominating
    ranked = sorted(
        clusters.values(),
        key=lambda g: (
            len({s.source for s in g}),
            (g[0].published or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        ),
        reverse=True,
    )
    anchors = [g[0] for g in ranked]
    weights = {s.name: s.weight for s in section.sources}
    chosen = _balance_sources(anchors, section.max_stories, weights)
    chosen_ids = {s.id for s in chosen}

    # neutralise the selected stories in small batches rather than one call each
    ordered = [g[0] for g in ranked if g[0].id in chosen_ids]
    neutral_by_id: dict[str, dict] = {}
    for start in range(0, len(ordered), batch_size):
        chunk = ordered[start : start + batch_size]
        results = _neutralise_batch(chunk, ollama)
        for story, result in zip(chunk, results):
            neutral_by_id[story.id] = result

    briefs: list[Brief] = []
    for group in ranked:
        anchor = group[0]
        if anchor.id not in chosen_ids:
            continue
        neutral = neutral_by_id.get(anchor.id) or _fallback(anchor)
        others = [s.source for s in group[1:] if s.source != anchor.source]
        briefs.append(
            Brief(
                story=anchor,
                headline=neutral["headline"],
                summary=neutral["summary"],
                facts=neutral["facts"],
                claims=neutral["claims"],
                loaded_terms=neutral["loaded_terms"],
                framing_removed=neutral["framing_removed"],
                corroborating_sources=sorted(set(others)),
                cluster_size=len({s.source for s in group}),
            )
        )
    return briefs


def source_balance_report(briefs: list[Brief]) -> str:
    counts = Counter(b.story.source for b in briefs)
    return ", ".join(f"{name} x{n}" for name, n in counts.most_common())

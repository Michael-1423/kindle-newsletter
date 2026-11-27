"""Compose the newsletter from neutral briefs using the local LLM."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from .config import Section, Settings
from .neutralise import Brief, source_balance_report
from .ollama import Ollama, OllamaError
from .sources import Story

log = logging.getLogger(__name__)

WRITER_SYSTEM = """You are the editor of a personal daily newsletter for one reader who is
tired of partisan coverage. You write tight, factual, readable prose.

Hard rules:
- Never invent facts. Use only the supplied material.
- Present single-source claims as claims, not facts.
- No editorial adjectives, no outrage, no "amid growing fears" framing.
- Attribute contested claims to the outlet that made them.
- Plain language. Short paragraphs. No bullet-point dumps.
- British or American spelling, but be consistent within a section."""

NEWS_PROMPT = """Write the "{title}" section of today's newsletter.

Reader's interest: {focus}
Target length: about {words} words.
Today's date: {date}

You have {count} neutral briefs. Write a flowing section with a short intro line,
then one paragraph per story. For each story give the reader what happened and why
it matters, without spin. Where a brief is marked SINGLE SOURCE, hedge it.

GROUNDING RULES:
- Every name, number, date and place must appear in the briefs below.
- Do NOT add background, context or colour that is not in the briefs.
- If you do not know a detail, write around it. Never guess a name or a number.
- A shorter, accurate section is far better than a padded, invented one.

End the section with a one-line "Why it matters" takeaway.

BRIEFS:
{briefs}
"""

FEATURE_PROMPT = """Write the "{title}" section of today's newsletter.

Target length: about {words} words.
Today's date: {date}

Instruction: {instruction}

You have been given exactly ONE item below. Write about that item and nothing else.

GROUNDING RULES:
- Every name, number, date and place must appear in the material below.
- Do NOT add background, context or colour that is not in the material.
- If you do not know a detail, write around it. Never guess a name or a number.
- A shorter, accurate piece is far better than a padded, invented one.

MATERIAL:
{candidates}
"""

INTRO_PROMPT = """Write a 60-word opening note for today's personal newsletter.
It should feel like a calm morning briefing, not a newspaper editorial.
Mention the date ({date}) and hint at what is inside without listing everything.
No greeting cliches like "Good morning, dear reader". No sign-off.

Sections today: {sections}
"""


EXPAND_PROMPT = """Your draft below is too short. Rewrite it at the full target length.

Target: about {words} words. The draft is {actual} words.
Keep every fact and claim exactly as it stands. Add depth, not filler: more context
on what happened, how it fits what came before, what the numbers mean, and what to
watch next. Do not repeat yourself, do not pad, do not add facts that are not in the
briefs. No headings, no bullet points, no preamble - start straight into the prose.

BRIEFS:
{briefs}

DRAFT:
{draft}
"""


@dataclass
class SectionDraft:
    id: str
    title: str
    body: str
    sources: list[str] = field(default_factory=list)
    balance: str = ""


@dataclass
class Newsletter:
    title: str
    subtitle: str
    author: str
    date: datetime
    intro: str
    sections: list[SectionDraft]
    word_count: int = 0

    def plain_text(self) -> str:
        parts = [f"{self.title}\n{self.subtitle}\n{self.date.strftime('%A, %d %B %Y')}\n"]
        if self.intro:
            parts.append(self.intro + "\n")
        for section in self.sections:
            parts.append(f"\n## {section.title}\n\n{section.body}\n")
        return "\n".join(parts)


def _strip_markdown_noise(text: str, title: str | None = None) -> str:
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # the model often echoes the section title as a first line; the EPUB adds its own
    if title:
        lines = text.split("\n", 1)
        if len(lines) > 1 and lines[0].strip().strip(":").lower() == title.lower():
            text = lines[1].strip()
    return text.strip()


def _word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def _ensure_length(
    body: str,
    target_words: int,
    briefs: str,
    ollama: Ollama,
    temperature: float = 0.5,
    num_predict: int = 2600,
    floor: float = 0.8,
    passes: int = 2,
) -> str:
    """Models reliably undershoot a word target on the first pass. Ask again.

    A 12B model tends to plateau around 600 words per section, so this closes the
    gap rather than guaranteeing the target - and stops as soon as a pass stops
    helping.
    """
    actual = _word_count(body)
    for attempt in range(1, passes + 1):
        if target_words <= 0 or actual >= target_words * floor:
            break
        log.info("    draft was %d/%d words; expanding (pass %d)", actual, target_words, attempt)
        try:
            expanded = ollama.generate(
                EXPAND_PROMPT.format(words=target_words, actual=actual, briefs=briefs, draft=body),
                system=WRITER_SYSTEM,
                temperature=temperature,
                num_predict=num_predict,
            )
        except OllamaError as exc:
            log.warning("    expand failed: %s", exc)
            break
        grown = _word_count(expanded)
        if grown <= actual:
            break
        body, actual = expanded, grown
    return body


def _compose_news(section: Section, briefs: list[Brief], ollama: Ollama, date: datetime) -> SectionDraft:
    blocks = "\n".join(b.as_prompt_block() for b in briefs)
    prompt = NEWS_PROMPT.format(
        title=section.title,
        focus=section.focus or "general interest",
        words=section.words,
        date=date.strftime("%A, %d %B %Y"),
        count=len(briefs),
        briefs=blocks,
    )
    body = ollama.generate(prompt, system=WRITER_SYSTEM, temperature=0.5, num_predict=1800)
    body = _strip_markdown_noise(body, section.title)
    body = _ensure_length(body, section.words, blocks, ollama, temperature=0.5)
    body = _strip_markdown_noise(body, section.title)
    return SectionDraft(
        id=section.id,
        title=section.title,
        body=_strip_markdown_noise(body),
        sources=sorted({b.story.source for b in briefs}),
        balance=source_balance_report(briefs),
    )


def _compose_feature(section: Section, stories: list[Story], ollama: Ollama, date: datetime) -> SectionDraft:
    candidates = "\n".join(s.as_prompt_block(max_chars=2200) for s in stories)
    prompt = FEATURE_PROMPT.format(
        title=section.title,
        words=section.words,
        date=date.strftime("%A, %d %B %Y"),
        instruction=section.prompt or "Write an engaging short piece.",
        candidates=candidates,
    )
    body = ollama.generate(prompt, system=WRITER_SYSTEM, temperature=0.7, num_predict=1600)
    body = _strip_markdown_noise(body, section.title)
    body = _ensure_length(
        body, section.words, candidates, ollama, temperature=0.7, num_predict=2200
    )
    body = _strip_markdown_noise(body, section.title)
    return SectionDraft(
        id=section.id,
        title=section.title,
        body=_strip_markdown_noise(body),
        sources=sorted({s.source for s in stories}),
    )


def compose(
    settings: Settings,
    section_material: dict[str, list],
    ollama: Ollama,
    date: datetime | None = None,
) -> Newsletter:
    date = date or datetime.now()
    drafts: list[SectionDraft] = []

    for section in settings.sections:
        material = section_material.get(section.id) or []
        if not material:
            log.warning("no material for section %s; skipping", section.id)
            continue
        log.info("composing %s (%d items)", section.title, len(material))
        try:
            if section.kind == "news":
                draft = _compose_news(section, material, ollama, date)
            else:
                draft = _compose_feature(section, material, ollama, date)
            drafts.append(draft)
        except OllamaError as exc:
            log.error("compose failed for %s: %s", section.title, exc)

    try:
        intro = ollama.generate(
            INTRO_PROMPT.format(
                date=date.strftime("%A, %d %B %Y"),
                sections=", ".join(d.title for d in drafts),
            ),
            system=WRITER_SYSTEM,
            temperature=0.6,
            num_predict=200,
        )
        intro = _strip_markdown_noise(intro)
    except OllamaError as exc:
        log.warning("intro failed: %s", exc)
        intro = ""

    total = _word_count(intro) + sum(_word_count(d.body) for d in drafts)
    return Newsletter(
        title=settings.title,
        subtitle=settings.subtitle,
        author=settings.author,
        date=date,
        intro=intro,
        sections=drafts,
        word_count=total,
    )

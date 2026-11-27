"""Fetch raw material for each section from RSS, arXiv and Wikipedia."""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import Section, Source

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) PersonalNewsletter/1.0"
WIKI_API = "https://en.wikipedia.org/api/rest_v1"


@dataclass
class Story:
    title: str
    summary: str
    link: str
    source: str
    section_id: str
    published: datetime | None = None
    body: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.link}|{self.title}".encode()).hexdigest()[:12]

    def as_prompt_block(self, max_chars: int = 1800) -> str:
        text = self.body or self.summary
        text = _clean(text)[:max_chars]
        when = self.published.strftime("%Y-%m-%d %H:%M UTC") if self.published else "unknown"
        return (
            f"### {self.title}\n"
            f"Source: {self.source} | Published: {when}\n"
            f"URL: {self.link}\n"
            f"Content: {text}\n"
        )


def _clean(text: str) -> str:
    text = BeautifulSoup(text or "", "lxml").get_text(" ")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = getattr(entry, key, None)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _similar(a: str, b: str) -> bool:
    """Cheap near-duplicate check on title tokens."""
    ta, tb = set(_norm_title(a).split()), set(_norm_title(b).split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) > 0.6


def _get(url: str, timeout: int = 20, **kwargs) -> requests.Response:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp


def _parse_feed(url: str, timeout: int = 20):
    """Fetch with a hard timeout, then hand the bytes to feedparser."""
    return feedparser.parse(_get(url, timeout=timeout).content)


def _fetch_rss(source: Source, section: Section, cutoff: datetime) -> list[Story]:
    feed = _parse_feed(source.url)
    if feed.bozo and not feed.entries:
        log.warning("RSS failed for %s: %s", source.name, feed.bozo_exception)
        return []

    stories: list[Story] = []
    for entry in feed.entries[:40]:
        published = _parse_date(entry)
        if published and published < cutoff:
            continue
        summary = entry.get("summary") or entry.get("description") or ""
        if entry.get("content"):
            summary = " ".join(c.get("value", "") for c in entry["content"])
        stories.append(
            Story(
                title=_clean(entry.get("title", "")),
                summary=_clean(summary),
                link=entry.get("link", ""),
                source=source.name,
                section_id=section.id,
                published=published,
            )
        )
    return stories


def _fetch_arxiv(source: Source, section: Section, cutoff: datetime) -> list[Story]:
    query = source.query or "cat:cs.AI"
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={query}&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={source.max_results}"
    )
    feed = _parse_feed(url, timeout=40)
    stories: list[Story] = []
    for entry in feed.entries:
        published = _parse_date(entry)
        if published and published < cutoff:
            continue
        stories.append(
            Story(
                title=_clean(entry.get("title", "")),
                summary=_clean(entry.get("summary", "")),
                link=entry.get("link", ""),
                source=source.name,
                section_id=section.id,
                published=published,
                extra={"authors": [a.get("name") for a in entry.get("authors", [])][:4]},
            )
        )
    return stories


def _fetch_wikipedia_onthisday(source: Source, section: Section, cutoff: datetime) -> list[Story]:
    now = datetime.now(timezone.utc)
    url = f"{WIKI_API}/feed/onthisday/selected/{now.month}/{now.day}"
    resp = _get(url)
    stories: list[Story] = []
    for event in resp.json().get("selected", []):
        pages = event.get("pages") or []
        link = pages[0].get("content_urls", {}).get("desktop", {}).get("page", "") if pages else ""
        year = event.get("year", "")
        stories.append(
            Story(
                title=f"{year}: {_clean(event.get('text', ''))[:90]}",
                summary=_clean(event.get("text", "")),
                link=link or "https://en.wikipedia.org/wiki/Main_Page",
                source=source.name,
                section_id=section.id,
                extra={"year": year, "pages": [p.get("titles", {}).get("normalized") for p in pages]},
            )
        )
    return stories


def _fetch_wikipedia_random(source: Source, section: Section, cutoff: datetime) -> list[Story]:
    resp = _get(
        "https://en.wikipedia.org/w/api.php",
        params={
            "action": "query",
            "generator": "random",
            "grnnamespace": 0,
            "grnlimit": source.count,
            "prop": "extracts",
            "exintro": 1,
            "explaintext": 1,
            "format": "json",
        },
    )
    pages = (resp.json().get("query") or {}).get("pages") or {}
    stories: list[Story] = []
    for page in pages.values():
        extract = _clean(page.get("extract", ""))
        if len(extract) < 200:
            continue
        stories.append(
            Story(
                title=page.get("title", "Untitled"),
                summary=extract,
                link=f"https://en.wikipedia.org/wiki/{page.get('title','').replace(' ', '_')}",
                source=source.name,
                section_id=section.id,
            )
        )
    return stories


def _fetch_hn_algolia(source: Source, section: Section, cutoff: datetime) -> list[Story]:
    """Hacker News front page via the Algolia API (hnrss.org is often unreachable)."""
    resp = _get("https://hn.algolia.com/api/v1/search", params={"tags": "front_page", "hitsPerPage": 30})
    stories: list[Story] = []
    for hit in resp.json().get("hits", []):
        title = _clean(hit.get("title") or "")
        if not title:
            continue
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        created = hit.get("created_at_i")
        published = datetime.fromtimestamp(created, tz=timezone.utc) if created else None
        if published and published < cutoff:
            continue
        stories.append(
            Story(
                title=title,
                summary=_clean(hit.get("story_text") or "") or f"{hit.get('points', 0)} points on Hacker News",
                link=link,
                source=source.name,
                section_id=section.id,
                published=published,
            )
        )
    return stories


_FETCHERS = {
    "rss": _fetch_rss,
    "arxiv": _fetch_arxiv,
    "hn_algolia": _fetch_hn_algolia,
    "wikipedia_onthisday": _fetch_wikipedia_onthisday,
    "wikipedia_random": _fetch_wikipedia_random,
}


def _fetch_article_text(url: str, max_chars: int = 4000) -> str:
    """Best-effort article body extraction; returns '' on any failure."""
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "aside", "form", "header"]):
            tag.decompose()
        paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        text = " ".join(p for p in paragraphs if len(p) > 60)
        return _clean(text)[:max_chars]
    except Exception as exc:  # noqa: BLE001 - network best effort
        log.debug("article fetch failed for %s: %s", url, exc)
        return ""


def _excluded(story: Story, terms: list[str]) -> bool:
    haystack = f"{story.title} {story.summary[:300]}".lower()
    for term in terms:
        term = term.lower().strip()
        if not term:
            continue
        # word-boundary match so "sale" does not filter out "wholesale"
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", haystack):
            return True
    return False


_STALE_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


_DIGEST_MARKERS = (
    "the download",
    "daily briefing",
    "morning briefing",
    "news roundup",
    "weekly roundup",
    "live updates",
    "live blog",
    "what we know",
    "things to know",
    "our staff",
    "gift guide",
    "best deals",
)


def _is_digest(story: Story) -> bool:
    """Roundup/digest posts list a dozen unrelated items.

    They read like news in a feed but compose into a shapeless paragraph of
    one-line mentions, so drop them from news sections.
    """
    title = story.title.lower()
    return any(marker in title for marker in _DIGEST_MARKERS)


def _is_stale_retrospective(story: Story) -> bool:
    """Some feeds carry anniversary/retrospective pieces dated today.

    Deprioritise a story whose headline is anchored to a year well in the past -
    that is a 'this day in 2010' feature, not today's news.
    """
    years = [int(y) for y in _STALE_YEAR.findall(story.title)]
    if not years:
        return False
    return max(years) < datetime.now(timezone.utc).year - 1


def _diversify(stories: list[Story], per_source: int) -> list[Story]:
    """Take up to `per_source` stories from each outlet, newest first.

    Truncating a globally sorted list lets a feed that stamps every entry with the
    same recent timestamp (ESPN does this) crowd out every other outlet.
    """
    by_source: dict[str, list[Story]] = {}
    for story in stories:
        by_source.setdefault(story.source, []).append(story)

    picked: list[Story] = []
    for group in by_source.values():
        picked.extend(group[:per_source])
    picked.sort(
        key=lambda s: s.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return picked


def _pick_feature(stories: list[Story]) -> Story | None:
    """Choose the single best feature candidate deterministically.

    Prefer a story with enough substance to sustain a section, then the longest
    summary. Deterministic on purpose: the same day's material should always
    produce the same pick, so model comparisons stay fair.
    """
    if not stories:
        return None
    return max(stories, key=lambda s: (len(s.body or s.summary), len(s.summary)))


def collect_section(
    section: Section,
    lookback_hours: int,
    enrich: bool = True,
    exclude: list[str] | None = None,
) -> list[Story]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    stories: list[Story] = []

    for source in section.sources:
        fetcher = _FETCHERS.get(source.type)
        if not fetcher:
            log.warning("unknown source type %s for %s", source.type, source.name)
            continue
        try:
            source_cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=source.lookback_hours)
                if source.lookback_hours
                else cutoff
            )
            found = fetcher(source, section, source_cutoff)
            log.info("  %-22s %2d stories", source.name, len(found))
            stories.extend(found)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
            log.warning("  %-22s failed: %s", source.name, exc)

    # de-duplicate across sources
    unique: list[Story] = []
    for story in stories:
        if not story.title or not story.link:
            continue
        if any(_similar(story.title, u.title) or story.link == u.link for u in unique):
            continue
        unique.append(story)

    if exclude:
        before = len(unique)
        unique = [s for s in unique if not _excluded(s, exclude)]
        if before != len(unique):
            log.info("  filtered %d off-topic stories", before - len(unique))

    if section.kind == "news":
        before = len(unique)
        unique = [s for s in unique if not _is_digest(s)]
        if before != len(unique):
            log.info("  filtered %d digest/roundup posts", before - len(unique))

    # today's news before anniversary/retrospective features
    unique.sort(key=_is_stale_retrospective)

    # feature sections want a small shortlist: the writer picks one, and a long
    # list tempts it to summarise every candidate instead of choosing
    if section.kind == "feature":
        # hand the writer exactly one candidate - asking a small model to choose
        # from a list reliably produces a list of everything on it
        chosen = _pick_feature(unique)
        return [chosen] if chosen else []
    limit = max(section.max_stories * 2, 8)
    candidates = _diversify(unique, per_source=limit)

    if enrich and section.kind == "news":
        for story in candidates[: section.max_stories * 2]:
            if _is_article_url(story.link):
                story.body = _fetch_article_text(story.link)

    return candidates


def _is_article_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if "arxiv.org" in host or "wikipedia.org" in host:
        return False
    return True

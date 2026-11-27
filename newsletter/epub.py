"""Render the newsletter as a Kindle-friendly EPUB 3."""

from __future__ import annotations

import html
import re
from pathlib import Path

from ebooklib import epub

from .compose import Newsletter

CSS = """
body { font-family: Georgia, serif; line-height: 1.55; margin: 0 4%; }
h1 { font-size: 1.7em; margin: 1.2em 0 0.2em; }
h2 { font-size: 1.25em; margin: 1.4em 0 0.4em; border-bottom: 1px solid #ccc; padding-bottom: 0.2em; }
p { margin: 0 0 0.9em; text-align: justify; }
.subtitle { font-style: italic; color: #555; margin-top: 0; }
.meta { font-size: 0.8em; color: #777; }
.intro { font-style: italic; border-left: 3px solid #999; padding-left: 0.8em; }
.sources { font-size: 0.75em; color: #666; margin-top: 0.4em; }
"""


def _paragraphs(text: str) -> str:
    """Convert plain text into escaped <p> blocks."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    out = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) > 1 and all(line.startswith(("-", "*")) for line in lines):
            items = "".join(f"<li>{html.escape(line.lstrip('-* ').strip())}</li>" for line in lines)
            out.append(f"<ul>{items}</ul>")
        else:
            out.append(f"<p>{html.escape(' '.join(lines))}</p>")
    return "\n".join(out)


def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        f"<head><title>{html.escape(title)}</title>"
        f'<link rel="stylesheet" href="style.css" type="text/css"/></head>'
        f"<body>{body}</body></html>"
    )


def build_epub(newsletter: Newsletter, out_path: Path) -> Path:
    book = epub.EpubBook()
    book.set_identifier(f"daily-signal-{newsletter.date:%Y%m%d}")
    book.set_title(f"{newsletter.title} - {newsletter.date:%d %b %Y}")
    book.set_language("en")
    book.add_author(newsletter.author)

    style = epub.EpubItem(
        uid="style", file_name="style.css", media_type="text/css", content=CSS
    )
    book.add_item(style)

    chapters: list[epub.EpubHtml] = []

    cover_body = (
        f"<h1>{html.escape(newsletter.title)}</h1>"
        f'<p class="subtitle">{html.escape(newsletter.subtitle)}</p>'
        f'<p class="meta">{newsletter.date.strftime("%A, %d %B %Y")} &middot; '
        f"{newsletter.word_count} words &middot; ~{max(1, newsletter.word_count // 220)} min read</p>"
    )
    if newsletter.intro:
        cover_body += f'<div class="intro">{_paragraphs(newsletter.intro)}</div>'

    cover = epub.EpubHtml(title="Cover", file_name="cover.xhtml", lang="en")
    cover.content = _xhtml("Cover", cover_body)
    cover.add_item(style)
    book.add_item(cover)
    chapters.append(cover)

    for i, section in enumerate(newsletter.sections, start=1):
        body = f"<h2>{html.escape(section.title)}</h2>{_paragraphs(section.body)}"
        if section.sources:
            body += f'<p class="sources">Sources: {html.escape(", ".join(section.sources))}</p>'
        chapter = epub.EpubHtml(
            title=section.title, file_name=f"section_{i:02d}.xhtml", lang="en"
        )
        chapter.content = _xhtml(section.title, body)
        chapter.add_item(style)
        book.add_item(chapter)
        chapters.append(chapter)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path

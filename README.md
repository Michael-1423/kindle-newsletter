# The Daily Signal

A personal daily newsletter, generated locally and delivered to a Kindle.

No account, no feed algorithm, no editorial board. It fetches from sources you choose,
strips the spin, writes a newsletter with a local LLM, and emails it as an EPUB.

Built because mainstream papers optimise for engagement and agenda rather than for what
is actually happening. This pipeline is deliberately transparent: you can read every
prompt, every source, and every transformation.

---

## Contents

- [What it produces](#what-it-produces)
- [Architecture](#architecture)
- [Stage by stage](#stage-by-stage)
- [Configuration](#configuration)
- [Running it](#running-it)
- [Model comparison](#model-comparison)
- [Setup](#setup)
- [Design decisions and why](#design-decisions-and-why)
- [Known limits](#known-limits)
- [Project layout](#project-layout)

---

## What it produces

A ~3,000-word EPUB (roughly 12–15 minutes of reading) with five sections:

| Section | Kind | Target | What it is |
|---|---|---|---|
| Global Politics | news | 850w | 5 stories, source-balanced, spin removed |
| Tech & AI | news | 850w | research and discoveries, not gadget deals |
| Sports | news | 600w | results, transfers, records |
| History | feature | 650w | one "on this day" event, told as a story |
| One Random Fact | feature | 300w | one surprising Wikipedia article |

The **news** sections get many candidates and pick a balanced subset.
The **feature** sections get exactly one candidate and go deep on it.

---

## Architecture

The pipeline is a straight line. Each stage has one job and a clean interface, so any
stage can be replaced without touching the others.

```
                    config.yaml + .env
                            |
                            v
   +--------------------------------------------------+
   |  1. FETCH            sources.py                  |
   |     RSS / arXiv / Hacker News / Wikipedia        |
   |     -> list[Story]                               |
   +--------------------------------------------------+
                            |
                            v
   +--------------------------------------------------+
   |  2. SELECT           sources.py                  |
   |     dedupe -> filter -> weight -> diversify      |
   |     -> balanced candidate list                   |
   +--------------------------------------------------+
                            |
                            v
   +--------------------------------------------------+
   |  3. NEUTRALISE       neutralise.py    [LLM]      |
   |     cluster for corroboration                    |
   |     batched wire-brief rewrite                   |
   |     -> list[Brief]                               |
   +--------------------------------------------------+
                            |
                            v
   +--------------------------------------------------+
   |  4. COMPOSE          compose.py       [LLM]      |
   |     per-section prose + intro                    |
   |     length enforcement pass                      |
   |     -> Newsletter                                |
   +--------------------------------------------------+
                            |
                            v
   +--------------------------------------------------+
   |  5. RENDER           epub.py                     |
   |     EPUB 3 + plain text                          |
   +--------------------------------------------------+
                            |
                            v
   +--------------------------------------------------+
   |  6. DELIVER          deliver.py                  |
   |     SMTP -> Send to Kindle                       |
   +--------------------------------------------------+
```

Everything below stage 2 is optional: `--dry-run` stops after fetch, `--no-send` stops
before delivery.

### Why this shape

The user's original brief was three layers: **sources → bias removal → LLM composition**.
That is exactly stages 1, 3, and 4. Stage 2 (selection) was added because a naive
"newest first" ordering let a single high-volume feed crowd out every other outlet.
Stages 5 and 6 are mechanical.

The important property is that **bias removal is a separate, inspectable stage**. It is
not a line in a system prompt hoping the model behaves. It produces structured `Brief`
objects with `facts`, `claims`, and `loaded_terms_removed`, which the composer then
consumes. You can log them, diff them, or throw them away.

---

## Stage by stage

### 1. Fetch — `sources.py`

Four fetcher types, selected per source via `type:` in `config.yaml`:

| Type | Source | Notes |
|---|---|---|
| `rss` (default) | Any RSS/Atom feed | Fetched with `requests` then parsed by `feedparser` |
| `arxiv` | arXiv API | Papers lag ~2 days, so needs a longer lookback |
| `hn_algolia` | Hacker News | Algolia API; `hnrss.org` is often unreachable |
| `wikipedia_onthisday` | Wikipedia REST | "On this day" events for today's date |
| `wikipedia_random` | Wikipedia API | Random article extracts for the fact section |

Every story becomes a `Story` dataclass:

```python
Story(title, summary, link, source, section_id, published, body, extra)
```

Per-source failures are caught and logged, never fatal — one dead feed must not kill
the run.

**Lookback** is `lookback_hours` globally (36h default), overridable per source. arXiv
is set to 120h because its API lags.

### 2. Select — `sources.py`

This stage exists because of a specific bug. ESPN stamps all 43 of its RSS entries with
the **same timestamp**. Sorting everything by recency therefore filled the sports section
with 3 ESPN stories and zero from BBC or Sky.

The fix is a pipeline of small, explicit filters:

```
raw stories
  -> dedupe          (title similarity + exact link)
  -> exclude         (config `exclude:` terms, word-boundary matched)
  -> drop digests    ("The Download", "live blog", roundups)
  -> demote stale    ("this day in 2010" retrospectives)
  -> diversify       (cap per outlet, THEN sort by recency)
  -> feature pick    (one item only, for feature sections)
```

`_diversify()` takes up to `limit` stories **per source** before sorting. That is the
core fix: no single outlet can monopolise the candidate list.

**Weights** (`weight:` on a source) push research feeds above deals feeds when slots are
scarce. `_balance_sources()` in `neutralise.py` uses them for a weighted round-robin:
a source with `weight: 0.5` only gets a slot every other pass. This is how Tech & AI
ended up led by ScienceDaily and arXiv instead of The Verge's Black Friday coverage.

**Feature sections get exactly one candidate** via `_pick_feature()`. This is deliberate:
small models asked to "choose the best of these three" reliably write about all three.
Removing the choice removes the failure. The pick is deterministic (longest summary) so
the same day's material always produces the same result — which keeps model comparisons
fair.

### 3. Neutralise — `neutralise.py` [LLM]

The impartiality layer. Three passes, cheapest first:

**a. Corroboration clustering.** Stories describing the same event are grouped by title
similarity (embeddings available but off by default — loading a second model next to the
writer costs ~5 GB VRAM). A cluster's size tells the composer whether a claim is
multi-source or single-source.

**b. Source balance.** Weighted round-robin as described above.

**c. Neutral rewrite.** Each selected story becomes a `Brief`:

```python
Brief(story, headline, summary, facts, claims, loaded_terms, framing_removed,
      corroborating_sources, cluster_size)
```

The prompt is a **wire-service editor**, not a summariser. It strips adjectives, emotive
verbs, and editorial framing; forbids attributing motive unless a named source states it;
and separates `facts` from `claims` — assertions lacking a named source. The composer is
then told to hedge `SINGLE SOURCE` briefs and never present `claims` as fact.

**Batching.** Neutralising is a *short-output* task (~100–150 tokens per brief), so it is
dominated by per-call overhead rather than token cost. `_neutralise_batch()` handles 5
stories per call: 12 calls → 3, roughly **40% off wall-clock time**. It degrades
gracefully — malformed JSON falls back to per-story calls, and missing indices fall back
to raw summaries.

### 4. Compose — `compose.py` [LLM]

One call per section, plus a short intro. Two prompt families:

- **`NEWS_PROMPT`** — flowing prose, one paragraph per brief, ends with "Why it matters".
- **`FEATURE_PROMPT`** — exactly one item, told as a story with a hook and payoff.

Both carry the same **GROUNDING RULES**: every name, number, date and place must appear
in the supplied material; never guess. This was added after both candidate models
fabricated historical details that were not in the source.

**Length enforcement.** Models reliably undershoot a word target. `_ensure_length()`
re-runs the draft with an expansion prompt, up to 2 passes, and **never accepts a shorter
result**. It stops early once a pass stops helping.

Note the model plateaus near 600 words per section regardless of instruction. Two passes
reach ~750–815. This is a capability limit, not a context limit — see
[notes/local-llm-performance.md](notes/local-llm-performance.md).

`_strip_markdown_noise()` removes headings the model echoes (the EPUB adds its own) and
strips markdown emphasis.

### 5. Render — `epub.py`

EPUB 3 via `ebooklib`: a cover page with the intro, then one chapter per section, with a
stylesheet tuned for e-ink (Georgia, 1.55 line height, justified). A plain `.txt` twin is
written alongside for diffing and quick reading.

### 6. Deliver — `deliver.py`

SMTP with STARTTLS, EPUB attached. Amazon converts it on arrival. Requires the sender
address to be on the Kindle **Approved Personal Document Email List**.

> Not yet exercised end to end — this is the next milestone.

---

## Configuration

Two files, deliberately separate:

- **`config.yaml`** — content: sections, sources, weights, filters, prompts. Safe to commit.
- **`.env`** — secrets and runtime: Ollama host/model, Kindle address, SMTP credentials. Never committed.

### Adding a section

```yaml
  - id: science
    title: "Science"
    kind: news            # news | feature
    words: 700
    max_stories: 4
    focus: "peer-reviewed findings, not press releases"
    exclude: ["sponsored"]
    sources:
      - name: Nature
        url: "https://www.nature.com/nature.rss"
        weight: 1.5       # higher = more likely to win a scarce slot
```

### Adding a source

```yaml
      - name: Some Feed
        url: "https://example.com/rss.xml"
        lookback_hours: 72    # optional override
        weight: 1.0
```

### Environment (`.env`)

```bash
OLLAMA_HOST=http://localhost:11435      # note: not the default 11434
OLLAMA_MODEL=gemma4:12b-mlx
OLLAMA_THINK=false                      # required for reasoning models
OLLAMA_NUM_CTX=8192                     # Ollama defaults to 4096
OLLAMA_COMPARE_MODELS=llama3.1:8b
KINDLE_EMAIL=your-kindle@kindle.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=your-app-password
```

---

## Running it

```bash
python -m newsletter.main --dry-run     # fetch only, no LLM (~12s)
python -m newsletter.main --no-send     # full build, no email
python -m newsletter.main               # build and send to Kindle
python -m newsletter.main --model llama3.1:8b   # override writer model
```

Output lands in `output/daily-signal-<date>.epub` and `.txt`.

---

## Model comparison

Deciding between models should not be vibes-based. `compare.py` fetches material **once**
and builds the full newsletter with each model over byte-identical input, so the only
variable is the model.

```bash
python -m newsletter.main --compare
python -m newsletter.main --compare gemma4:12b-mlx llama3.1:8b
```

Each model writes to `output/<date>/<model-slug>/`, and a `comparison.md` is generated
with timings, word counts, and per-section breakdown. The `Ollama` client tracks calls,
tokens, and generation seconds, so throughput is measured rather than assumed.

**Judge on fabrication first**, then length, structure, neutrality, repetition. Read the
`.txt` files side by side. Judge over several days — one run is noisy.

Day-1 results and observations: [notes/model-comparison.md](notes/model-comparison.md).

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill it in
```

**Ollama** must be running. Note the port: this project expects **11435**, not the
default 11434. Set it explicitly if needed:

```bash
OLLAMA_HOST=0.0.0.0:11435 ollama serve
```

Pull a writer model and verify it works:

```bash
ollama pull gemma4:12b-mlx
python -m newsletter.main --dry-run
python -m newsletter.main --no-send
```

**Kindle delivery** (not yet verified):

1. Find your address: Amazon → Manage Your Content and Devices → Devices → your Kindle.
2. Approve the sender: Preferences → Personal Document Settings → Approved Personal
   Document Email List.
3. Use an SMTP **app password**, not your account password.

---

## Design decisions and why

| Decision | Reason |
|---|---|
| Separate neutralise stage, not a prompt line | Inspectable and testable. Produces structured `facts`/`claims` instead of hoping. |
| Batch short LLM calls, keep long ones separate | Short outputs are overhead-bound, so batching is nearly free. Long outputs would hit context limits and degrade. |
| One candidate for feature sections | Small models write about everything in a list. Removing choice removes the failure. |
| Deterministic feature pick | Keeps A/B comparisons fair across models and days. |
| Per-source caps before recency sort | ESPN's identical timestamps otherwise crowd out every other outlet. |
| Weights on sources | Reader wants research, not deals. Weighting is explicit and tunable. |
| `think: false` | Reasoning models burn the whole budget on hidden thinking and return empty strings. |
| `num_ctx: 8192` | Ollama's 4096 default silently truncates long brief bundles. Cheap insurance. |
| Graceful degradation everywhere | One dead feed, or one bad LLM call, must not lose the whole issue. |
| Local-only | No API keys, no per-token cost, no data leaving the machine. |

---

## Known limits

- **Sections plateau at ~600 words** with a 12B model. Reaching a target is easier by
  adding a section than by stretching an existing one.
- **Base M4 is bandwidth-bound** at ~120 GB/s, capping a 7.7 GB model near 15–35 tok/s.
  Measured 17–34. There is no large software win available. See the notes.
- **Fabrication risk is real** and is the main quality concern. Grounding rules reduce it;
  they do not eliminate it. Read critically.
- **Neutrality is not objectivity.** The pipeline removes loaded language and separates
  claims from facts. It cannot verify that a source is telling the truth, and it inherits
  whatever the source feeds omit.
- **Embeddings clustering is off by default** to avoid memory pressure.
- **Delivery is untested.** EPUB and SMTP are written but not yet verified end to end.
- **No scheduling yet.** Intended: launchd at 08:00.

---

## Project layout

```
newsletter/
  config.py       settings + YAML/.env loading
  ollama.py       thin Ollama client, think/num_ctx aware, token stats
  sources.py      fetchers, filtering, diversification, feature pick
  neutralise.py   clustering, source balance, batched wire-brief rewrite
  compose.py      section prose, length enforcement, intro
  epub.py         EPUB 3 rendering
  deliver.py      Send-to-Kindle over SMTP
  compare.py      A/B harness over shared material
  main.py         CLI and orchestration

config.yaml       sections, sources, weights, filters, prompts
notes/            performance analysis, model comparison log, to-do
output/           generated newsletters (gitignored)
```

---

## Further reading

- [notes/local-llm-performance.md](notes/local-llm-performance.md) — why local inference
  is as fast as it is: the bandwidth equation, prompt vs generation, the `num_ctx` trap,
  why switching clients doesn't help, and how to benchmark fairly.
- [notes/model-comparison.md](notes/model-comparison.md) — day-by-day model comparison log.
- [notes/todo.md](notes/todo.md) — open items.

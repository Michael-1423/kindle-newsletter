# To-do

State at end of 2026-11-28 session. Ordered by what unblocks the most.

---

## Tomorrow: decide on a model

- [ ] Read `output/2026-11-28/gemma4-12b-mlx/*.txt` and
      `output/2026-11-28/llama3-1-8b/*.txt` side by side
- [ ] Confirm the day-1 call: **llama3.1:8b is out** (fabricated historical detail,
      and was not actually faster). Record the reasoning in `notes/model-comparison.md`
- [ ] Run a second comparison with a fresh candidate or two, e.g.
      `python -m newsletter.main --compare gemma4:12b-mlx qwen3:8b`
      Candidates worth trying: `qwen3:8b`, `mistral-nemo:12b`, `gemma3:12b`
- [ ] **Re-run on a quiet machine with swap at 0** for a fair speed reading:
      `sysctl -n vm.swapusage` before and after
- [ ] Judge each model on **fabrication first**, then length, structure, neutrality

## Re-verify the fixes made late in the session

These were verified individually but not in a full end-to-end run:

- [ ] Single-story fix: history and fact sections should each produce **one** story.
      Was confirmed for `_compose_feature()` in isolation (570 words, one event).
      Confirm it holds across a full `--no-send` run
- [ ] Grounding rules: check the new "never guess a name or number" instruction actually
      reduces invented detail, by diffing against the day-1 output
- [ ] Batched neutralise: confirm briefs still look right in a full run, and watch for
      the `batch covered N/M items` log line indicating fallbacks
- [ ] Confirm `num_ctx: 8192` is actually being applied (`curl -s localhost:11435/api/ps`)

## Then: Kindle delivery (the last untested stage)

- [ ] Fill `.env` with `KINDLE_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
      (use a Gmail **app password**, not the account password)
- [ ] Approve the sender address in Amazon → Manage Your Content and Devices →
      Preferences → Personal Document Settings → Approved Personal Document Email List
- [ ] Test with `python -m newsletter.main` (no `--no-send`)
- [ ] Confirm the EPUB renders correctly on the device: cover, TOC, chapter breaks,
      justified text, no broken entities
- [ ] Check it survives Amazon's conversion — EPUB is preferred, but verify formatting

## Then: scheduling

- [ ] Write a `launchd` plist for 08:00 daily (`~/Library/LaunchAgents/`)
- [ ] Log to a file so a silent failure is visible the next morning
- [ ] Ensure the machine is awake at 08:00, or accept that a missed run is skipped
- [ ] Add a `--notify` failure path (desktop notification or fallback email)

## Polish

- [ ] Word count is ~3,000 vs a 3,400 target. Either add a 6th section or accept ~13 min
- [ ] Consider enabling embeddings clustering once memory headroom is confirmed
- [ ] `deliver.py` guesses MIME type then ignores it — pass `mime` through or drop it
- [ ] `compare.py` catches bare `Exception` alongside `OllamaError`; narrow it
- [ ] Add a `--sections` flag to build a single section for fast prompt iteration
- [ ] Cache fetched stories so prompt tweaks don't re-hit the network

## Deferred

- [ ] Push to a private GitHub repo (blocked: `gh` not authenticated, no token in env)
- [ ] Unit tests for filtering and selection (the logic with real bug history)
- [ ] Archive old `output/` folders automatically

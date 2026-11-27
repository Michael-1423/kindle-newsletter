# Model Comparison Log

One section per day. Judge over several days before choosing; a single run is noisy
because feed content changes daily and sampling is stochastic.

Method: `python -m newsletter.main --compare` fetches material **once** and builds
the full newsletter with each model over byte-identical input. Outputs land in
`output/<date>/<model-slug>/`.

---

## Day 1 - 2026-11-28

| Model | Time | Words | ~min | tok/s | LLM calls | Out tokens |
|---|---|---|---|---|---|---|
| `gemma4:12b-mlx` | 607s | 2,985 | 13 | 22.8 | 16 | 10,398 |
| `llama3.1:8b` | 511s | 3,581 | 16 | 20.0 | 14 | 8,797 |

### Words per section

| Section | gemma4 12B | llama3.1 8B | target |
|---|---|---|---|
| Global Politics | 942 | 1,116 | 850 |
| Tech & AI | 813 | 722 | 850 |
| Sports | 314 | 519 | 600 |
| History | 576 | 792 | 650 |
| One Random Fact | 280 | 359 | 300 |

### Observations

**llama3.1:8b is not actually faster.** It finished sooner (511s vs 607s) but at a
*lower* token rate (20.0 vs 22.8 tok/s) — it simply wrote fewer tokens overall and
needed one fewer expansion pass. On a base M4 both are bandwidth-bound; the 8B's
smaller size (4.9 GB vs 7.7 GB) did **not** translate into the expected ~1.6x speedup.
Worth re-checking on a quiet machine with no swap in use before drawing conclusions.

**Length:** llama won comfortably (3,581 vs 2,985 words, both beating target in the
news sections). It also needed fewer expansion passes.

**Fabrication — the decisive criterion.** The history section is the clearest signal.
Both models were given exactly three source sentences:

- 1895: Nobel signed his will, establishing the Nobel Prize.
- 1868: Custer's 7th Cavalry attacked Black Kettle's Cheyenne encampment on the Washita River.
- 1856: William III revised Luxembourg's constitution.

`llama3.1:8b` invented a great deal on top of that: a "5th Cavalry Brigade", cannon
fire, death tolls of "100 to 150", Black Kettle's wife being named "Medicine Woman",
the *New York Tribune*, a correspondent "James De Wolf", and "atrocity propaganda".
Some of these are real history but **none were in the supplied material**, and the
model presented them as established fact.

`gemma4:12b-mlx` fabricated less but still added unsupplied detail (the "merchant of
death" epithet, the 1896 death date, prizes first awarded 1901). Its prose is flatter
and more hedged, which reads as more trustworthy even when it is also guessing.

**Single-story rule: both failed.** Both wrote about all three history events instead
of picking one (3/3 source events mentioned each). The prompt says explicitly to write
about exactly one and ignore the others. This needs a structural fix — passing a
shortlist of one is more reliable than asking a model to self-select.

### Day 1 verdict

Do not decide yet. Lean: **gemma4 12B for factual restraint, llama3.1 8B for length.**
The fabrication gap is the more important one, since the whole point of this project is
trustworthy news — but day 1 of 3+ is not enough to call it.

### To fix before the next comparison

1. Feature sections should receive **one** candidate, not three (removes the choice).
2. Ground the writer harder: pass the raw source sentence alongside each brief and
   instruct "if a detail is not in the source, leave it out".
3. Re-run both models on a quiet machine (swap at 0) for a fair speed reading.

---

## Decision

**2026-11-28: `llama3.1:8b` rejected.** Day-1 fabrication on the history section was
extensive (invented unit designations, casualty figures, a named correspondent and his
newspaper), and it was not meaningfully faster — 20.0 tok/s vs gemma4's 22.8, because
the 8B wrote fewer tokens rather than writing them faster. Speed was the only reason to
prefer it, and speed did not materialise.

`gemma4:12b-mlx` is the working default while other models are tested.

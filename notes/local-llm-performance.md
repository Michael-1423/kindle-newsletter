# Local LLM Performance on Apple Silicon

Notes on why the newsletter takes as long as it does, what can be fixed, and what
cannot. Written as a reference for future work on this project or any other local
LLM pipeline on this machine.

---

## 1. The machine

| Property | Value |
|---|---|
| Chip | Apple M4 (base, not Pro/Max) |
| Unified memory | 24 GB |
| CPU cores | 4 performance + 6 efficiency |
| **Memory bandwidth** | **120 GB/s** |
| Ollama | 0.32.14, using the **MLX** engine |

The single most important number here is **120 GB/s**. On Apple Silicon the GPU and
CPU share the same memory, so there is no separate VRAM to copy into. That is
convenient, but it means token generation speed is bounded by how fast weights can be
streamed from memory into the compute units.

### Bandwidth tiers matter enormously

| Chip | Memory bandwidth | Relative |
|---|---|---|
| M4 (base) | 120 GB/s | 1x |
| M4 Pro | 273 GB/s | 2.3x |
| M4 Max | 546 GB/s | 4.6x |

Two machines with the *same* 24 GB of RAM can differ by 4.6x in generation speed
purely because of bandwidth. This is why "does it fit in RAM?" is only half the
question.

---

## 2. The core equation

Token generation is **memory-bandwidth bound**. Roughly, every generated token
requires reading the whole model once:

```
tokens/second  ≈  memory bandwidth  ÷  model size in memory
```

For our 12B model at 4-bit (7.7 GB on disk):

```
120 GB/s ÷ 7.7 GB ≈ 15.6 tokens/second
```

### Measured reality

| Scenario | Measured |
|---|---|
| Short prompt, idle machine | **31–34 tok/s** |
| Long prompt, machine swapping | **17–22 tok/s** |
| Theoretical naive estimate | ~15.6 tok/s |

We are measuring **at or above** the naive estimate. That is expected: the estimate
assumes every byte is read with zero reuse and ignores KV-cache behaviour, quantisation
details, and MLX's kernel optimisations. Treat it as an order-of-magnitude floor, not a
hard ceiling.

**The practical conclusion is unchanged: there is no large throughput win available.**
We are already in the right ballpark for this hardware. A 2x speedup would require
different silicon, not different software.

### The one real win we found

The 17 tok/s vs 34 tok/s gap was **not** a code problem. It was **6.1 GB of swap in
use** on a 7 GB swap file. The model (7.7 GB) plus macOS plus the rest of the stack
exceeded comfortable headroom, so pages were being written to and read from SSD.

Freeing memory roughly doubled generation speed with no code change at all.

**Rule: check swap before optimising code.**

```bash
sysctl -n vm.swapusage
memory_pressure | tail -3
```

---

## 3. Prompt processing vs generation

These are very different costs and are often conflated.

| Phase | Speed | What it is |
|---|---|---|
| **Prompt processing** | ~132 tok/s | Reading the input. Parallel, efficient. |
| **Generation** | ~17–34 tok/s | Writing the output. Sequential, memory-bound. |

Measured: a 2,363-token prompt took 18.6s to process, then generated at ~22 tok/s.

Prompt processing is roughly **4–6x faster** than generation. Consequences:

- Feeding the model a lot of context is comparatively cheap.
- Asking it to write a lot is expensive.
- Therefore: **long inputs are fine, long outputs are the cost centre.**

This is why a 3,300-word newsletter is ~4,400 output tokens ≈ **3 minutes of pure
generation** at 25 tok/s.

---

## 4. Where the time actually went

An 11-minute run was **not** dominated by slow tokens. It was dominated by **number of
calls**.

Original architecture per run:

- 12 separate neutralise calls (one per story, 4 sections)
- 5 compose calls (one per section)
- 7 expansion passes (sections falling short of target)
- **24 LLM round-trips**

At ~25s each, that is ~10 minutes. The tokens were never the problem; the **serialised
round-trips** were.

### Fix: batch the neutralise pass

Neutralising is a *short-output* task: each brief is ~100–150 tokens. That means it is
cheap per item and dominated by fixed per-call overhead. Batching 5 stories into one
call:

- 12 calls → **3 calls**
- Measured: 45s for 3 items (~15s/item) vs ~25s/item separately
- Roughly **40% off total runtime**

Batching works here because short outputs don't saturate the context, and the model
handles "rewrite these 5 numbered items" reliably.

**Batching does NOT work well for the compose pass.** Those are long outputs (600–900
tokens each); combining them would hit context limits and degrade quality. Keep those
one per section.

---

## 5. Context length: the trap we checked and cleared

Ollama defaults `num_ctx` to **4096** regardless of what the model supports. Our model
natively supports **262,144**.

The hypothesis was reasonable: if a prompt is ~3,000 tokens, only ~1,000 remain for
output ≈ 700 words — suspiciously close to where our sections plateaued.

**Tested and disproved:**

| `num_ctx` | Output words | `done_reason` |
|---|---|---|
| 4096 | 310 | `stop` |
| 8192 | 291 | `stop` |

`done_reason: stop` means *the model chose to finish*, not *it ran out of room*
(which would be `length`). Raising the window changed nothing, and the longer window
even produced slightly fewer words.

**Conclusion:** the ~600-word plateau is a **model capability limit**, not truncation.

We still set `num_ctx: 8192` as the default, because it is cheap (~1 GB VRAM) and
protects against real truncation on larger brief bundles. It just isn't the reason for
short sections.

### Separately: the empty-response bug

Early on every call returned an empty string with `done_reason: length`. Cause: this is
a **reasoning model**, and it spent the entire token budget on hidden thinking before
emitting any visible output.

Fix: `think: false` in the API payload. Generation went from 36s producing nothing to
2.6s producing a correct answer.

**Rule: any reasoning model (gemma4, qwen3, deepseek-r1) needs thinking explicitly
disabled for structured extraction tasks.**

---

## 6. Why switching clients does not help

Ollama 0.32 already runs Apple's MLX backend:

```
ollama runner --mlx-engine --model gemma4:12b-mlx
```

LM Studio, `mlx-lm`, Jan, and llama.cpp all use **the same MLX kernels** on this
hardware. Realistic difference between them: **±10–15%**, mostly from default sampling
settings and scheduling, not from a fundamentally faster path.

Switching clients is not worth it for a ~10% gain. Switching **model size** is worth it,
because that changes the numerator in the bandwidth equation.

---

## 7. What actually changes the numbers

| Lever | Effect | Verdict |
|---|---|---|
| **Free up swap** | ~2x tok/s | Do first, costs nothing |
| **Batch short-output calls** | −40% wall clock | Done |
| **Raise `num_ctx` to 8192** | prevents real truncation | Done |
| **Smaller model (8B)** | ~1.5–2x tok/s, ~4.5 GB | Worth A/B testing |
| **More sections** | +words without fighting the model | Best way to add length |
| Bigger model (30B+) | Slower, may not fit comfortably | Not for 24 GB base M4 |
| Different client | ±10–15% | Not worth the churn |

### The key insight on length

The model plateaus around **600 words per section** no matter how it is asked. Two
expansion passes got sections to 750–815 words, at ~45s per pass.

Two ways to reach a target length:

1. **Fight the model** — more expansion passes. Diminishing returns, costs time.
2. **Add sections** — each new section is a fresh ~600 words the model writes happily.

Option 2 is far more efficient. If a 20-minute issue is wanted, add a section rather
than trying to stretch existing ones.

---

## 8. Model comparison methodology

To decide between models fairly, **only the model should vary**. So:

1. Fetch all source material **once**.
2. Feed byte-identical material to each model.
3. Write each output to `output/<date>/<model-slug>/`.
4. Generate `comparison.md` with timings, word counts, and per-section breakdown.

Run it with:

```bash
python -m newsletter.main --compare
# or name them explicitly
python -m newsletter.main --compare gemma4:12b-mlx llama3.1:8b
```

### What to judge

Read the `.txt` outputs side by side and check:

1. **Fabrication** — facts, numbers, or names not present in the source material.
   This is the most serious failure and the reason to reject a model outright.
2. **Length** — does it approach the per-section target or stall short?
3. **Structure** — one story told properly, or a list of items summarised?
4. **Neutrality** — editorial adjectives, unattributed motive, loaded framing.
5. **Repetition** — the same point made twice.

### Judge over several days

A single run is noisy: feed content changes daily, and sampling is stochastic. **Three
to five days of output per model** is the minimum for a defensible decision. Keep every
day's folders; they are the evidence.

---

## 9. Quick reference

```bash
# check for the real bottleneck
sysctl -n vm.swapusage
memory_pressure | tail -3

# what is loaded and at what context
curl -s http://localhost:11435/api/ps

# model facts (native context, layer count, parameter count)
curl -s http://localhost:11435/api/show -d '{"model":"gemma4:12b-mlx"}'

# single run, no email
python -m newsletter.main --no-send

# A/B two models over identical material
python -m newsletter.main --compare
```

### Numbers to remember

| Thing | Value |
|---|---|
| Base M4 bandwidth | 120 GB/s |
| 12B @ 4-bit | 7.7 GB |
| Naive ceiling | ~15.6 tok/s |
| Observed generation | 17–34 tok/s |
| Observed prompt processing | ~132 tok/s |
| Model plateau per section | ~600 words |
| 3,300 words of output | ~4,400 tokens ≈ 3 min generation |

### The three rules

1. **Check swap before optimising code.** Swapping silently halves throughput.
2. **Count round-trips, not just tokens.** Batching short outputs beats micro-tuning.
3. **When output is short, suspect the model, not the context window.** Verify with
   `done_reason`: `stop` = model chose to stop, `length` = truncated.

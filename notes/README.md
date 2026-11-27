# Notes

Reference notes for this project. Written to be re-read later, not just as scratch.

| Note | What it covers |
|---|---|
| [local-llm-performance.md](local-llm-performance.md) | Why local inference is as fast as it is, the bandwidth equation, what batching and `num_ctx` actually buy, why switching clients doesn't help, and how to A/B two models fairly. |
| [model-comparison.md](model-comparison.md) | Day-by-day model comparison log with timings, word counts and fabrication evidence. |
| [todo.md](todo.md) | Open items, ordered by what unblocks the most. |

## The short version

- Base M4 has **120 GB/s** memory bandwidth. A 7.7 GB model therefore tops out near
  **15–35 tok/s**. We measure 17–34. **We are at the hardware limit.**
- **Prompt processing is ~5x faster than generation.** Long inputs are cheap; long
  outputs are the cost.
- Runtime was dominated by **24 serialised LLM calls**, not slow tokens. Batching the
  short neutralise calls cut ~40%.
- Sections plateau at **~600 words** because of the model, not the context window
  (verified: `done_reason: stop`, and 8192 ctx produced *fewer* words).
- Switching to LM Studio / mlx-lm / Jan gains **±10–15%**, because Ollama already runs
  the same MLX kernels.
- **Check swap first.** 6 GB of swap halved throughput with zero code changes.

## Model comparison

`llama3.1:8b` was **rejected on day 1**: it fabricated historical detail not present in
the source (unit names, casualty figures, a named reporter), and was not actually faster
— 20.0 tok/s vs gemma4's 22.8, because it wrote fewer tokens rather than writing them
faster. `gemma4:12b-mlx` is the working default while other models are tested.

Each day's output lands in `output/<date>/<model>/`, with a generated `comparison.md`
alongside. Judge on fabrication first, then length, structure, neutrality, and
repetition. See [model-comparison.md](model-comparison.md) for the log.

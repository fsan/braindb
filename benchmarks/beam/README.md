# BrainDB on BEAM (Beyond a Million Tokens)

Public benchmark harness running BrainDB against the **BEAM** memory benchmark
([arXiv:2510.27246](https://arxiv.org/abs/2510.27246), ICLR 2026, Tavakoli et al.
of U Alberta + UMass Amherst).

**Status**: Step 0 scaffolding (this file, the bench compose, the upstream
submodule). Phase 1 harness code (`adapter.py`, `bench.py`, `judge.py`,
`score.py`, `warmup.py`) is being implemented in the same commit series.

---

## Trust model

The integrity bar is: **the parts that score us are theirs, used unmodified**.
Nothing in our code paraphrases, copies, or reinterprets the benchmark.

| Component | Where it lives | Why this matters |
|---|---|---|
| BEAM dataset | `Mohammadta/BEAM` on HuggingFace; pinned by sha256 in our harness | Same SHA → same dataset; anyone can verify |
| BEAM eval script | Git submodule `upstream/` pinned to a specific commit SHA of `mohammadtavakoli78/BEAM` | We call `python -m src.evaluation.run_evaluation` unmodified |
| Judge prompt | `from src.prompts import unified_llm_judge_base_prompt` — loaded at runtime from the submodule | Zero prompt content in our codebase; no risk of paraphrase or typo |
| Adapter (dataset → BrainDB ingest) | `adapter.py` (ours, ~150 LOC) | Transparent: read the code |
| Bench runner (per-conversation reset + ingest + warmup + answer) | `bench.py` (ours, ~120 LOC) | Same |
| Judge runner (calls Qwen with the upstream prompt) | `judge.py` (ours, ~80 LOC) | Anyone can re-judge our `answers.json` with any model |
| Eval wrapper (invokes upstream eval) | `score.py` (ours, ~50 LOC) | Just a thin caller |

**The "no copy" rule** is satisfied at the strongest level: the judge prompt
text never leaves the upstream submodule. We import it as a Python string
and pass it to our chosen judge model. Hindsight and mem0 both do the same
(they wire it into their own judge runners); the upstream eval code itself
already imports it from `src/prompts.py::unified_llm_judge_base_prompt`.

---

## Isolation from your personal BrainDB

The bench runs in `docker-compose.bench.yml` — a completely separate stack
from your personal `docker-compose.yml`. Layered isolation:

- Separate Docker project namespace: `name: braindb_bench`
- **Separate Postgres container** (`braindb_bench_postgres`) on host port 5434
  (host port 5433 is already used by the personal `postgres_container` in
  this environment; bench takes 5434 to avoid a collision)
- Separate Postgres database: `braindb_bench` (never `braindb`)
- Separate Postgres data volume: `braindb_bench_pgdata`
- **Separate BrainDB API on port 8001** (personal stays on 8000)
- **Separate host data directory**: `./data_bench/sources/` — the bench
  watcher polls this; personal watcher continues polling `./data/sources/`
  and never sees bench files
- Hard-coded safety assertion in `bench.py`: the active `DATABASE_URL`
  MUST contain `braindb_bench` literally, or the runner refuses to start
- Explicit invocation: bench requires
  `docker compose -f docker-compose.bench.yml --env-file .env.bench up`;
  a plain `docker compose up` runs the personal stack as normal

Before the first benchmark run, snapshot your personal Postgres volume as a
paranoia tarball:

```bash
docker run --rm -v braindb_pgdata:/data -v "$(pwd)":/backup alpine \
  tar czf /backup/braindb_personal_backup_$(date +%Y%m%d).tar.gz /data
```

One-command restore if anything ever goes wrong.

---

## Caveats on the published number

We use **Qwen 3.6-27B** (local, via the workstation vLLM tunnel) as the LLM
judge. Published BEAM scores used GPT-4o (mem0) or Gemini-2.5-flash-lite
(Hindsight). **Our Qwen-judged number is NOT directly comparable to those
published numbers** — judge models systematically differ.

The mitigations:

1. We publish `answers.json` (our raw answers, judge-independent) so anyone
   with another model's API access can re-judge our work and verify.
2. We run a 30-question stratified Claude Sonnet calibration sample (~$15–30
   Anthropic credit) so the delta between Qwen and Claude scoring is
   published alongside the headline.
3. BEAM's ceiling is ~73% (Hindsight @ 1M, Gemini judge) / ~64% (mem0 @ 1M,
   GPT-4o judge) — lower than LongMemEval's ~95%. Less headroom → small judge
   biases shift the number more. This makes the calibration sample
   **mandatory for credibility**, not optional.

Bench-mode config tunes the cadence of the wiki maintenance pipeline (5s
tick instead of 60s; writer concurrency 5 instead of 1) so per-conversation
warmup completes in minutes, not 30+ min. **The pipeline itself is
identical to production** — same extraction prompts, same wiki maintainer,
same writer. Only the throughput knobs differ, and they are listed
explicitly in `docker-compose.bench.yml` so reviewers see them.

---

## Citations

If you reference BrainDB's BEAM numbers, please also cite the original BEAM
paper — it's their benchmark, we just ran on it:

```
@inproceedings{tavakoli2026beam,
  title={Beyond a Million Tokens: Benchmarking and Enhancing Long-Term Memory in LLMs},
  author={Tavakoli, Mohammad and Salemi, Alireza and Ye, Carrie and Abdalla, Mohamed and Zamani, Hamed and Mitchell, J. Ross},
  booktitle={ICLR 2026},
  year={2026}
}
```

BEAM dataset and code are CC BY-SA 4.0 / MIT respectively; see
`upstream/LICENSE`.

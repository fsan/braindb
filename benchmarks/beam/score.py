"""BEAM run reporter.

Reads ``judge_summary.json`` from a run dir and writes a human-readable
``results.md`` in the same dir. Aggregate scores are already computed by
``judge.py`` (using the upstream BEAM judge prompt verbatim); this script
just formats them for review.

Optionally also invokes upstream's full ``run_evaluation`` if the user has
installed the upstream deps in their venv — this gives BLEU/ROUGE/cosine
metrics alongside the LLM-judge score. Off by default (heavy install).

CLI::

    python -m benchmarks.beam.score --run-dir benchmarks/beam/runs/<run_id>
    python -m benchmarks.beam.score --run-dir <run_id> --invoke-upstream
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _fmt_pct(score: float) -> str:
    return f"{score * 100:.2f}%"


def _render_markdown(summary: dict, run_config: dict | None) -> str:
    lines: list[str] = []
    lines.append(f"# BEAM run — {_fmt_pct(summary['overall_average_score'])}")
    lines.append("")
    if run_config:
        lines.append("## Run config")
        lines.append("")
        for k in ("split", "limit", "started_at", "git_sha", "git_dirty"):
            if k in run_config:
                lines.append(f"- **{k}**: `{run_config[k]}`")
        lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(f"- **Overall score** (LLM-judge nugget average, all questions): "
                 f"**{_fmt_pct(summary['overall_average_score'])}**")
    lines.append(f"- **Questions judged**: {summary['total_questions_judged']}")
    lines.append(f"- **Conversations**: {summary['conversations_judged']}"
                 f" / {summary['conversations_attempted']}")
    lines.append("")
    lines.append("> **Caveat**: This score uses Qwen 3.6-27B as the LLM judge "
                 "via the workstation vLLM endpoint. Published BEAM scores from "
                 "Hindsight (73.9% @ 1M) and mem0 (64.1% @ 1M) used Gemini-2.5-"
                 "flash-lite and GPT-4o respectively. Our score is NOT directly "
                 "comparable to those numbers until a cross-judge calibration "
                 "sample is published (see plan: Phase 3).")
    lines.append("")
    lines.append("## Per-category breakdown")
    lines.append("")
    lines.append("| Category | Questions | Average score |")
    lines.append("|---|---|---|")
    for cat, info in sorted(summary["categories"].items()):
        lines.append(f"| {cat} | {info['question_count']} | "
                     f"{_fmt_pct(info['average_score'])} |")
    lines.append("")
    lines.append("## Per-conversation detail")
    lines.append("")
    lines.append("| Conversation | Questions | Score |")
    lines.append("|---|---|---|")
    for conv in summary["per_conversation"]:
        if "fatal_error" in conv:
            lines.append(f"| {conv['conv']} | — | **FATAL: {conv['fatal_error']}** |")
        else:
            lines.append(
                f"| {conv['conv']} | {conv.get('total_questions_judged', 0)} | "
                f"{_fmt_pct(conv.get('overall_average_score', 0.0))} |"
            )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _invoke_upstream(run_dir: Path) -> int:
    """Optionally invoke upstream's run_evaluation across all per-conv pairs.
    Requires upstream/requirements.txt installed in the active environment.
    """
    print("WARNING: invoking upstream run_evaluation requires "
          "benchmarks/beam/upstream/requirements.txt installed. "
          "This adds spacy + sentence-transformers + scipy etc. (~50 deps).")
    upstream_src = run_dir.resolve().parents[2] / "upstream" / "src"
    conv_dirs = sorted(p for p in run_dir.iterdir() if p.is_dir() and p.name.startswith("conv_"))
    for conv_dir in conv_dirs:
        ans = conv_dir / "answers.json"
        pq = conv_dir / "probing_questions.json"
        out = conv_dir / "upstream_eval_output.json"
        if not ans.exists() or not pq.exists():
            print(f"skip {conv_dir.name}: missing answers.json or probing_questions.json")
            continue
        print(f"running upstream eval on {conv_dir.name} ...")
        env_cmd = [
            "python", "-c",
            "import sys; sys.path.insert(0, r'"
            + str(upstream_src).replace("\\", "\\\\")
            + "'); "
            "from src.evaluation.run_evaluation import run_evaluation; "
            "from src.llm import gpt_llm; "
            "run_evaluation("
            f"probing_questions_address=r'{pq}', "
            f"answers_directory=r'{ans}', "
            f"output_address=r'{out}', "
            "model=gpt_llm)"
        ]
        result = subprocess.run(env_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  failed: {result.stderr[:400]}")
        else:
            print(f"  -> {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--invoke-upstream", action="store_true",
                   help="also invoke upstream's run_evaluation (requires upstream deps)")
    args = p.parse_args()

    run_dir = Path(args.run_dir).resolve()
    summary_path = run_dir / "judge_summary.json"
    if not summary_path.exists():
        print(f"no judge_summary.json in {run_dir}. Run `python -m benchmarks.beam.judge --run-dir {run_dir}` first.")
        return 1

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    run_config_path = run_dir / "run_config.json"
    run_config = json.loads(run_config_path.read_text(encoding="utf-8")) if run_config_path.exists() else None

    md = _render_markdown(summary, run_config)
    results_path = run_dir / "results.md"
    results_path.write_text(md, encoding="utf-8")
    print(f"wrote {results_path}")
    print()
    print(md)

    if args.invoke_upstream:
        _invoke_upstream(run_dir)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

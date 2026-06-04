"""BEAM dataset -> BrainDB ingest adapter.

For each conversation in the BEAM dataset (Mohammadta/BEAM on HuggingFace),
render the full chat history as a single markdown file under
``data_bench/sources/beam/<conversation_id>.md``. The bench watcher picks
it up and runs the same production extraction pipeline that ingests any
other document (same chunker, same fact extraction, same wiki maintainer).

The probing-questions JSON (the actual benchmark questions + rubrics) is
NOT ingested — those are loaded separately by the bench runner and
asked via ``/api/v1/agent/query`` after the warmup barrier passes.

Also exposes a small helper API the runner imports:

    iter_conversations(split: str) -> Iterator[Conversation]
    write_conversation_md(conv, output_dir: Path) -> Path
    load_probing_questions(conv) -> dict[str, list[dict]]

CLI:

    python -m benchmarks.beam.adapter --split 1M --output data_bench/sources/beam/
    python -m benchmarks.beam.adapter --split 1M --index 0   # dry-run, prints to stdout
"""
from __future__ import annotations

import argparse
import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = REPO_ROOT / "benchmarks" / "beam" / "dataset_cache"
DEFAULT_OUTPUT = REPO_ROOT / "data_bench" / "sources" / "beam"


@dataclass
class Conversation:
    """One BEAM row, surfaced as a small typed wrapper."""
    split: str                   # "100K" / "500K" / "1M"
    index: int                   # row index within the split
    conversation_id: str         # BEAM's own id (e.g. "1")
    raw: dict                    # the underlying HF row

    @property
    def slug(self) -> str:
        """Stable filename component, e.g. 'beam_1m_conv_001'."""
        return f"beam_{self.split.lower()}_conv_{int(self.conversation_id):03d}"


def _hf_env() -> None:
    """Point HuggingFace at the gitignored bench cache."""
    cache = str(DEFAULT_CACHE)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_DATASETS_CACHE", cache)
    os.environ.setdefault("HF_HOME", cache)


def iter_conversations(split: str = "1M") -> Iterator[Conversation]:
    """Yield each conversation in the requested BEAM split."""
    _hf_env()
    from datasets import load_dataset  # type: ignore
    ds = load_dataset("Mohammadta/BEAM")
    if split not in ds:
        raise ValueError(f"unknown split {split!r}; available: {list(ds.keys())}")
    for i, row in enumerate(ds[split]):
        yield Conversation(
            split=split,
            index=i,
            conversation_id=str(row["conversation_id"]),
            raw=row,
        )


def load_probing_questions(conv: Conversation) -> dict[str, list[dict]]:
    """Parse ``probing_questions`` field (Python-dict-as-string) into a
    plain dict keyed by category (abstention, multi_session_reasoning, etc.).
    """
    pq = conv.raw["probing_questions"]
    if isinstance(pq, dict):
        return pq
    return ast.literal_eval(pq)


def _user_profile_summary(profile: dict) -> str:
    """Extract a short human-readable summary from BEAM's user_profile dict.
    The dict contains keys like 'user_info' with multi-line strings; we keep
    the first ~10 lines so the agent has persona context without the markdown
    file becoming dominated by profile prose.
    """
    info = profile.get("user_info", "") if isinstance(profile, dict) else str(profile)
    lines = [ln.rstrip() for ln in info.split("\n") if ln.strip()][:12]
    return "\n".join(lines)


def _render_message(msg: dict) -> str:
    """Render one chat message as a markdown turn."""
    role = msg.get("role", "unknown").lower()
    content = (msg.get("content") or "").rstrip()
    # BEAM messages have 'time_anchor' per message; we surface it on user
    # turns only, to anchor temporal-reasoning questions without doubling
    # the prefix on every assistant reply.
    ts = msg.get("time_anchor")
    if role == "user":
        header = f"**User** ({ts}):" if ts else "**User:**"
    elif role == "assistant":
        header = "**Assistant:**"
    else:
        header = f"**{role.title()}:**"
    return f"{header}\n\n{content}"


def render_conversation_md(conv: Conversation) -> str:
    """Render the entire conversation as a single markdown document.

    Format: a short frontmatter-style header (id, category, subtopics,
    profile snippet), then one section per batch (with the batch's time
    anchor), then all messages in that batch as alternating User/Assistant
    blocks. This is the SAME shape ingested by BrainDB's watcher in
    production — no benchmark-only extraction prompts, no special handling.
    """
    raw = conv.raw
    seed = raw.get("conversation_seed") or {}
    profile = raw.get("user_profile") or {}
    chat_batches = raw.get("chat") or []

    parts: list[str] = []
    parts.append(f"# BEAM conversation {conv.slug}")
    parts.append("")
    parts.append(f"- conversation_id: {conv.conversation_id}")
    parts.append(f"- split: {conv.split}")
    if seed.get("category"):
        parts.append(f"- category: {seed['category']}")
    subtopics = seed.get("subtopics") or []
    if subtopics:
        parts.append(f"- subtopics: {', '.join(map(str, subtopics))}")
    profile_text = _user_profile_summary(profile)
    if profile_text:
        parts.append("")
        parts.append("## User profile")
        parts.append("")
        parts.append(profile_text)

    for batch_idx, batch in enumerate(chat_batches, start=1):
        if not batch:
            continue
        ts = batch[0].get("time_anchor") if isinstance(batch[0], dict) else None
        parts.append("")
        parts.append(f"## Batch {batch_idx}" + (f" — {ts}" if ts else ""))
        parts.append("")
        for msg in batch:
            if not isinstance(msg, dict) or not (msg.get("content") or "").strip():
                continue
            parts.append(_render_message(msg))
            parts.append("")

    # Ensure file ends with a single trailing newline
    text = "\n".join(parts).rstrip() + "\n"
    # Normalise stray Windows-style line endings just in case the dataset
    # carries any (won't change semantics; keeps the watcher's chunker happy)
    text = re.sub(r"\r\n?", "\n", text)
    return text


def write_conversation_md(conv: Conversation, output_dir: Path = DEFAULT_OUTPUT) -> Path:
    """Render and write one conversation; return the file path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{conv.slug}.md"
    text = render_conversation_md(conv)
    out_path.write_text(text, encoding="utf-8")
    return out_path


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", default="1M", choices=["100K", "500K", "1M"])
    p.add_argument("--output", default=str(DEFAULT_OUTPUT),
                   help=f"output directory (default: {DEFAULT_OUTPUT})")
    p.add_argument("--index", type=int, default=None,
                   help="if set, dry-run: render only this row index to stdout, do not write")
    p.add_argument("--limit", type=int, default=None,
                   help="if set, write only the first N conversations from the split")
    args = p.parse_args()

    out_dir = Path(args.output)

    if args.index is not None:
        # Dry run: print one rendered conversation to stdout.
        convs = list(iter_conversations(args.split))
        if args.index >= len(convs):
            print(f"index {args.index} out of range for split {args.split} (size {len(convs)})")
            return 1
        text = render_conversation_md(convs[args.index])
        print(text)
        return 0

    count = 0
    for conv in iter_conversations(args.split):
        if args.limit is not None and count >= args.limit:
            break
        path = write_conversation_md(conv, out_dir)
        size_kb = path.stat().st_size / 1024
        print(f"wrote {path} ({size_kb:.1f} KB)")
        count += 1
    print(f"\nwrote {count} conversation(s) to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

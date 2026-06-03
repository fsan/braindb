"""
Locks in that the watcher's prompts NO LONGER dictate parameter values
to the LLM. Before this round, every fact extracted from every document
got the same `certainty=0.8`, `importance=0.6`, `relevance_score=0.9`
because the watcher's prompt copy-pasted those numbers into the
example call the LLM was told to make. That hid the LLM's judgment.

This is a static regex check on the prompt-builder source — no LLM
call, no DB hit, deterministic. The live variance check is performed
manually with a user-chosen file after deploy.
"""
from pathlib import Path

import re


WATCHER_PATH = Path(__file__).resolve().parents[1] / "braindb" / "ingest_watcher.py"


def _watcher_source() -> str:
    return WATCHER_PATH.read_text(encoding="utf-8")


def test_chunk_extraction_prompt_does_not_dictate_certainty():
    src = _watcher_source()
    # Locate the chunk-extraction prompt block.
    # The bug was a literal "certainty=0.8" embedded in the prompt string.
    matches = re.findall(r'["\'][^"\']*certainty=0\.[0-9][^"\']*["\']', src)
    assert not matches, (
        "Watcher prompt still dictates a certainty literal to the LLM: "
        f"{matches}. The LLM should judge certainty per save_fact / "
        "save_thought docstring instead."
    )


def test_chunk_extraction_prompt_does_not_dictate_importance():
    src = _watcher_source()
    matches = re.findall(r'["\'][^"\']*importance=0\.[0-9][^"\']*["\']', src)
    # Note: the watcher's datasource ingest body (importance=0.6) is fine
    # — that's a Python dict literal, NOT inside a prompt string. The
    # regex above looks specifically for the pattern inside a quoted
    # prompt-text segment.
    assert not matches, (
        "Watcher prompt still dictates an importance literal to the LLM: "
        f"{matches}. The LLM should judge importance per the tool docstring."
    )


def test_chunk_extraction_prompt_does_not_dictate_relevance_score():
    src = _watcher_source()
    matches = re.findall(r'["\'][^"\']*relevance_score=0\.[0-9][^"\']*["\']', src)
    assert not matches, (
        "Watcher prompt still dictates a relevance_score literal to the LLM: "
        f"{matches}. The LLM should judge relevance_score per create_relation's docstring."
    )

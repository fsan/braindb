"""Unit tests for `braindb.services.wiki_sections` — the pure parsing and
splicing layer behind the writer's section-edit tools.

These tests cover the DB-free functions only (`parse_sections`,
`splice_section`, `delete_section`, `check_grammar`). The DB helpers
(`fetch_wiki_for_section_op`, `apply_section_write`) are covered by
the end-to-end smoke test inside `braindb_api` (see plan Phase 1).

The contract being tested:

- `parse_sections(body)` returns `(header, [Section(name, content)])`.
  Sections are split on `<!-- section:NAME -->` markers; the header
  is everything before the first marker.
- `splice_section` REPLACES an existing section's content, or APPENDS
  a fresh section if the name is new. Bytes outside the targeted
  section are preserved exactly.
- `delete_section` removes a section, raises `KeyError` if missing.
- `check_grammar` flags: no markers, malformed `[[ref:` tokens, missing
  Summary callout. Tolerates the grouped-refs variant `[[ref:UUID1],
  [ref:UUID2]]` documented in the wiki frontend plan.
- Round-trip identity: parse → splice (with same content) → string is
  byte-identical to the input when the input is itself in normal form.
"""
from __future__ import annotations

import pytest

from braindb.services.wiki_sections import (
    Section,
    StaleRevisionError,
    check_grammar,
    delete_section,
    parse_sections,
    splice_section,
)

UUID_A = "11111111-1111-1111-1111-111111111111"
UUID_B = "22222222-2222-2222-2222-222222222222"

# A minimal but realistic body in normal form (matches the writer
# prompt's "Recommended structure"). Used as the baseline for splice +
# roundtrip tests.
NORMAL_BODY = (
    "<!-- wiki:meta canonical_name=Test language=en revision=1 -->\n"
    "# Test\n"
    "> **Summary:** one line\n"
    "> **Disambiguation:** what this is\n"
    f"<!-- section:overview -->\n"
    f"opening prose [[ref:{UUID_A}]]\n"
    "<!-- section:timeline -->\n"
    f"2026 — event [[ref:{UUID_B}]]\n"
    "<!-- section:references -->\n"
    f"- [[ref:{UUID_A}]] — source A\n"
    f"- [[ref:{UUID_B}]] — source B\n"
)


# ====================================================================== #
# parse_sections                                                          #
# ====================================================================== #

def test_parse_sections_extracts_each_section_in_order():
    header, sections = parse_sections(NORMAL_BODY)
    names = [s.name for s in sections]
    assert names == ["overview", "timeline", "references"]


def test_parse_sections_preserves_header_verbatim():
    header, _ = parse_sections(NORMAL_BODY)
    assert header.startswith("<!-- wiki:meta")
    assert "# Test" in header
    assert "> **Summary:**" in header
    # header ends at (not after) the first marker
    assert "<!-- section:" not in header


def test_parse_sections_section_content_excludes_marker_line():
    _, sections = parse_sections(NORMAL_BODY)
    overview = next(s for s in sections if s.name == "overview")
    assert overview.content.startswith("opening prose ")
    assert "<!-- section:" not in overview.content


def test_parse_sections_no_markers_returns_empty_sections():
    body = "just plain text with no markers\n"
    header, sections = parse_sections(body)
    assert header == body
    assert sections == []


def test_parse_sections_char_count_is_content_length():
    _, sections = parse_sections(NORMAL_BODY)
    assert all(s.char_count == len(s.content) for s in sections)


# ====================================================================== #
# splice_section — replace existing                                       #
# ====================================================================== #

def test_splice_replace_existing_section():
    new = splice_section(NORMAL_BODY, "overview", "rewritten prose")
    _, sections = parse_sections(new)
    overview = next(s for s in sections if s.name == "overview")
    assert "rewritten prose" in overview.content
    # Other sections untouched
    timeline = next(s for s in sections if s.name == "timeline")
    assert "2026 — event" in timeline.content


def test_splice_replace_preserves_header():
    original_header, _ = parse_sections(NORMAL_BODY)
    new = splice_section(NORMAL_BODY, "overview", "rewritten")
    new_header, _ = parse_sections(new)
    assert new_header == original_header


def test_splice_replace_preserves_section_order():
    new = splice_section(NORMAL_BODY, "timeline", "new timeline")
    _, sections = parse_sections(new)
    assert [s.name for s in sections] == ["overview", "timeline", "references"]


# ====================================================================== #
# splice_section — append new section                                     #
# ====================================================================== #

def test_splice_append_new_section_when_name_missing():
    new = splice_section(NORMAL_BODY, "roadmap", "Q3 2026 plans")
    _, sections = parse_sections(new)
    assert "roadmap" in [s.name for s in sections]
    # appended at the END
    assert sections[-1].name == "roadmap"
    assert "Q3 2026 plans" in sections[-1].content


def test_splice_append_does_not_disturb_existing_sections():
    new = splice_section(NORMAL_BODY, "roadmap", "future")
    _, sections = parse_sections(new)
    # original 3 sections still present in same order
    original_names = ["overview", "timeline", "references"]
    assert [s.name for s in sections][:3] == original_names


# ====================================================================== #
# delete_section                                                          #
# ====================================================================== #

def test_delete_section_removes_named_section():
    new = delete_section(NORMAL_BODY, "timeline")
    _, sections = parse_sections(new)
    names = [s.name for s in sections]
    assert "timeline" not in names
    assert names == ["overview", "references"]


def test_delete_section_raises_keyerror_for_missing():
    with pytest.raises(KeyError):
        delete_section(NORMAL_BODY, "nonexistent")


def test_delete_section_preserves_header():
    original_header, _ = parse_sections(NORMAL_BODY)
    new = delete_section(NORMAL_BODY, "timeline")
    new_header, _ = parse_sections(new)
    assert new_header == original_header


# ====================================================================== #
# Round-trip identity                                                     #
# ====================================================================== #

def test_roundtrip_identity_on_normal_body():
    """Splicing a section with its own content must produce a body that
    is byte-identical to the input. This is the strongest proof that
    the parser + rebuilder are self-consistent — no drift, no marker
    corruption."""
    _, sections = parse_sections(NORMAL_BODY)
    overview = next(s for s in sections if s.name == "overview")
    roundtrip = splice_section(
        NORMAL_BODY, "overview", overview.content.rstrip("\n"),
    )
    assert roundtrip == NORMAL_BODY


# ====================================================================== #
# check_grammar                                                           #
# ====================================================================== #

def test_grammar_clean_body_passes():
    assert check_grammar(NORMAL_BODY) == []


def test_grammar_flags_missing_markers():
    body = "# Test\n> **Summary:** s\nNo markers here.\n"
    issues = check_grammar(body)
    assert any("no <!-- section:" in i for i in issues)


def test_grammar_flags_missing_summary():
    body = (
        "<!-- wiki:meta canonical_name=X -->\n"
        "# X\n"
        "<!-- section:overview -->\n"
        "no summary callout above\n"
    )
    issues = check_grammar(body)
    assert any("> **Summary:**" in i for i in issues)


def test_grammar_tolerates_grouped_refs():
    """The grouped form `[[ref:UUID1], [ref:UUID2]]` is documented in the
    wiki frontend plan as a real-world variant the renderer accepts.
    check_grammar must not flag it as malformed."""
    body = (
        "<!-- wiki:meta canonical_name=X -->\n"
        "# X\n"
        "> **Summary:** s\n"
        "<!-- section:overview -->\n"
        f"grouped citation [[ref:{UUID_A}], [ref:{UUID_B}]] in text\n"
    )
    issues = check_grammar(body)
    # No malformed-ref complaints (the only issue could be summary, but
    # we included it)
    assert not any("malformed" in i for i in issues), issues


def test_grammar_flags_truly_broken_ref():
    body = (
        "<!-- wiki:meta canonical_name=X -->\n"
        "# X\n"
        "> **Summary:** s\n"
        "<!-- section:overview -->\n"
        "broken ref [[ref:not-a-uuid]] here\n"
    )
    issues = check_grammar(body)
    assert any("malformed" in i for i in issues), issues


# ====================================================================== #
# StaleRevisionError class                                                #
# ====================================================================== #

def test_stale_revision_error_is_exception():
    """The DB helpers raise this when expect_revision mismatches the
    current DB revision. The tool wrappers translate it into a string
    error the LLM can read; the class itself is the integration point."""
    assert issubclass(StaleRevisionError, Exception)
    err = StaleRevisionError("expected 5, current 6")
    assert "5" in str(err) and "6" in str(err)


# ====================================================================== #
# Section dataclass                                                       #
# ====================================================================== #

def test_section_is_frozen_dataclass():
    s = Section(name="x", content="y")
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        s.name = "z"  # type: ignore[misc]


def test_section_char_count_property():
    s = Section(name="x", content="abcdef")
    assert s.char_count == 6

"""
Typed agent output contract.

Convention (absolute): every agent/subagent finishes via the `final_answer`
trick, and its payload is ALWAYS one of these Pydantic models — never a loose
free string we scrape. `@function_tool` turns the model into a strict JSON
schema for the tool arguments, so the LLM is constrained to emit valid
structured output instead of free-running and truncating.

These mirror the style of `braindb/schemas/` (the REST layer); they reuse the
existing pydantic dependency — no new dependency, no new machinery.
"""
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# Coercion helpers — weak/quantised models often emit "" (empty string) for
# nullable fields instead of `null`, or `null` for empty-list fields instead
# of `[]`. The Pydantic schemas are nullable + defaulted at the type level;
# these `before` validators just accept the wrong-type variants gracefully
# so we don't reject a perfectly intended "skip" decision because the model
# sent `target_wiki_no=""` instead of `null`. The validation contract is
# unchanged — we still produce a properly-typed Pydantic instance.

def _coerce_empty_to_none(v):
    """Accept '', 'null', 'none', 'n/a' (any case, with/without whitespace)
    as equivalent to None for nullable fields."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in ("null", "none", "n/a"):
            return None
    return v


def _coerce_to_int_or_none(v):
    """For nullable-int fields: '' / 'null' / etc → None; numeric strings → int."""
    v = _coerce_empty_to_none(v)
    if v is None or isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None  # last resort — don't fail the whole submission on a bad number


def _coerce_to_list(v):
    """For list fields: None / '' → []; everything else as-is for Pydantic to validate."""
    if v is None or v == "":
        return []
    return v


class AgentAnswer(BaseModel):
    """General recall/save answer (the public /agent/query endpoint).

    The endpoint is general-purpose (Claude Code, arbitrary recall/save), so
    the answer itself is necessarily natural language — but it is still
    delivered through the typed `final_answer` trick, never as loose
    top-level model output.
    """
    answer: str = Field(..., description="The full natural-language response to the caller.")


class MaintainerDecision(BaseModel):
    """The wiki maintainer's per-orphan decision. Existing wikis are
    referenced by their CATALOG NUMBER (the numbered list at the end of the
    prompt), never by uuid — the harness maps number->id deterministically.

    Action-dependent fields: `target_wiki_no`, `proposed_name`, and
    `consolidate_nos` are only meaningful for one specific action each. For
    every other action you MUST send JSON `null` for the optional ones (not
    "", not 0, not "n/a") and an empty array `[]` for `consolidate_nos`.
    """
    action: Literal["attach", "create", "consolidate", "skip", "ambiguous"] = Field(
        ...,
        description=(
            "The decision for this orphan. Exactly one of: "
            "`attach` (link to an existing wiki by catalog number), "
            "`create` (mint a new wiki with a proposed name), "
            "`consolidate` (merge >=2 catalog-numbered wikis), "
            "`skip` (not worth a wiki — infrastructural / keyword-token), "
            "`ambiguous` (cannot disambiguate the real subject)."
        ),
    )
    target_wiki_no: int | None = Field(
        None,
        description=(
            "REQUIRED ONLY when action=`attach`: the integer CATALOG NUMBER "
            "of the existing wiki to attach the orphan to (1-indexed, taken "
            "from the numbered WIKIS list at the end of the prompt). "
            "For action in (`create`, `consolidate`, `skip`, `ambiguous`) "
            "this field MUST be JSON null. Do NOT use empty string \"\", 0, "
            "or 'n/a' — use literal null."
        ),
    )
    proposed_name: str | None = Field(
        None,
        description=(
            "REQUIRED ONLY when action=`create`: the canonical name for the "
            "new wiki (must appear in the evidence — never invent). "
            "For action in (`attach`, `consolidate`, `skip`, `ambiguous`) "
            "this field MUST be JSON null. Do NOT use empty string \"\"."
        ),
    )
    consolidate_nos: list[int] = Field(
        default_factory=list,
        description=(
            "REQUIRED ONLY when action=`consolidate`: an array of >=2 "
            "integer CATALOG NUMBERS naming the duplicate wikis to merge "
            "(from the numbered WIKIS list). "
            "For every other action this field MUST be an empty array [] "
            "(NOT null, NOT empty string)."
        ),
    )
    rationale: str = Field(
        ...,
        description=(
            "ALWAYS REQUIRED. One to three sentences justifying the chosen "
            "action: which catalog wiki(s) you matched (or that the catalog "
            "has none), and why this action was the right one. This makes "
            "the decision auditable."
        ),
    )

    # Forgiving coercion — weak/quantised models often emit empty strings or
    # "null" strings instead of literal JSON null. Accept those as None
    # rather than rejecting the whole submission (the prompt and the
    # descriptions above ask for null; the validators are the safety net).
    @field_validator("target_wiki_no", mode="before")
    @classmethod
    def _coerce_target_wiki_no(cls, v):
        return _coerce_to_int_or_none(v)

    @field_validator("proposed_name", mode="before")
    @classmethod
    def _coerce_proposed_name(cls, v):
        return _coerce_empty_to_none(v)

    @field_validator("consolidate_nos", mode="before")
    @classmethod
    def _coerce_consolidate_nos(cls, v):
        return _coerce_to_list(v)


class WikiWriteResult(BaseModel):
    """The wiki writer's full output. `body` is the complete markdown page —
    a typed field of the schema, exactly like any other field (not loose
    text, not delimiter-wrapped).

    `canonical_no` is only meaningful for `consolidate` mode. For
    `create` / `attach` you MUST send JSON null (not "", not 0).
    """
    mode: Literal["create", "attach", "consolidate"] = Field(
        ...,
        description=(
            "The write mode of THIS job (matches the mode the harness "
            "passed in the prompt): `create` (fresh wiki), `attach` "
            "(integrate new members into an existing wiki), `consolidate` "
            "(merge multiple duplicate wikis into a survivor)."
        ),
    )
    canonical_no: int | None = Field(
        None,
        description=(
            "REQUIRED ONLY when mode=`consolidate`: the integer NUMBER of "
            "the surviving wiki chosen from the numbered DUPLICATES list "
            "in the prompt (1-indexed, never a uuid). "
            "For mode in (`create`, `attach`) this field MUST be JSON null. "
            "Do NOT use empty string \"\", 0, or 'n/a'."
        ),
    )
    body: str = Field(
        ...,
        description=(
            "The COMPLETE markdown wiki page — the full document. Include "
            "the meta header, summary, disambiguation, every section, all "
            "[[ref:UUID]] citations, and the references section. This is "
            "what becomes the wiki entity's content; it replaces the prior "
            "body wholesale (the prior version is auto-snapshotted)."
        ),
    )

    @field_validator("canonical_no", mode="before")
    @classmethod
    def _coerce_canonical_no(cls, v):
        return _coerce_to_int_or_none(v)


class SubagentResult(BaseModel):
    """A delegated subagent's return (replaces the free-string subagent answer)."""
    result: str = Field(..., description="The distilled result of the delegated task.")

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

from pydantic import BaseModel, Field


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
    """
    action: Literal["attach", "create", "consolidate", "skip", "ambiguous"]
    target_wiki_no: int | None = Field(
        None,
        description="attach: the CATALOG NUMBER of the existing wiki to "
                    "attach the orphan to (from the numbered WIKIS list at "
                    "the end of the prompt). Null otherwise.")
    proposed_name: str | None = Field(
        None, description="create: the canonical name for the new wiki.")
    consolidate_nos: list[int] = Field(
        default_factory=list,
        description="consolidate: the CATALOG NUMBERS (>=2) of the duplicate "
                    "wikis to merge (from the numbered WIKIS list). Empty "
                    "otherwise.")
    rationale: str = Field(..., description="One to three sentences justifying the action.")


class WikiWriteResult(BaseModel):
    """The wiki writer's full output. `body` is the complete markdown page —
    a typed field of the schema, exactly like any other field (not loose
    text, not delimiter-wrapped)."""
    mode: Literal["create", "attach", "consolidate"]
    canonical_no: int | None = Field(
        None,
        description="consolidate ONLY: the NUMBER of the surviving wiki "
                    "chosen from the numbered duplicates list in the prompt "
                    "(never an id). Null for create/attach.")
    body: str = Field(..., description="The complete markdown wiki page.")


class SubagentResult(BaseModel):
    """A delegated subagent's return (replaces the free-string subagent answer)."""
    result: str = Field(..., description="The distilled result of the delegated task.")

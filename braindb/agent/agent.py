"""
BrainDB internal agent — builder and runners.

Convention (absolute): every agent run finishes via the `final_answer`
trick, and that tool's argument is ALWAYS a typed Pydantic model. The LLM
never emits loose / free-form output we then scrape.

There is one agent per purpose, differing only by which typed
`submit_*` variant it carries (all named "final_answer" so prompts and
`StopAtTools(["final_answer"])` stay generic). The structured contract
lives on the **tool argument schema** (`@function_tool` + Pydantic),
which is what the user wanted: validated final answer, free middle
turns. We deliberately do NOT set `output_type` on the Agent — that flag
makes the SDK pass `response_format: json_schema` on every LLM call,
which steers weaker models to satisfy the schema on turn 1 and never
call any tool (the regression we are fixing).

How we still recover the typed payload: each `submit_*` tool body parks
its already-validated `payload` into `braindb.agent.run_state.last_submit`
(a ContextVar). `run_typed` reads it back after `Runner.run` returns.
asyncio's per-Task context isolation makes nested/parallel runs safe.
"""
import logging
from pathlib import Path
from typing import TypeVar

from agents import Agent, ModelSettings, Runner, StopAtTools, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

from braindb.agent.hooks import CountdownHooks
from braindb.agent.run_state import install_slot, release_slot
from braindb.agent.schemas import (
    AgentAnswer,
    MaintainerDecision,
    SubagentResult,
    WikiWriteResult,
)
from braindb.agent.tools import (
    create_relation,
    delegate_to_subagent,
    delete_entity,
    delete_relation,
    generate_embeddings,
    get_entity,
    get_stats,
    ingest_file,
    list_entities,
    quick_search,
    recall_memory,
    save_fact,
    save_rule,
    save_source,
    save_thought,
    search_sql,
    submit_answer,
    submit_maintainer,
    submit_subagent,
    submit_wiki,
    update_entity,
    view_entity_relations,
    view_log,
    view_tree,
)
from braindb.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system_prompt.md").read_text(encoding="utf-8")

# Every tool except the final submit (that one is typed per purpose).
_BASE_TOOLS = [
    recall_memory,
    quick_search,
    save_fact,
    save_thought,
    save_source,
    save_rule,
    ingest_file,
    get_entity,
    list_entities,
    update_entity,
    delete_entity,
    create_relation,
    view_entity_relations,
    delete_relation,
    view_tree,
    search_sql,
    view_log,
    get_stats,
    generate_embeddings,
    delegate_to_subagent,
]

T = TypeVar("T")


def _model() -> LitellmModel:
    return LitellmModel(
        model=settings.resolved_agent_model,
        api_key=settings.resolved_api_key,
        base_url=settings.resolved_base_url,
    )


def _build(name: str, submit_tool) -> Agent:
    """Build an agent. NOTE: no `output_type` — see module docstring. The
    structured contract lives on `submit_tool`'s argument schema, not on
    the agent."""
    set_tracing_disabled(disabled=True)
    agent = Agent(
        name=name,
        instructions=SYSTEM_PROMPT,
        model=_model(),
        model_settings=ModelSettings(),
        tools=[*_BASE_TOOLS, submit_tool],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["final_answer"]),
    )
    logger.info(
        "Agent built: %s (model=%s) — free middle turns, typed final_answer",
        name, settings.resolved_agent_model,
    )
    return agent


_cache: dict[str, Agent] = {}


def _cached(key: str, name: str, submit_tool) -> Agent:
    a = _cache.get(key)
    if a is None:
        a = _build(name, submit_tool)
        _cache[key] = a
    return a


def get_agent() -> Agent:
    """Default agent: general recall/save (public /agent/query)."""
    return _cached("answer", "BrainDB Memory Agent", submit_answer)


def get_maintainer_agent() -> Agent:
    return _cached("maintainer", "BrainDB Wiki Maintainer", submit_maintainer)


def get_writer_agent() -> Agent:
    return _cached("writer", "BrainDB Wiki Writer", submit_wiki)


def get_subagent() -> Agent:
    return _cached("subagent", "BrainDB Subagent", submit_subagent)


def create_braindb_agent() -> Agent:
    """Backward-compat alias — the default (general) agent."""
    return get_agent()


async def run_typed(
    query: str,
    agent: Agent,
    expected_cls: type[T],
    max_turns: int | None = None,
) -> T:
    """Run a typed agent and return the validated Pydantic instance it
    submitted. The instance is guaranteed-valid because the SDK validates
    the LLM's `final_answer` call args against `expected_cls` BEFORE the
    tool body runs (via `@function_tool`'s strict JSON schema).

    Raises `RuntimeError` if the run ends without `final_answer` firing
    (e.g. `max_turns` exhausted) — surfaces a real model failure instead
    of silently returning bad data. Routers handle this like any other
    agent error: log + release the job lease + 5xx.
    """
    turns = max_turns or settings.agent_max_turns
    slot, token = install_slot()
    # Layer-3 nudge: when the run is about to exhaust `max_turns`, the hook
    # appends a synthetic "you have N turns left, finalise via final_answer"
    # user message to the conversation. One nudge per run; disabled when
    # `agent_countdown_threshold == 0`. See braindb/agent/hooks.py.
    hooks = CountdownHooks(
        max_turns=turns,
        threshold=settings.agent_countdown_threshold,
        tool_name="final_answer",
    )
    try:
        logger.info("Running typed query (%s): %s", agent.name, query[:160])
        await Runner.run(starting_agent=agent, input=query, max_turns=turns, hooks=hooks)
        payload = slot.value
        if not isinstance(payload, expected_cls):
            # NOTE: this fires whenever `Runner.run` returns and no `submit_*`
            # tool was called. The two real causes are (a) the model ended
            # the run by emitting plain prose with no tool call (the SDK
            # terminates naturally at that point) and (b) the SDK hit its
            # own max_turns guard. The SDK raises `MaxTurnsExceeded`
            # separately for (b), so by the time we get here it is almost
            # always (a) — a model-discipline failure on the final turn.
            raise RuntimeError(
                f"{agent.name} did not call final_answer with a "
                f"{expected_cls.__name__} (got {type(payload).__name__}). "
                f"The run terminated without the typed final tool firing — "
                f"the model likely ended with plain prose."
            )
        return payload
    finally:
        release_slot(token)


async def run_agent_query(query: str, max_turns: int | None = None) -> dict:
    """General recall/save path (public /agent/query, and the ingest watcher
    over HTTP). The model finishes via `final_answer(payload: AgentAnswer)`;
    the response shape stays `{"answer","max_turns"}` for backward
    compatibility."""
    turns = max_turns or settings.agent_max_turns
    payload: AgentAnswer = await run_typed(query, get_agent(), AgentAnswer, max_turns=turns)
    return {"answer": payload.answer, "max_turns": turns}

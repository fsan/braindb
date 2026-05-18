"""
BrainDB internal agent — builder and runners.

Convention (absolute): every agent run finishes via the `submit_result`
trick, and that tool's argument is ALWAYS a typed Pydantic model. The LLM
never emits loose / free-form output we then scrape.

There is one agent per purpose, differing only by (a) which typed
`submit_result` variant it carries and (b) its `output_type` (the matching
Pydantic model). `output_type` is load-bearing: with `StopAtTools` the SDK
str()-coerces the stop-tool's return UNLESS `output_type` is a non-str type
(see agents/run_internal/turn_resolution.py) — so setting it keeps the
validated model object as `final_output`. All variants keep the tool name
"submit_result" so prompts and `StopAtTools(["submit_result"])` stay generic.
"""
import logging
from pathlib import Path
from typing import Any

from agents import Agent, ModelSettings, Runner, StopAtTools, set_tracing_disabled
from agents.extensions.models.litellm_model import LitellmModel

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


def _model() -> LitellmModel:
    return LitellmModel(
        model=settings.resolved_agent_model,
        api_key=settings.resolved_api_key,
        base_url=settings.resolved_base_url,
    )


def _build(name: str, submit_tool, output_model) -> Agent:
    set_tracing_disabled(disabled=True)
    agent = Agent(
        name=name,
        instructions=SYSTEM_PROMPT,
        model=_model(),
        model_settings=ModelSettings(),
        tools=[*_BASE_TOOLS, submit_tool],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["submit_result"]),
        output_type=output_model,
    )
    logger.info(
        "Agent built: %s (output=%s, model=%s)",
        name, output_model.__name__, settings.resolved_agent_model,
    )
    return agent


_cache: dict[str, Agent] = {}


def _cached(key: str, name: str, submit_tool, output_model) -> Agent:
    a = _cache.get(key)
    if a is None:
        a = _build(name, submit_tool, output_model)
        _cache[key] = a
    return a


def get_agent() -> Agent:
    """Default agent: general recall/save (public /agent/query)."""
    return _cached("answer", "BrainDB Memory Agent", submit_answer, AgentAnswer)


def get_maintainer_agent() -> Agent:
    return _cached("maintainer", "BrainDB Wiki Maintainer", submit_maintainer, MaintainerDecision)


def get_writer_agent() -> Agent:
    return _cached("writer", "BrainDB Wiki Writer", submit_wiki, WikiWriteResult)


def get_subagent() -> Agent:
    return _cached("subagent", "BrainDB Subagent", submit_subagent, SubagentResult)


def create_braindb_agent() -> Agent:
    """Backward-compat alias — the default (general) agent."""
    return get_agent()


async def run_typed(query: str, agent: Agent, max_turns: int | None = None) -> Any:
    """Run a query through a typed agent. Returns the validated Pydantic model
    the agent's `submit_result` produced (its `output_type`)."""
    turns = max_turns or settings.agent_max_turns
    logger.info("Running typed query (%s): %s", agent.name, query[:160])
    result = await Runner.run(starting_agent=agent, input=query, max_turns=turns)
    return result.final_output


async def run_agent_query(query: str, max_turns: int | None = None) -> dict:
    """General recall/save path (public /agent/query, and the ingest watcher
    over HTTP). The model still finishes via the typed `submit_result`
    (AgentAnswer); the response shape stays {"answer","max_turns"} for
    backward compatibility."""
    turns = max_turns or settings.agent_max_turns
    fo = await run_typed(query, get_agent(), max_turns=turns)
    answer = fo.answer if isinstance(fo, AgentAnswer) else str(fo)
    return {"answer": answer, "max_turns": turns}

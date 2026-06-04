"""
Agent endpoint — POST /api/v1/agent/query

External callers (Claude Code, other tools) send a natural language query;
the BrainDB agent (LiteLLM + NVIDIA NIM) handles recall/save/relate via
its internal tools and returns a summary.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from braindb.agent.agent import run_agent_query
from braindb.db import get_conn
from braindb.services.activity_log import log_activity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class AgentQueryRequest(BaseModel):
    # Bumped 10000 -> 40000 to accommodate the ingest watcher's per-chunk
    # extraction prompts at the larger CHUNK_WORDS=1200 size (chunk text
    # ~6 KB + boilerplate + cross-fact-relation instructions ~7.5 KB total,
    # well under the new cap). Sized so that even at 40K input chars
    # (~10K tokens) the LLM has plenty of headroom: Qwen 27B runs at
    # max_model_len=40960, so input + system prompt + tool defs +
    # ~35-turn tool-call iteration leaves ~30% of the window for output.
    query: str = Field(..., min_length=1, max_length=40000)
    max_turns: int | None = Field(default=None, ge=1, le=60)


@router.post("/query")
async def agent_query(body: AgentQueryRequest):
    """Run a natural-language query through the BrainDB agent.

    When AGENT_VERBOSE=true is set in the server environment, every tool call
    is logged to stdout and visible via `docker logs braindb_api`.
    """
    try:
        result = await run_agent_query(body.query, max_turns=body.max_turns)
        with get_conn() as conn:
            log_activity(conn, "agent_query", details={
                "query": body.query[:500],
                "max_turns": result.get("max_turns"),
            })
        return result
    except Exception as e:
        logger.exception("Agent query failed")
        raise HTTPException(500, f"Agent failed: {e}")

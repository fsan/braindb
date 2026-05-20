"""Runtime nudge: tell the LLM to finalise when it's about to run out of turns.

WHY this exists
---------------
The strict typed-final contract (`final_answer` tool with a Pydantic argument
schema, no `output_type` on the Agent — see `braindb/agent/agent.py`) raises a
`RuntimeError` if the model ends a run without calling `final_answer`. Weak
or quantised models sometimes over-explore (chaining `recall_memory` /
`delegate_to_subagent` calls beyond what's necessary) and reach
`max_turns` without ever submitting. The strict path correctly catches this
as a failure, but we'd rather give the model a fighting chance: shortly
before `max_turns` is exhausted, inject a chat message reminding it to
finalise.

HOW the nudge gets into the conversation
-----------------------------------------
The openai-agents SDK's `RunHooks.on_llm_start` callback (see
`agents/lifecycle.py`) receives the mutable `input_items` list that's about
to be sent to the LLM. Appending one item to that list adds a synthetic
user message visible to the model on its NEXT turn. That's the same
mechanism the SDK uses internally for any added context. We exploit it
exactly once per run (idempotent), at the configured threshold.

Knobs (see `braindb/config.py`)
- `agent_countdown_threshold` (default 5): how many turns before
  `max_turns` we start nudging. Set to 0 to disable the nudge entirely.

Design constraints
- One nudge per run (no spam).
- Defensive: any internal error in the hook is caught and logged, never
  re-raised — a future SDK shape change must not bring down agent runs.
- Pure on-LLM-start counting — no SDK-private state inspection.
"""
from __future__ import annotations

import logging
from typing import Any

from agents.lifecycle import RunHooks

logger = logging.getLogger(__name__)


class CountdownHooks(RunHooks):
    """Mutates `input_items` to inject a "you have N turns left, finalise"
    user message when the agent is close to exhausting `max_turns`.

    Lifecycle (per run):
      - constructed once with `max_turns`, `threshold`, `tool_name`.
      - `on_llm_start` fires before each LLM call; increments `_turns`.
      - when `_turns >= max_turns - threshold` AND `_fired` is False,
        flips `_fired = True` and appends ONE message to `input_items`.
      - subsequent calls are no-ops because `_fired` is True.

    Disabled when `threshold <= 0` (the hook still receives callbacks but
    never injects).
    """

    def __init__(self, max_turns: int, threshold: int, tool_name: str = "final_answer") -> None:
        self.max_turns = max_turns
        self.threshold = max(0, int(threshold))
        self.tool_name = tool_name
        self._turns: int = 0
        self._fired: bool = False

    # NOTE: `on_llm_start` is the canonical hook for injecting context
    # before the next LLM call (the SDK passes `input_items` mutably).
    # We don't override `on_tool_start` because we want to count
    # LLM-call turns, not tool calls — those can be multiple per turn.
    async def on_llm_start(
        self,
        context: Any,
        agent: Any,
        system_prompt: str | None,
        input_items: list,
    ) -> None:
        try:
            self._turns += 1
            self._maybe_inject(input_items)
        except Exception as e:  # noqa: BLE001 — defensive: never kill the run
            logger.warning(
                "CountdownHooks.on_llm_start swallowed an internal error "
                "(turns=%d, fired=%s): %r", self._turns, self._fired, e,
            )

    def _maybe_inject(self, input_items: list) -> None:
        """Pure logic: decide whether to append the nudge now. Separated so
        tests can stub it to verify the on_llm_start wrapper's
        exception-swallowing behaviour."""
        if self.threshold <= 0:
            return  # explicitly disabled
        if self._fired:
            return  # already nudged once; no spam
        remaining = self.max_turns - self._turns
        if remaining > self.threshold:
            return  # still plenty of room
        # Time to nudge. Append one synthetic user message; subsequent
        # turns will not re-inject (_fired flips).
        self._fired = True
        nudge = self._format_nudge(remaining)
        # The SDK accepts either {"role":..., "content":...} dicts or
        # ResponseInputItem instances in `input_items`. Dict form is
        # provider-portable across the LiteLLM backends we use.
        input_items.append({"role": "user", "content": nudge})
        logger.info(
            "CountdownHooks injected nudge at turn %d/%d (remaining=%d): %s",
            self._turns, self.max_turns, remaining, nudge[:120],
        )

    def _format_nudge(self, remaining: int) -> str:
        """The text the model sees. Kept short and imperative — weak models
        respond best to a single, unambiguous instruction."""
        # Clamp to non-negative for readability; if remaining went past 0
        # we still want a coherent message even though the SDK would
        # raise MaxTurnsExceeded shortly.
        remaining = max(0, remaining)
        return (
            f"You have {remaining} tool call{'s' if remaining != 1 else ''} "
            f"left before the run is forced to end. Finalise NOW by calling "
            f"`{self.tool_name}` with your answer. Do not start any new "
            f"research; deliver what you already know via `{self.tool_name}`."
        )

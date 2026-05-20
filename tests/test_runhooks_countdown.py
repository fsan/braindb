"""Edge-case tests for Stage C / Layer 3 — RunHooks countdown nudge.

The contract being tested:

- A `CountdownHooks` class lives in `braindb.agent.hooks` and subclasses
  `agents.RunHooks`. It implements `on_llm_start`, counting LLM turns and,
  when ≤ `threshold` turns remain before `max_turns`, mutating the
  `input_items` list passed to the LLM to APPEND a synthetic nudge
  reminding the model to finalise via `final_answer`.

- The nudge fires at most ONCE per run (idempotent). After firing, the
  hook does not re-inject on subsequent turns.

- The hook is defensive: a malformed `input_items` argument or any
  unexpected SDK shape change must not crash the run — exceptions are
  swallowed (and logged) so the agent loop keeps going.

- `threshold=0` disables the hook (safety hatch / opt-out).

- `max_turns < threshold` (weird config) does not crash; behaves as
  "always at threshold from turn 1" but still only fires once.

These tests instantiate the hook directly and call `on_llm_start`
synchronously via asyncio — no live LLM, no real agent loop.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from braindb.agent.hooks import CountdownHooks

EXPECTED_TOOL_NAME = "final_answer"


def _run(coro):
    """Run a single coroutine to completion. Each test gets a fresh loop."""
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.run(coro)


def _make_args(input_items: list | None = None):
    """Helper to build the args `on_llm_start` is called with. We only care
    about `input_items` (the mutable list the hook may append to); the other
    args are stubs."""
    ctx = mock.MagicMock(name="context")
    agent = mock.MagicMock(name="agent", spec=[])
    agent.name = "TestAgent"
    return ctx, agent, "system-prompt-stub", (input_items if input_items is not None else [])


@pytest.mark.asyncio
async def test_countdown_idle_when_far_from_max() -> None:
    """If we're nowhere near max_turns - threshold, the hook must not
    inject anything into input_items."""
    hooks = CountdownHooks(max_turns=20, threshold=5, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    for _ in range(3):  # 3 LLM calls, well below max_turns - threshold = 15
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    assert items == [], f"hook fired too early; items={items!r}"
    assert hooks._fired is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_countdown_fires_at_threshold() -> None:
    """When the running turn count crosses `max_turns - threshold`, the
    hook must append exactly one item to `input_items` and flip its
    fired flag."""
    max_turns, threshold = 20, 5
    hooks = CountdownHooks(max_turns=max_turns, threshold=threshold, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    # Turns 1..(max_turns - threshold - 1) must NOT fire.
    for i in range(max_turns - threshold - 1):
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    assert items == []
    # The next call crosses the threshold → fires.
    ctx, agent, sp, _ = _make_args(items)
    await hooks.on_llm_start(ctx, agent, sp, items)
    assert len(items) == 1, f"expected exactly 1 nudge appended, got {items!r}"
    nudge = items[0]
    # The nudge must mention the final-tool name; format can be dict or str.
    nudge_text = nudge.get("content") if isinstance(nudge, dict) else str(nudge)
    assert EXPECTED_TOOL_NAME in nudge_text, f"nudge missing tool name; got {nudge_text!r}"
    assert hooks._fired is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_countdown_idempotent_after_firing() -> None:
    """Once the hook has injected, subsequent on_llm_start calls must not
    add more nudges to input_items (the prior nudge is already in the
    conversation; duplicating is spam)."""
    hooks = CountdownHooks(max_turns=10, threshold=3, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    # Push past the threshold to force firing
    for _ in range(8):
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    assert hooks._fired is True  # type: ignore[attr-defined]
    nudges_after_first = len(items)
    # Several more turns — should not append again
    for _ in range(5):
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    assert len(items) == nudges_after_first, "hook re-injected on subsequent turns"


@pytest.mark.asyncio
async def test_countdown_disabled_when_threshold_zero() -> None:
    """`threshold=0` disables the hook entirely — opt-out for ops who don't
    want the nudge."""
    hooks = CountdownHooks(max_turns=10, threshold=0, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    for _ in range(50):  # Way past any reasonable max_turns
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    assert items == [], "hook fired despite threshold=0"
    assert hooks._fired is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_countdown_max_turns_below_threshold_safe() -> None:
    """Pathological config (`max_turns=3, threshold=5`) must NOT crash.
    The hook should still fire at most once and not blow up."""
    hooks = CountdownHooks(max_turns=3, threshold=5, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    for _ in range(5):
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    # The exact when-fires policy is implementation-defined; the contract is:
    # at most one nudge, no exception raised.
    assert len(items) <= 1


@pytest.mark.asyncio
async def test_countdown_does_not_break_normal_completion() -> None:
    """If the model finalises BEFORE the threshold is hit, the hook should
    not have injected anything (record-of-non-action: nothing in items)."""
    hooks = CountdownHooks(max_turns=20, threshold=5, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    # Simulate a quick agent that uses 3 turns and submits.
    for _ in range(3):
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    # No further LLM calls (agent finished). Items still empty.
    assert items == []
    assert hooks._fired is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hook_exception_does_not_kill_run() -> None:
    """Internal hook errors (e.g. SDK shape change) must be SWALLOWED so
    the agent loop can keep running. Otherwise a defensive bug in the
    hook brings down production runs."""
    hooks = CountdownHooks(max_turns=20, threshold=5, tool_name=EXPECTED_TOOL_NAME)
    items: list = []

    # Patch the internal `_maybe_inject` to blow up. The public
    # `on_llm_start` must still complete without raising.
    with mock.patch.object(hooks, "_maybe_inject", side_effect=RuntimeError("sim shape change")):
        ctx, agent, sp, _ = _make_args(items)
        try:
            await hooks.on_llm_start(ctx, agent, sp, items)
        except Exception as e:  # noqa: BLE001 — that's the point
            pytest.fail(f"on_llm_start let an exception escape: {e!r}")


# ------------------------------------------------------------------ #
# Tone-adaptive nudge wording (soft vs hard based on max_turns)        #
# ------------------------------------------------------------------ #
#
# After tuning the countdown to be friendlier on deep-research models
# (Qwen), the nudge message picks its tone from `max_turns` at
# construction time:
#   - max_turns > 5  → SOFT tone ("start wrapping up, you have N
#     left"). Used for the general /agent/query path with the default
#     max_turns=20.
#   - max_turns ≤ 5  → HARD tone ("call `final_answer` with your
#     answer now"). Used for the Layer 4 retry path with
#     max_turns=3, where the run is explicitly a single-purpose
#     "you forgot to finalise, call the tool now" correction.
#
# The tone is picked from max_turns alone (no new constructor flag)
# so call sites don't change.


@pytest.mark.asyncio
async def test_soft_tone_when_max_turns_above_threshold() -> None:
    """With a generous budget (max_turns=20, threshold=8), the nudge
    fires at turn 12 (remaining=8) and uses the soft "wrapping up"
    phrasing — NOT the hard "now" phrasing. Deep-research models
    should be allowed a few focused gap-filling calls before
    final_answer rather than forced to stop mid-thread."""
    max_turns, threshold = 20, 8
    hooks = CountdownHooks(max_turns=max_turns, threshold=threshold, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    # Burn turns up to the threshold; the next call crosses it.
    for _ in range(max_turns - threshold):
        ctx, agent, sp, _ = _make_args(items)
        await hooks.on_llm_start(ctx, agent, sp, items)
    assert len(items) == 1, f"expected exactly 1 nudge appended, got {items!r}"
    nudge_text = items[0]["content"]
    # Soft tone hallmarks
    assert "wrapping up" in nudge_text.lower(), (
        f"soft tone must contain 'wrapping up'; got {nudge_text!r}"
    )
    assert "gap-filling" in nudge_text.lower(), (
        f"soft tone must mention 'gap-filling' (the explicit allowance "
        f"for focused investigation); got {nudge_text!r}"
    )
    assert EXPECTED_TOOL_NAME in nudge_text
    # Hard-tone exclusivity: the soft message must NOT include the
    # imperative "with your answer now" phrase from the hard message.
    assert "with your answer now" not in nudge_text.lower(), (
        f"soft tone must not contain hard-tone phrase; got {nudge_text!r}"
    )


@pytest.mark.asyncio
async def test_hard_tone_when_max_turns_at_retry_budget() -> None:
    """With a tight budget (max_turns=3, the Layer 4 retry value), the
    nudge fires immediately on turn 1 (since remaining drops to ≤
    threshold right away) and uses the HARD phrasing — the retry
    context is explicitly "you forgot to finalise, call the tool
    now"; no time for soft wrapping-up framing."""
    hooks = CountdownHooks(max_turns=3, threshold=8, tool_name=EXPECTED_TOOL_NAME)
    items: list = []
    ctx, agent, sp, _ = _make_args(items)
    await hooks.on_llm_start(ctx, agent, sp, items)
    assert len(items) == 1, f"expected exactly 1 nudge; got {items!r}"
    nudge_text = items[0]["content"]
    # Hard tone hallmarks
    assert "with your answer now" in nudge_text.lower(), (
        f"hard tone must contain 'with your answer now'; got {nudge_text!r}"
    )
    assert EXPECTED_TOOL_NAME in nudge_text
    # Soft-tone exclusivity: the hard message must NOT include the
    # "wrapping up" softening phrase.
    assert "wrapping up" not in nudge_text.lower(), (
        f"hard tone must not contain soft-tone phrase; got {nudge_text!r}"
    )


def test_remaining_plural_grammar() -> None:
    """The nudge text must use 'tool call' (singular) when remaining=1
    and 'tool calls' (plural) for any other count. Tested by directly
    calling the private `_format_nudge` so we don't have to rig up an
    on_llm_start sequence per count."""
    # Soft-tone hook (max_turns > 5)
    hooks_soft = CountdownHooks(max_turns=20, threshold=8, tool_name=EXPECTED_TOOL_NAME)
    assert "1 tool call left" in hooks_soft._format_nudge(1)  # type: ignore[attr-defined]
    assert "2 tool calls left" in hooks_soft._format_nudge(2)  # type: ignore[attr-defined]
    assert "8 tool calls left" in hooks_soft._format_nudge(8)  # type: ignore[attr-defined]

    # Hard-tone hook (max_turns <= 5)
    hooks_hard = CountdownHooks(max_turns=3, threshold=8, tool_name=EXPECTED_TOOL_NAME)
    assert "1 tool call left" in hooks_hard._format_nudge(1)  # type: ignore[attr-defined]
    assert "2 tool calls left" in hooks_hard._format_nudge(2)  # type: ignore[attr-defined]

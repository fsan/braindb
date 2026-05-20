"""Edge-case tests for Stage C — `submit_result` → `final_answer` rename + slot pattern.

These are UNIT tests: they import `braindb.agent.*` directly and exercise the
internal contract surface (`FunctionTool.name`, the `_build()` factory's
`StopAtTools` config, the run_state slot lifecycle, run_typed's strict
behaviour). No live LLM, no HTTP — fast and deterministic.

They run alongside the existing integration tests; pytest's session-scoped
`_require_live_api` fixture from `conftest.py` still applies (the suite as a
whole expects a healthy stack), but THESE tests don't actually call the API.

Until Stage C / Layer 1 lands, most assertions here are RED on the
`experimental/structured-output-proper` branch (the rename hasn't happened
yet). After the rename they go green and serve as regression coverage.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from braindb.agent import agent as agent_module
from braindb.agent import run_state
from braindb.agent.schemas import (
    AgentAnswer,
    MaintainerDecision,
    SubagentResult,
    WikiWriteResult,
)
from braindb.agent.tools import (
    submit_answer,
    submit_maintainer,
    submit_subagent,
    submit_wiki,
)


# ------------------------------------------------------------------ #
# Layer 1 — rename surface (FAILS until Stage C / Layer 1 ships)      #
# ------------------------------------------------------------------ #

EXPECTED_FINAL_TOOL_NAME = "final_answer"


@pytest.mark.parametrize(
    "tool",
    [submit_answer, submit_maintainer, submit_wiki, submit_subagent],
    ids=["answer", "maintainer", "wiki", "subagent"],
)
def test_submit_tools_renamed_to_final_answer(tool) -> None:
    """Every typed `submit_*` @function_tool must expose name 'final_answer'
    to the SDK after the rename. The LLM sees this name in the tool catalog;
    a mismatch with the prompt or `StopAtTools` config breaks termination."""
    assert hasattr(tool, "name"), (
        f"{tool!r} is not a FunctionTool — did @function_tool decoration get dropped?"
    )
    assert tool.name == EXPECTED_FINAL_TOOL_NAME, (
        f"{tool!r}.name={tool.name!r}; expected {EXPECTED_FINAL_TOOL_NAME!r} after rename"
    )


def test_stop_at_tools_uses_final_answer() -> None:
    """The `_build()` factory must configure `StopAtTools` with the new name.
    Build all four agents and inspect their tool_use_behavior."""
    agents_to_check = [
        agent_module.get_agent(),
        agent_module.get_maintainer_agent(),
        agent_module.get_writer_agent(),
        agent_module.get_subagent(),
    ]
    for a in agents_to_check:
        beh = a.tool_use_behavior
        # SDK stores it as a dict {"stop_at_tool_names": [...]} OR as a
        # StopAtTools dataclass with the same attribute. Accept both shapes.
        names = (
            beh.get("stop_at_tool_names") if isinstance(beh, dict)
            else getattr(beh, "stop_at_tool_names", None) or getattr(beh, "tool_names", None)
        )
        assert names is not None, f"{a.name}: tool_use_behavior {beh!r} has no recognisable stop-names"
        assert EXPECTED_FINAL_TOOL_NAME in names, (
            f"{a.name}: StopAtTools={names!r}; expected to include {EXPECTED_FINAL_TOOL_NAME!r}"
        )


@pytest.mark.parametrize(
    "prompt_path",
    [
        Path("braindb/agent/prompts/system_prompt.md"),
        Path("braindb/agent/prompts/wiki_maintainer_prompt.md"),
        Path("braindb/agent/prompts/wiki_writer_prompt.md"),
    ],
    ids=["system", "wiki_maintainer", "wiki_writer"],
)
def test_prompts_no_stale_submit_result(prompt_path: Path) -> None:
    """Prompt files must NOT contain the literal `submit_result` after the
    rename — otherwise the LLM gets a confused contract (catalog says
    `final_answer`, prompt says `submit_result`)."""
    repo_root = Path(__file__).parent.parent  # tests/ → repo root
    full = repo_root / prompt_path
    assert full.exists(), f"prompt missing: {full}"
    body = full.read_text(encoding="utf-8")
    assert "submit_result" not in body, (
        f"{prompt_path} still references 'submit_result' — should be 'final_answer'"
    )


# ------------------------------------------------------------------ #
# Slot pattern (already shipped in 8560cfa; regression coverage)      #
# ------------------------------------------------------------------ #


def test_slot_install_and_release_isolation() -> None:
    """Two sequential install/release cycles produce distinct slot objects.
    Within a cycle, `record_submit` mutates the active slot; after release,
    the outer slot's value is unchanged."""
    slot1, token1 = run_state.install_slot()
    assert slot1.value is None
    run_state.record_submit("payload-1")
    assert slot1.value == "payload-1"
    run_state.release_slot(token1)

    slot2, token2 = run_state.install_slot()
    assert slot2 is not slot1
    assert slot2.value is None       # fresh slot, not stale data from slot1
    run_state.record_submit("payload-2")
    assert slot2.value == "payload-2"
    assert slot1.value == "payload-1"  # the released slot still holds its old data, but is no longer the ContextVar's value
    run_state.release_slot(token2)


def test_slot_nested_install_release() -> None:
    """The wiki maintainer/writer pattern: parent run_typed installs a slot,
    a delegated subagent installs its own, releases, then parent finalises.
    The child's record_submit must NOT contaminate the parent's slot."""
    parent_slot, parent_token = run_state.install_slot()
    run_state.record_submit("parent-data")
    assert parent_slot.value == "parent-data"

    # Child run_typed enters
    child_slot, child_token = run_state.install_slot()
    assert child_slot is not parent_slot
    assert child_slot.value is None
    run_state.record_submit("child-data")
    assert child_slot.value == "child-data"
    assert parent_slot.value == "parent-data"  # unaffected
    run_state.release_slot(child_token)

    # Back in parent context; record_submit should target parent again
    run_state.record_submit("parent-data-after-child")
    assert parent_slot.value == "parent-data-after-child"
    run_state.release_slot(parent_token)


def test_record_submit_outside_run_is_silent_noop() -> None:
    """If `record_submit` is called outside any `install_slot()` scope (e.g.
    a bug in a tool, or stale state), it must NOT raise. The current
    implementation silently drops the payload because the ContextVar
    defaults to None."""
    # This must not raise even with no active slot.
    run_state.record_submit("orphan-payload")
    # The slot var should still be None
    assert run_state._slot_var.get() is None


# ------------------------------------------------------------------ #
# run_typed strict-mode behaviour                                     #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_run_typed_raises_when_submit_never_fires() -> None:
    """If Runner.run completes without any `submit_*` having called
    record_submit, run_typed must raise RuntimeError — the strict-mode
    invariant. Surfaces 'model emitted prose' / 'max_turns exhausted'
    as a real failure rather than silently returning bad data."""
    fake_agent = mock.MagicMock(name="fake_agent")
    fake_agent.name = "FakeAgent"

    async def fake_runner_run(starting_agent, input, max_turns, **kwargs):
        # Pretend the LLM ran but never called any submit_*.
        return mock.MagicMock(final_output="some-prose-text")

    with mock.patch.object(agent_module.Runner, "run", new=fake_runner_run):
        with pytest.raises(RuntimeError, match="did not call final_answer|did not submit"):
            await agent_module.run_typed("query", fake_agent, AgentAnswer, max_turns=5)


@pytest.mark.asyncio
async def test_run_typed_returns_typed_payload_when_submitted() -> None:
    """If record_submit IS called during Runner.run with the expected typed
    payload, run_typed returns that exact instance — the typed-final
    contract."""
    fake_agent = mock.MagicMock(name="fake_agent")
    fake_agent.name = "FakeAgent"
    expected = AgentAnswer(answer="hello world")

    async def fake_runner_run(starting_agent, input, max_turns, **kwargs):
        # Simulate a submit_* tool body firing during the run
        run_state.record_submit(expected)
        return mock.MagicMock(final_output="ok")

    with mock.patch.object(agent_module.Runner, "run", new=fake_runner_run):
        got = await agent_module.run_typed("query", fake_agent, AgentAnswer, max_turns=5)
    assert got is expected
    assert got.answer == "hello world"


# ------------------------------------------------------------------ #
# Pydantic typed-arg validation (regression cover)                     #
# ------------------------------------------------------------------ #


# ------------------------------------------------------------------ #
# Stage C / Layer 4 — retry-with-correction on prose-terminal         #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_run_typed_retries_when_first_attempt_missing_final() -> None:
    """When the first `Runner.run` ends without `final_answer` firing,
    `run_typed` must inject a correction message and re-invoke
    `Runner.run` ONCE. On the retry, if the model calls `final_answer`
    via `record_submit`, the typed payload is returned and the caller
    gets a success — no 500."""
    fake_agent = mock.MagicMock(name="fake_agent")
    fake_agent.name = "FakeAgent"
    expected = AgentAnswer(answer="recovered after correction")
    call_count = {"n": 0}

    async def fake_runner_run(starting_agent, input, max_turns, **kwargs):
        call_count["n"] += 1
        result_mock = mock.MagicMock()
        result_mock.to_input_list.return_value = [{"role": "user", "content": "prior context"}]
        result_mock.final_output = "prose without final_answer call"
        if call_count["n"] == 2:
            # The retry: simulate the model now calling final_answer
            run_state.record_submit(expected)
        return result_mock

    with mock.patch.object(agent_module.Runner, "run", new=fake_runner_run):
        # Make sure retry is enabled
        with mock.patch.object(agent_module.settings, "agent_retry_on_missing_final", True):
            got = await agent_module.run_typed("query", fake_agent, AgentAnswer, max_turns=10)
    assert got is expected
    assert call_count["n"] == 2, "expected exactly one retry"


@pytest.mark.asyncio
async def test_run_typed_raises_when_retry_also_fails() -> None:
    """If BOTH the first attempt AND the retry end without `final_answer`,
    `run_typed` must still raise `RuntimeError`. No silent success on a
    genuinely-broken model that refuses the contract even after
    correction."""
    fake_agent = mock.MagicMock(name="fake_agent")
    fake_agent.name = "FakeAgent"
    call_count = {"n": 0}

    async def fake_runner_run(starting_agent, input, max_turns, **kwargs):
        call_count["n"] += 1
        result_mock = mock.MagicMock()
        result_mock.to_input_list.return_value = []
        result_mock.final_output = "still prose"
        # Neither attempt calls record_submit — slot stays None.
        return result_mock

    with mock.patch.object(agent_module.Runner, "run", new=fake_runner_run):
        with mock.patch.object(agent_module.settings, "agent_retry_on_missing_final", True):
            with pytest.raises(RuntimeError, match="did not call final_answer|even after"):
                await agent_module.run_typed("query", fake_agent, AgentAnswer, max_turns=10)
    assert call_count["n"] == 2, "expected exactly one retry before giving up"


@pytest.mark.asyncio
async def test_run_typed_retry_disabled_via_setting() -> None:
    """`agent_retry_on_missing_final=False` is the opt-out: when the first
    attempt ends without `final_answer`, raise immediately — no retry."""
    fake_agent = mock.MagicMock(name="fake_agent")
    fake_agent.name = "FakeAgent"
    call_count = {"n": 0}

    async def fake_runner_run(starting_agent, input, max_turns, **kwargs):
        call_count["n"] += 1
        result_mock = mock.MagicMock()
        result_mock.to_input_list.return_value = []
        result_mock.final_output = "prose"
        return result_mock

    with mock.patch.object(agent_module.Runner, "run", new=fake_runner_run):
        with mock.patch.object(agent_module.settings, "agent_retry_on_missing_final", False):
            with pytest.raises(RuntimeError, match="did not call final_answer"):
                await agent_module.run_typed("query", fake_agent, AgentAnswer, max_turns=10)
    assert call_count["n"] == 1, "retry should NOT happen when setting is False"


@pytest.mark.asyncio
async def test_run_typed_correction_message_appended_on_retry() -> None:
    """The retry call must pass `result.to_input_list() + [correction]` as
    `input` to `Runner.run`, where `correction` is a user-role message
    that explicitly references `final_answer` so the LLM gets an
    unambiguous instruction (not a parse-the-prose hack)."""
    fake_agent = mock.MagicMock(name="fake_agent")
    fake_agent.name = "FakeAgent"
    prior_items = [
        {"role": "user", "content": "save this fact"},
        {"role": "assistant", "content": "okay, doing the work..."},
    ]
    captured_inputs: list = []

    async def fake_runner_run(starting_agent, input, max_turns, **kwargs):
        captured_inputs.append(input)
        result_mock = mock.MagicMock()
        result_mock.to_input_list.return_value = prior_items
        result_mock.final_output = "prose"
        # No record_submit anywhere — to force the retry path AND fail again.
        return result_mock

    with mock.patch.object(agent_module.Runner, "run", new=fake_runner_run):
        with mock.patch.object(agent_module.settings, "agent_retry_on_missing_final", True):
            with pytest.raises(RuntimeError):
                await agent_module.run_typed("save this fact", fake_agent, AgentAnswer, max_turns=10)

    # First call gets the raw query string; second gets the prior history + a correction.
    assert len(captured_inputs) == 2
    assert captured_inputs[0] == "save this fact"
    retry_input = captured_inputs[1]
    assert isinstance(retry_input, list), f"retry input must be a message list, got {type(retry_input).__name__}"
    assert retry_input[: len(prior_items)] == prior_items, "retry must preserve the prior conversation"
    correction = retry_input[-1]
    assert isinstance(correction, dict) and correction.get("role") == "user", (
        f"correction message must be a user-role dict, got {correction!r}"
    )
    assert "final_answer" in correction.get("content", ""), (
        f"correction must mention `final_answer` so the model gets a clear instruction; got {correction!r}"
    )


@pytest.mark.parametrize(
    "tool, model, pydantic_required",
    [
        (submit_answer, AgentAnswer, ["answer"]),
        (submit_maintainer, MaintainerDecision, ["action", "rationale"]),
        (submit_wiki, WikiWriteResult, ["mode", "body"]),
        (submit_subagent, SubagentResult, ["result"]),
    ],
    ids=["answer", "maintainer", "wiki", "subagent"],
)
def test_submit_tool_schema_matches_pydantic_required(tool, model, pydantic_required) -> None:
    """The LLM-visible JSON schema's `required` list (inside the embedded
    payload definition) must match Pydantic's view of required fields,
    NOT the OpenAI strict-mode "all fields required" force-list.

    Background: with `@function_tool(strict_mode=True)` (the SDK default),
    the embedded payload schema lists EVERY property in `required`,
    regardless of `field: T | None = None` defaults at the Pydantic
    level. That over-strictness causes weak models to emit `final_answer`
    args that pass Pydantic but fail the inflated OpenAI-strict schema —
    leading to "Invalid JSON input: 1 validation error" loops the
    Layer 4 retry can't break out of (verified live on deepinfra/Gemma
    against the wiki maintainer). Setting `strict_mode=False` makes the
    submitted schema follow Pydantic's `required` faithfully; Pydantic
    still validates the parsed args so the typed contract holds.
    """
    schema = tool.params_json_schema
    # SDK wraps the payload model in a payload field; the model's own
    # schema is in `$defs[<ModelName>]`.
    inner = schema["$defs"][model.__name__]
    assert set(inner["required"]) == set(pydantic_required), (
        f"{tool.name} (model={model.__name__}): schema required="
        f"{inner['required']!r}; expected to match Pydantic's "
        f"{pydantic_required!r}. If this fails, the @function_tool "
        f"likely still has strict_mode=True overriding Pydantic's "
        f"required list."
    )


def test_typed_models_validate_strictly() -> None:
    """The @function_tool argument schemas are derived from these Pydantic
    models. Validation MUST reject malformed input — that's what protects
    the typed-final contract from the LLM emitting garbage args."""
    # Each model has at least one required field; passing the wrong shape
    # must raise pydantic.ValidationError.
    with pytest.raises(Exception):  # pydantic.ValidationError
        AgentAnswer(answer=123)  # wrong type
    with pytest.raises(Exception):
        MaintainerDecision()  # missing 'action'
    with pytest.raises(Exception):
        WikiWriteResult()  # missing 'mode' and 'body'
    with pytest.raises(Exception):
        SubagentResult()  # missing 'result'
    # Round-trip a valid one to confirm the happy path still works.
    a = AgentAnswer(answer="x")
    assert a.answer == "x"


# ------------------------------------------------------------------ #
# Forgiving coercion on nullable / list fields                        #
# ------------------------------------------------------------------ #
#
# Weak / quantised models often emit `""` (empty string) for nullable
# fields instead of literal JSON `null`, and `null` for empty-list
# fields instead of `[]`. The schema descriptions explicitly forbid
# both, but the `mode="before"` field_validators in schemas.py are the
# safety net: they accept the wrong-type variants gracefully so a
# perfectly intended "skip" decision isn't rejected by a closing
# Pydantic error. The validation contract is unchanged — we still
# produce a properly-typed Pydantic instance.
#
# These tests cover the coercion behaviour and confirm the
# action-dependent fields can be omitted-by-empty-string for non-attach
# / non-create / non-consolidate actions.


def test_maintainer_decision_coerces_empty_string_to_none() -> None:
    """`target_wiki_no=""` / `proposed_name=""` from the LLM coerce to
    None — Pydantic would normally reject `""` for `int | None`."""
    d = MaintainerDecision(
        action="skip",
        target_wiki_no="",
        proposed_name="",
        consolidate_nos=[],
        rationale="not worth a wiki",
    )
    assert d.target_wiki_no is None
    assert d.proposed_name is None
    assert d.consolidate_nos == []


def test_maintainer_decision_coerces_null_string_to_none() -> None:
    """Literal `"null"` / `"none"` / `"n/a"` strings (any case, surrounding
    whitespace ok) coerce to None — matches what weak models emit when
    they confuse "send JSON null" with "send the string null"."""
    for sentinel in ["null", "Null", "NULL", "none", "  null  ", "n/a", "N/A"]:
        d = MaintainerDecision(
            action="skip",
            target_wiki_no=sentinel,
            proposed_name=sentinel,
            consolidate_nos=[],
            rationale="not worth a wiki",
        )
        assert d.target_wiki_no is None, f"target_wiki_no should coerce {sentinel!r} → None"
        assert d.proposed_name is None, f"proposed_name should coerce {sentinel!r} → None"


def test_maintainer_decision_coerces_numeric_string_to_int() -> None:
    """`target_wiki_no="42"` (string-encoded integer from a weak model)
    coerces to `42` rather than raising."""
    d = MaintainerDecision(
        action="attach",
        target_wiki_no="42",
        rationale="attach to wiki 42",
    )
    assert d.target_wiki_no == 42
    assert isinstance(d.target_wiki_no, int)


def test_maintainer_decision_coerces_null_consolidate_nos_to_empty_list() -> None:
    """`consolidate_nos=None` (the weak model sent null instead of [])
    coerces to []. Without this, Pydantic raises because the field is
    `list[int]`, not `list[int] | None`."""
    d = MaintainerDecision(
        action="skip",
        consolidate_nos=None,
        rationale="not duplicates",
    )
    assert d.consolidate_nos == []


def test_wiki_write_result_coerces_canonical_no() -> None:
    """`canonical_no` (the wiki writer's consolidate-mode field) gets the
    same treatment: empty string / null string → None; numeric string
    → int."""
    r = WikiWriteResult(mode="create", canonical_no="", body="# Wiki body")
    assert r.canonical_no is None

    r = WikiWriteResult(mode="create", canonical_no="null", body="# Wiki body")
    assert r.canonical_no is None

    r = WikiWriteResult(mode="consolidate", canonical_no="3", body="# Wiki body")
    assert r.canonical_no == 3


def test_maintainer_decision_happy_path_still_works() -> None:
    """The coercion validators must NOT break the happy path where the
    LLM sends well-typed values."""
    d = MaintainerDecision(
        action="attach",
        target_wiki_no=7,
        proposed_name=None,
        consolidate_nos=[],
        rationale="attach to wiki 7",
    )
    assert d.target_wiki_no == 7

    d2 = MaintainerDecision(
        action="consolidate",
        consolidate_nos=[2, 5, 9],
        rationale="all three describe the same subject",
    )
    assert d2.consolidate_nos == [2, 5, 9]

    d3 = MaintainerDecision(
        action="create",
        proposed_name="Sawki",
        rationale="new subject, no existing wiki",
    )
    assert d3.proposed_name == "Sawki"

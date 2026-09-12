"""Stable-tier guidance for subagent delegation and the long-horizon board.

Pins the behavior contract behind "the model has the tool but rarely calls it":
the guidance must be present exactly when the tool is in the session's schema,
must never name a tool the session lacks (a dangling reference teaches ghost
vocabulary), and must be byte-stable for a given toolset so the prefix cache
holds — the toolset is fixed at construction, so the same input must render
identical bytes every time.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.prompt_builder import (
    DELEGATION_GUIDANCE,
    longtask_guidance_text,
)
from agent.system_prompt import build_system_prompt_parts

DELEGATE_ONLY = ["delegate_task"]
LONGTASK_TOOLS = [
    "longtask_create",
    "longtask_read",
    "longtask_next",
    "longtask_update_node",
    "longtask_attach_report",
    "longtask_verify_node",
]
LONGTASK_ONLY = list(LONGTASK_TOOLS)
BOTH = DELEGATE_ONLY + LONGTASK_TOOLS


def _stable_prompt(valid_tool_names, **overrides):
    """Render the real stable tier for a session with *valid_tool_names*.

    Runs the real prompt builders (patched only at the run_agent seam, same
    contract as tests/agent/test_platform_hint_desktop.py) so cache-stability
    and text placement are what is verified, not a mock's return value.
    """
    base = dict(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=list(valid_tool_names),
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _execution_guidance=False,
        _environment_probe=False,
        _parallel_tool_call_guidance=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _platform_hint_overrides={},
        model="",
        provider="",
        pass_session_id=False,
        session_id="",
        platform="cli",
    )
    base.update(overrides)
    agent = SimpleNamespace(**base)
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
        patch("run_agent.build_skills_system_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


class TestDelegationGuidanceInjection:
    def test_present_when_the_tool_is_available(self):
        prompt = _stable_prompt(DELEGATE_ONLY)
        assert DELEGATION_GUIDANCE in prompt

    def test_absent_when_the_tool_is_not_available(self):
        prompt = _stable_prompt(["terminal", "read_file"])
        assert "# Subagent delegation" not in prompt

    def test_absent_for_a_delegate_child(self):
        """Children have delegate_task stripped, so the block must vanish for
        them — otherwise a child is told to spawn subagents it cannot spawn."""
        prompt = _stable_prompt(["terminal", "read_file", "longtask_next"])
        assert "# Subagent delegation" not in prompt

    def test_guidance_names_no_tool_outside_the_session(self):
        prompt = _stable_prompt(DELEGATE_ONLY)
        assert "longtask" not in prompt

    def test_byte_stable_for_a_fixed_toolset(self):
        toolset = BOTH
        assert _stable_prompt(toolset) == _stable_prompt(toolset)


class TestLongtaskGuidanceInjection:
    def test_present_when_board_tools_are_available(self):
        prompt = _stable_prompt(BOTH)
        assert "# Long-horizon task board" in prompt

    def test_absent_without_board_tools(self):
        prompt = _stable_prompt(DELEGATE_ONLY)
        assert "# Long-horizon task board" not in prompt

    def test_single_board_tool_is_enough_to_trigger(self):
        prompt = _stable_prompt(["longtask_next"])
        assert "# Long-horizon task board" in prompt
        # …and it must not then name delegate_task, which this session lacks.
        assert "delegate_task" not in prompt


class TestLongtaskGuidanceText:
    def test_delegate_variant_names_the_tool(self):
        text = longtask_guidance_text(BOTH)
        assert "delegate_task" in text
        assert "claim-evidence" in text

    def test_solo_variant_has_no_dangling_delegate_reference(self):
        text = longtask_guidance_text(LONGTASK_ONLY)
        assert "delegate_task" not in text

    def test_both_variants_cover_the_board_lifecycle(self):
        for text in (longtask_guidance_text(BOTH), longtask_guidance_text(LONGTASK_ONLY)):
            for tool in LONGTASK_TOOLS:
                assert tool in text, f"{tool} missing from the board guidance"

    def test_unknown_toolset_defaults_to_the_delegate_variant(self):
        assert "delegate_task" in longtask_guidance_text(None)


class TestDelegateTaskDescriptionFraming:
    """The tool description must lead with when to USE it.

    The description used to open with background/dispatch mechanics and bury a
    single terse USE-FOR clause behind a four-item DO-NOT list; models given
    only that framing default to doing the work inline. Ordering is the fix, so
    ordering is what is pinned.
    """

    def test_positive_trigger_precedes_the_negative_list(self):
        from tools.delegate_tool import _build_top_level_description

        desc = _build_top_level_description()
        positive = desc.lower().find("use this")
        negative = desc.find("DO NOT USE FOR")
        assert positive != -1, "the description lost its positive trigger"
        assert negative != -1, "the description lost its DO-NOT list"
        assert positive < negative, (
            "the positive trigger must precede the DO-NOT list — that ordering "
            "is what counters the inline-by-default bias"
        )

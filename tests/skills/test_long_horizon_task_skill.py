"""Activation contract for the bundled long-horizon-task skill.

The skill body drives the ``longtask_*`` board tools and ``delegate_task``. It
must therefore only be advertised in the index when those tools are in the
session's schema — otherwise the model loads a protocol it cannot execute and
burns turns calling tools that do not exist.

The frontmatter key that does this is ``requires_toolsets``. A bare
``toolsets:`` key is inert (nothing reads it), which is exactly the failure this
file guards: the skill looked configured while enabling nothing.
"""

from pathlib import Path

from agent.prompt_builder import _skill_should_show
from agent.skill_utils import extract_skill_conditions, parse_frontmatter

SKILL_MD = (
    Path(__file__).resolve().parents[2] / "skills" / "long-horizon-task" / "SKILL.md"
)

LONGTASK_TOOLS = {"longtask_create", "longtask_next", "longtask_verify_node"}


def _conditions():
    frontmatter, _body = parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))
    return extract_skill_conditions(frontmatter)


class TestActivationContract:
    def test_declares_the_toolsets_its_body_needs(self):
        requires = set(_conditions()["requires_toolsets"])
        assert {"longtask", "delegation"} <= requires, (
            "the skill body drives longtask_* and delegate_task, so both "
            "toolsets must be declared in requires_toolsets"
        )

    def test_hidden_when_the_board_toolset_is_absent(self):
        assert (
            _skill_should_show(
                _conditions(),
                {"delegate_task", "terminal"},
                {"delegation", "terminal"},
            )
            is False
        )

    def test_shown_when_both_toolsets_are_active(self):
        assert (
            _skill_should_show(
                _conditions(),
                {"delegate_task", *LONGTASK_TOOLS},
                {"delegation", "longtask"},
            )
            is True
        )

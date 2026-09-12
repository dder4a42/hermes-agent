"""Deferral-layer fixes: behavior regression suite.

Each test class pins one user-visible behavior that was broken while the
tool_search bridge was active. Tests assert at public seams (planner
segment shapes, search results, listing lines, get_tool_definitions
output) — not private implementation details — so refactors that keep
the behavior keep the tests.

The bugs, as reproduced before the fix:

1. ``_plan_tool_batch_segments`` classified the literal name ``tool_call``
   as a sequential barrier, so a server opted in via
   ``supports_parallel_tool_calls: true`` silently lost all concurrency
   the moment the bridge activated (every deferred call arrives wrapped).
2. ``_short_desc`` cut at the first ``.`` anywhere, so "e.g.", "v1.2",
   and "api.github.com" truncated catalog listing lines to garbage.
3. The BM25 document didn't include the tool's source, so a query naming
   the service ("linear") missed tools whose own name omits it.
4. (docstring-only) the substring fallback documented a zero-IDF case
   that cannot occur with the Lucene IDF variant.
5. A direct call to a tool that tool search had deferred answered
   "Tool 'X' does not exist. Available tools: [...]" — indistinguishable
   from a typo — so the model abandoned a capability that was one bridge
   call away. It now routes the model to the bridge (tool_search /
   tool_describe / tool_call) and names the tool's source (plugin toolset /
   MCP server).
6. The bridge activation decision was re-taken on EVERY assembly, so a
   session that started with plugin tools listed directly flipped them into
   the deferred catalog as soon as MCP tools showed up (a server finishing a
   slow connect, a plugin toolset landing) — the model-facing tools array
   changed inside one conversation, breaking the cached prompt prefix, and
   tools the model had been calling directly answered "does not exist". The
   decision is now latched per session (``DeferralSession``), owned by the
   object that owns the conversation.
"""

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from agent.tool_dispatch_helpers import _plan_tool_batch_segments
from tools.tool_search import _short_desc, build_catalog, search_catalog


def _tc(name, arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _bridge_tc(underlying, arguments=None, call_id=None):
    """A tool_call bridge invocation as the model emits it."""
    return _tc(
        "tool_call",
        json.dumps({"name": underlying, "arguments": arguments or {}}),
        call_id=call_id,
    )


def _td(name, desc="", params=None, required=None):
    parameters = {"type": "object", "properties": params or {}}
    if required:
        parameters["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": desc, "parameters": parameters},
    }


def _kinds(segments):
    return [kind for kind, _ in segments]


def _flatten_ids(segments):
    return [tc.id for _, calls in segments for tc in calls]


@pytest.fixture
def mcp_pair(monkeypatch):
    """Two tools on a parallel-opted-in MCP server, registered for real.

    Registers via the actual registry (so ``resolve_underlying_call``'s
    deferability check passes) and marks the server parallel-safe through
    the real provenance maps in ``tools.mcp_tool``.
    """
    from tools import mcp_tool
    from tools.registry import registry

    names = ["mcp__pytestsrv__alpha_read", "mcp__pytestsrv__beta_read"]
    for n in names:
        registry.register(
            name=n,
            toolset="mcp-pytestsrv",
            schema=_td(n, "Read-only test tool.")["function"],
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )
    with mcp_tool._lock:
        for n in names:
            mcp_tool._mcp_tool_server_names[n] = "pytestsrv"
        mcp_tool._parallel_safe_servers.add("pytestsrv")
    yield names
    with mcp_tool._lock:
        mcp_tool._parallel_safe_servers.discard("pytestsrv")
        for n in names:
            mcp_tool._mcp_tool_server_names.pop(n, None)
    for n in names:
        registry.deregister(n)


class TestBridgePeelInPlanner:
    """Fix 1: batch admission is decided on the underlying tool."""

    def test_two_bridged_parallel_safe_mcp_calls_run_parallel(self, mcp_pair):
        alpha, beta = mcp_pair
        calls = [_bridge_tc(alpha, call_id="a"), _bridge_tc(beta, call_id="b")]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]
        assert _flatten_ids(segments) == ["a", "b"]

    def test_bridged_call_to_non_opted_in_tool_stays_sequential(self, mcp_pair):
        from tools import mcp_tool

        with mcp_tool._lock:
            mcp_tool._parallel_safe_servers.discard("pytestsrv")
        try:
            alpha, beta = mcp_pair
            calls = [_bridge_tc(alpha, call_id="a"), _bridge_tc(beta, call_id="b")]
            segments = _plan_tool_batch_segments(calls)
            assert _kinds(segments) == ["sequential"]
        finally:
            with mcp_tool._lock:
                mcp_tool._parallel_safe_servers.add("pytestsrv")

    def test_bridge_lookups_are_parallel_safe(self):
        calls = [
            _tc("tool_search", '{"query": "issues"}', call_id="s1"),
            _tc("tool_search", '{"query": "pages"}', call_id="s2"),
            _tc("tool_describe", '{"name": "mcp__x__y"}', call_id="d1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]
        assert _flatten_ids(segments) == ["s1", "s2", "d1"]

    def test_malformed_bridge_call_stays_a_barrier(self):
        calls = [
            _tc("tool_call", '{"arguments": {}}', call_id="bad"),  # no name
            _tc("web_search", '{"query": "x"}', call_id="r1"),
            _tc("web_search", '{"query": "y"}', call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential", "parallel"]
        assert [tc.id for tc in segments[0][1]] == ["bad"]

    def test_emission_order_survives_the_peel(self, mcp_pair):
        alpha, beta = mcp_pair
        calls = [
            _bridge_tc(alpha, call_id="a"),
            _tc("terminal", '{"command": "make"}', call_id="t"),
            _bridge_tc(beta, call_id="b"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _flatten_ids(segments) == ["a", "t", "b"]

    def test_bridged_mcp_admission_matches_direct_admission(self, mcp_pair, tmp_path, monkeypatch):
        """The peel restores PARITY, not extra permissiveness: a bridged call
        to an opted-in MCP tool gets exactly the admission the same tool gets
        when called directly. Opted-in MCP tools have always shared parallel
        runs with core path-scoped tools (the server opt-in is the owner's
        declared contract; the planner has never had per-MCP-tool resource
        scopes) — the bridge must not silently upgrade OR downgrade that."""
        monkeypatch.chdir(tmp_path)
        alpha, _ = mcp_pair
        direct = _plan_tool_batch_segments([
            _tc(alpha, "{}", call_id="m1"),
            _tc("write_file", '{"path":"x.py","content":"a"}', call_id="w1"),
        ])
        bridged = _plan_tool_batch_segments([
            _bridge_tc(alpha, {}, call_id="m1"),
            _tc("write_file", '{"path":"x.py","content":"a"}', call_id="w1"),
        ])
        assert [(k, [c.id for c in cs]) for k, cs in direct] == \
               [(k, [c.id for c in cs]) for k, cs in bridged]

    def test_core_file_tools_cannot_be_smuggled_through_the_bridge(self):
        """Wrapped core file tools remain sequential because they are not deferrable."""
        calls = [
            _bridge_tc("write_file", {"path": "a.py", "content": "x"}, call_id="w"),
            _bridge_tc("read_file", {"path": "a.py"}, call_id="r"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["w", "r"]


class TestShortDescSentenceBoundary:
    """Fix 2: listing lines survive abbreviations, versions, hostnames."""

    def test_clean_two_sentence_case_still_clips_at_first(self):
        assert _short_desc("Open an issue. Second sentence dropped.") == "Open an issue."

    def test_abbreviation_does_not_truncate(self):
        s = _short_desc("Create an issue (e.g. a bug report) in a repository.")
        assert s.startswith("Create an issue (e.g. a bug report)")

    def test_hostname_does_not_truncate(self):
        s = _short_desc("Fetch a page from api.github.com and return the JSON body.")
        assert "api.github.com" in s

    def test_version_string_does_not_truncate(self):
        s = _short_desc("Upgrade to v1.2 of the schema and migrate all rows.")
        assert "v1.2" in s

    def test_exclamation_terminator_is_kept(self):
        assert _short_desc("List repos! Supports pagination.") == "List repos!"

    def test_question_terminator_is_kept(self):
        s = _short_desc("What does this do? It lists channels.")
        assert s == "What does this do?"

    def test_long_text_still_clips_with_ellipsis(self):
        s = _short_desc("word " * 40)
        assert len(s) <= 61
        assert s.endswith("…")

    def test_empty_is_empty(self):
        assert _short_desc("") == ""


class TestSourceNameIndexing:
    """Fix 3: a query naming the service finds that source's tools."""

    @staticmethod
    def _register(name, toolset, desc):
        from tools.registry import registry

        registry.register(
            name=name,
            toolset=toolset,
            schema=_td(name, desc)["function"],
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )
        return name

    def test_service_query_reaches_tool_without_service_in_name(self):
        """A plugin tool named ``create_issue`` in toolset ``mcp-linear``
        must be reachable by the query "linear"."""
        from tools.registry import registry

        names = [
            self._register("create_issue", "mcp-linear", "Create a new issue in a team."),
            self._register("post_message", "mcp-slack", "Post a message to a channel."),
        ]
        try:
            defs = [_td(n, d) for n, d in
                    [("create_issue", "Create a new issue in a team."),
                     ("post_message", "Post a message to a channel.")]]
            catalog = build_catalog(defs)
            hits = search_catalog(catalog, "linear")
            assert [h.name for h in hits] == ["create_issue"]
        finally:
            for n in names:
                registry.deregister(n)

    def test_mcp_prefix_is_not_a_matchable_token(self):
        """The shared ``mcp`` prefix used to sit in every native MCP document
        as a near-zero-IDF token: a query containing "mcp" matched EVERY
        tool, drowning the discriminating terms. Now "mcp" contributes
        nothing to ranking, so the discriminating term decides alone."""
        from tools.registry import registry

        names = [
            self._register("mcp__linear__create_issue", "mcp-linear", "Create an issue."),
            self._register("mcp__slack__post_message", "mcp-slack", "Post a message."),
        ]
        try:
            defs = [_td("mcp__linear__create_issue", "Create an issue."),
                    _td("mcp__slack__post_message", "Post a message.")]
            catalog = build_catalog(defs)
            hits = search_catalog(catalog, "mcp message")
            # Before the fix "mcp" BM25-matched both docs, so both came
            # back and the order was decided by document length, not by
            # the term the model actually meant.
            assert [h.name for h in hits] == ["mcp__slack__post_message"]
        finally:
            for n in names:
                registry.deregister(n)

    def test_source_label_is_indexed_once_for_native_and_plugin_names(self):
        from tools.registry import registry

        source_label = "catalogsource"
        names = [
            self._register(
                "mcp__catalogsource__native_action",
                "mcp-catalogsource",
                "Perform a native action.",
            ),
            self._register(
                "plugin_action",
                "mcp-catalogsource",
                "Perform a plugin action.",
            ),
        ]
        try:
            catalog = build_catalog([
                _td("mcp__catalogsource__native_action", "Perform a native action."),
                _td("plugin_action", "Perform a plugin action."),
            ])
            # Compare in token space: the tokenizer may stem (e.g.
            # "catalogsource" -> "catalogsourc"), and the contract is that
            # the label lands in the document exactly once either way.
            from tools.tool_search import _tokenize
            label_token = _tokenize(source_label)[0]
            tokens_by_name = {entry.name: entry._tokens for entry in catalog}
            assert tokens_by_name[names[0]].count(label_token) == 1
            assert tokens_by_name[names[1]].count(label_token) == 1
        finally:
            for name in names:
                registry.deregister(name)

    def test_substring_fallback_covers_token_misses(self):
        """"hub" is a substring of github but never a token — the fallback
        (not BM25) must return the github tools."""
        from tools.registry import registry

        names = [
            self._register("github_create_issue", "mcp-github", "Create an issue."),
            self._register("github_merge_pr", "mcp-github", "Merge a pull request."),
        ]
        try:
            defs = [_td("github_create_issue", "Create an issue."),
                    _td("github_merge_pr", "Merge a pull request.")]
            catalog = build_catalog(defs)
            hits = search_catalog(catalog, "hub")
            assert {h.name for h in hits} == {"github_create_issue", "github_merge_pr"}
            assert search_catalog(catalog, "zzzz") == []
        finally:
            for n in names:
                registry.deregister(n)


class TestDeferredDirectCallGuidance:
    """Fix 5: a direct call to a deferred tool routes the model to the bridge.

    Before the fix the reply was ``Tool 'X' does not exist. Available
    tools: [...]`` — the same text a pure typo gets — so a model that had
    been calling the tool directly (it was visible until the bridge turned
    on) read it as "this capability is gone" instead of "say it through
    tool_call". The guidance must (a) say the tool was deferred, not missing,
    (b) name the bridge tools AND the call to make, and (c) name the source
    so the model can tell whether the tool is the capability it wants.
    """

    @pytest.fixture
    def deferred_plugin_tool(self):
        from tools.registry import registry

        name = "pytestguard_probe"
        registry.register(
            name=name,
            toolset="pytestguard",
            schema=_td(name, "Probe a deferred direct call.")["function"],
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )
        yield name
        registry.deregister(name)

    @pytest.fixture
    def deferred_mcp_tool(self):
        from tools.registry import registry

        name = "mcp__pytestguardsrv__remote_probe"
        registry.register(
            name=name,
            toolset="mcp-pytestguardsrv",
            schema=_td(name, "Remote probe.")["function"],
            handler=lambda args, **kw: json.dumps({"ok": True}),
        )
        yield name
        registry.deregister(name)

    def test_guidance_says_deferred_names_the_source_and_routes_to_the_bridge(
        self, deferred_plugin_tool
    ):
        from tools.tool_search import deferred_tool_guidance

        msg = deferred_tool_guidance(deferred_plugin_tool)
        assert msg is not None
        assert "deferred" in msg
        # The source, so the model can judge whether this is the capability it
        # was reaching for.
        assert "pytestguard" in msg
        # And every bridge route, so it can act on the message in one step.
        for bridge in ("tool_search", "tool_describe", "tool_call"):
            assert bridge in msg
        # Never the "it's gone" reading.
        assert "does not exist" not in msg

    def test_guidance_shows_the_exact_call_to_make(self, deferred_plugin_tool):
        from tools.tool_search import deferred_tool_guidance

        msg = deferred_tool_guidance(deferred_plugin_tool)
        assert f"tool_call(name='{deferred_plugin_tool}'" in msg

    def test_mcp_tools_are_attributed_to_their_server(self, deferred_mcp_tool):
        from tools.tool_search import deferred_tool_guidance

        msg = deferred_tool_guidance(deferred_mcp_tool)
        assert msg is not None
        assert "MCP server 'pytestguardsrv'" in msg

    def test_names_that_are_not_deferred_get_no_guidance(
        self, deferred_plugin_tool
    ):
        """Core tools, bridge tools, unknown names: the caller's own message
        stands (a core tool was never deferrable; an unknown name IS a typo)."""
        from tools.tool_search import deferred_tool_guidance

        assert deferred_tool_guidance("terminal") is None
        assert deferred_tool_guidance("tool_call") is None
        assert deferred_tool_guidance("pytestguard_absent_zz") is None
        assert deferred_tool_guidance("") is None

    def test_invalid_name_error_prefers_guidance_over_the_catalog(
        self, deferred_plugin_tool
    ):
        from agent.conversation_loop import _invalid_tool_name_error_content
        from tools.tool_search import BRIDGE_TOOL_NAMES

        # The real shape of a session with the bridge active: the visible names
        # are core tools + the three bridge tools, and the deferred tool is
        # absent from them.
        visible = {"terminal", "read_file"} | set(BRIDGE_TOOL_NAMES)
        msg = _invalid_tool_name_error_content(deferred_plugin_tool, visible)
        assert "does not exist" not in msg
        assert f"tool_call(name='{deferred_plugin_tool}'" in msg
        # No catalog dump: the model is being told to use the bridge, not to
        # re-pick from a list the tool is absent from anyway.
        assert "Available tools:" not in msg

    def test_invalid_name_error_does_not_promise_a_bridge_that_is_off(
        self, deferred_plugin_tool
    ):
        """A session that kept its deferrable tools direct has no tool_call to
        route through, so "does not exist" stays the truthful answer."""
        from agent.conversation_loop import _invalid_tool_name_error_content

        msg = _invalid_tool_name_error_content(
            deferred_plugin_tool, {"terminal", "read_file"}
        )
        assert "does not exist" in msg
        assert "tool_call" not in msg

    def test_invalid_name_error_keeps_the_catalog_for_genuine_typos(
        self, deferred_plugin_tool
    ):
        from agent.conversation_loop import _invalid_tool_name_error_content

        assert _invalid_tool_name_error_content("termnal", {"terminal"}) == \
            "Tool 'termnal' does not exist. Available tools: terminal"

    def test_invalid_name_error_keeps_the_blank_name_anti_priming_reply(
        self, deferred_plugin_tool
    ):
        from agent.conversation_loop import _invalid_tool_name_error_content

        msg = _invalid_tool_name_error_content("   ", {"terminal"})
        assert "tool name was empty" in msg
        assert "Available tools:" not in msg


class TestSessionDeferralLatch:
    """Fix 6: the disclosure decision is latched per session.

    Reproduced before the fix: a conversation that began with plugin tools
    listed directly (no MCP server had connected yet) flipped them into the
    deferred catalog the moment MCP tools appeared, so the model-facing tools
    array changed inside one conversation and the plugin tools answered
    "does not exist" to a direct call.

    The contract these tests hold: for a session that supplies its latch, the
    set of tools visible to the model only ever GROWS during the session —
    a later MCP connect / plugin load can add tools, but nothing already
    listed is ever moved behind the bridge (or promoted back out of it).
    """

    @staticmethod
    def _defs():
        return (
            _td("pytestlatch_probe", "A plugin capability."),
            _td("mcp__pytestlatch__remote_probe", "A remote capability."),
        )

    @pytest.fixture
    def deferrable_pair(self):
        from tools.registry import registry

        plugin, mcp = self._defs()
        for tool_def, toolset in ((plugin, "pytestlatch"), (mcp, "mcp-pytestlatch")):
            name = tool_def["function"]["name"]
            registry.register(
                name=name,
                toolset=toolset,
                schema=tool_def["function"],
                handler=lambda args, **kw: json.dumps({"ok": True}),
            )
        yield plugin, mcp
        for tool_def, _ in ((plugin, None), (mcp, None)):
            registry.deregister(tool_def["function"]["name"])

    @staticmethod
    def _names(result):
        return {t["function"]["name"] for t in result.tool_defs}

    def test_eager_session_stays_eager_when_deferrable_tools_arrive(
        self, deferrable_pair
    ):
        from tools.tool_search import (
            BRIDGE_TOOL_NAMES, DeferralSession, ToolSearchConfig, assemble_tool_defs,
        )

        plugin, mcp = deferrable_pair
        session = DeferralSession()
        cfg = ToolSearchConfig.from_raw({})
        before = assemble_tool_defs(
            [_td("terminal", "Run shell")],
            context_length=200_000, config=cfg, session=session,
        )
        assert not before.activated

        after = assemble_tool_defs(
            [_td("terminal", "Run shell"), plugin, mcp],
            context_length=200_000, config=cfg, session=session,
        )
        assert not after.activated, (
            "an MCP server finishing its connect must not turn the bridge on "
            "in the middle of a session"
        )
        names = self._names(after)
        assert self._names(before) <= names
        assert not (BRIDGE_TOOL_NAMES & names)
        assert plugin["function"]["name"] in names

    def test_deferred_session_keeps_deferring_and_never_promotes_tools(
        self, deferrable_pair
    ):
        from tools.tool_search import (
            BRIDGE_TOOL_NAMES, DeferralSession, ToolSearchConfig, assemble_tool_defs,
        )

        plugin, mcp = deferrable_pair
        session = DeferralSession()
        cfg = ToolSearchConfig.from_raw({"enabled": "on"})
        first = assemble_tool_defs(
            [_td("terminal", "Run shell"), plugin],
            context_length=200_000, config=cfg, session=session,
        )
        assert first.activated
        assert plugin["function"]["name"] not in self._names(first)

        later = assemble_tool_defs(
            [_td("terminal", "Run shell"), plugin, mcp],
            context_length=200_000, config=cfg, session=session,
        )
        assert later.activated
        assert BRIDGE_TOOL_NAMES <= self._names(later)
        # Nothing that was hidden becomes direct again (and vice versa).
        assert self._names(later) - BRIDGE_TOOL_NAMES == \
            self._names(first) - BRIDGE_TOOL_NAMES

    def test_two_sessions_decide_independently(self, deferrable_pair):
        """The latch is per conversation, not a process-wide decision."""
        from tools.tool_search import DeferralSession, ToolSearchConfig, assemble_tool_defs

        plugin, _ = deferrable_pair
        cfg = ToolSearchConfig.from_raw({})
        eager = DeferralSession()
        assemble_tool_defs(
            [_td("terminal", "Run shell")],
            context_length=200_000, config=cfg, session=eager,
        )
        fresh = DeferralSession()
        fresh_result = assemble_tool_defs(
            [_td("terminal", "Run shell"), plugin],
            context_length=200_000, config=cfg, session=fresh,
        )
        assert eager.activated is False
        assert fresh.activated is True
        assert fresh_result.activated is True

    def test_inspection_callers_still_follow_the_live_catalog(self, deferrable_pair):
        """No latch (``/tools``, banners, probes) = live decision, unchanged."""
        from tools.tool_search import ToolSearchConfig, assemble_tool_defs

        plugin, _ = deferrable_pair
        cfg = ToolSearchConfig.from_raw({})
        assert not assemble_tool_defs(
            [_td("terminal", "Run shell")], context_length=200_000, config=cfg,
        ).activated
        assert assemble_tool_defs(
            [_td("terminal", "Run shell"), plugin],
            context_length=200_000, config=cfg,
        ).activated

    def test_rebuilding_the_session_snapshot_never_drops_a_tool(
        self, deferrable_pair
    ):
        """End-to-end through ``get_tool_definitions``, the real entry point the
        agent (``agent_init``) and every MCP rebuild
        (``refresh_agent_mcp_tools``) use."""
        from tools.registry import discover_builtin_tools
        from tools.tool_search import BRIDGE_TOOL_NAMES, DeferralSession

        import model_tools

        discover_builtin_tools()
        session = DeferralSession()
        first = model_tools.get_tool_definitions(
            enabled_toolsets=["terminal"], quiet_mode=True,
            deferral_session=session,
        )
        assert session.activated is False
        first_names = {t["function"]["name"] for t in first}

        # An MCP server lands and the agent rebuilds its snapshot with the same
        # latch: the new tools are added, the existing ones are untouched.
        second = model_tools.get_tool_definitions(
            enabled_toolsets=["terminal", "pytestlatch"], quiet_mode=True,
            deferral_session=session,
        )
        second_names = {t["function"]["name"] for t in second}
        assert first_names <= second_names
        assert "pytestlatch_probe" in second_names
        assert not (BRIDGE_TOOL_NAMES & second_names)

        # The latched surface is not written into the process-wide memo: a
        # caller without a session over the SAME toolsets still sees the live
        # decision (deferred), as it did before the latch existed.
        live = model_tools.get_tool_definitions(
            enabled_toolsets=["terminal", "pytestlatch"], quiet_mode=True,
        )
        live_names = {t["function"]["name"] for t in live}
        assert BRIDGE_TOOL_NAMES <= live_names
        assert "pytestlatch_probe" not in live_names

    def test_a_failed_first_assembly_pins_the_session_eager(self, deferrable_pair):
        """The actual reported trigger, as a contract.

        The very first assemblies of that session could not run at all —
        ``assemble_tool_defs`` raised ``No module named 'snowballstemmer'``,
        which ``model_tools`` swallows into a WARNING — so the session shipped
        the pre-assembly list (every tool direct). When the dependency landed
        hours later the assembly started working, turned the bridge on, and
        pulled the plugin tools out of the model's array. A session that
        started eager must stay eager whatever the reason the first assembly
        was eager.
        """
        from unittest.mock import patch

        from tools.tool_search import BRIDGE_TOOL_NAMES, DeferralSession

        import model_tools
        import tools.tool_search as ts

        session = DeferralSession()
        with patch.object(
            ts, "assemble_tool_defs",
            side_effect=ImportError("No module named 'snowballstemmer'"),
        ):
            first = model_tools.get_tool_definitions(
                enabled_toolsets=["pytestlatch"], quiet_mode=True,
                deferral_session=session,
            )
        first_names = {t["function"]["name"] for t in first}
        assert session.activated is False
        assert not (BRIDGE_TOOL_NAMES & first_names)
        assert "pytestlatch_probe" in first_names

        # The dependency arrives; the assembly works again. The latched session
        # keeps the surface it has been serving all along.
        later = model_tools.get_tool_definitions(
            enabled_toolsets=["pytestlatch"], quiet_mode=True,
            deferral_session=session,
        )
        later_names = {t["function"]["name"] for t in later}
        assert not (BRIDGE_TOOL_NAMES & later_names)
        assert first_names <= later_names
        assert "pytestlatch_probe" in later_names

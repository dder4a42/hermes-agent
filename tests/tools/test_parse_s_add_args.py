"""Tests for tools.thought_tools.parse_s_add_args — shared flag parser
used by both the CLI and the gateway /s add handlers."""
from __future__ import annotations

from tools.thought_tools import parse_s_add_args


def test_empty_returns_blank_defaults():
    r = parse_s_add_args("")
    assert r["title"] == ""
    assert r["schedule_raw"] == ""
    assert r["attendees"] == []
    assert r["checklist"] == []


def test_bare_title_no_when():
    r = parse_s_add_args('"just a reminder"')
    assert r["title"] == "just a reminder"
    assert r["schedule_raw"] == ""


def test_title_and_when_basic():
    r = parse_s_add_args('"drink water" --when "tomorrow 8am"')
    assert r["title"] == "drink water"
    assert r["schedule_raw"] == "tomorrow 8am"


def test_title_from_multiple_tokens_no_quotes():
    r = parse_s_add_args('drink water --when tomorrow')
    assert r["title"] == "drink water"
    assert r["schedule_raw"] == "tomorrow"


def test_full_option_set():
    r = parse_s_add_args(
        '"Design review" --when "明天下午3点" '
        '--url "https://meet.example/xyz" '
        '--where "Room 42" '
        '--attendees Bob,Alice,Carol '
        '--tags meeting,urgent '
        '--remind-before 30m '
        '--checklist "arch diagram;3 benchmarks;update slides" '
        '--notes "kickoff for Q3 planning"'
    )
    assert r["title"] == "Design review"
    assert r["schedule_raw"] == "明天下午3点"
    assert r["url"] == "https://meet.example/xyz"
    assert r["location"] == "Room 42"
    assert r["attendees"] == ["Bob", "Alice", "Carol"]
    assert r["tags"] == ["meeting", "urgent"]
    assert r["remind_before"] == "30m"
    assert r["checklist"] == ["arch diagram", "3 benchmarks", "update slides"]
    assert r["notes"] == "kickoff for Q3 planning"


def test_where_alias_location():
    r_where = parse_s_add_args('t --when now --where "Room 42"')
    r_loc = parse_s_add_args('t --when now --location "Room 42"')
    assert r_where["location"] == r_loc["location"] == "Room 42"


def test_lead_alias_remind_before():
    r1 = parse_s_add_args('t --when now --remind-before 15m')
    r2 = parse_s_add_args('t --when now --lead 15m')
    assert r1["remind_before"] == r2["remind_before"] == "15m"


def test_attendees_alias_with():
    r1 = parse_s_add_args('t --when now --attendees a,b')
    r2 = parse_s_add_args('t --when now --with a,b')
    assert r1["attendees"] == r2["attendees"] == ["a", "b"]


def test_flag_missing_value_ignored():
    # Trailing --when with no value shouldn't crash.
    r = parse_s_add_args('t --when')
    assert r["title"] == "t"
    assert r["schedule_raw"] == ""


def test_checklist_semicolon_split_dedups_whitespace():
    r = parse_s_add_args('t --when now --checklist " a ; b ;  ; c "')
    assert r["checklist"] == ["a", "b", "c"]


def test_bad_shlex_falls_back_to_split():
    # Unclosed quote -- parser must not crash.
    r = parse_s_add_args('t --when tomorrow --notes "unclosed')
    # We just want no exception; content is best-effort.
    assert isinstance(r["title"], str)

"""Adversarial output from the hints model must come out as safe lines or nothing.

Spec: trc-backend docs/superpowers/specs/2026-09-16-interim-status-while-answering-design.md §5.
"""

from __future__ import annotations

import json

import pytest

from gateway.platforms.turn_status import STATUS_TEXT_RE, sanitize_lines


def test_a_json_array_becomes_ellipsis_lines_in_order():
    raw = json.dumps(["Looking through recent statements", "Checking the agreement terms"])
    assert sanitize_lines(raw, 4) == [
        "Looking through recent statements…",
        "Checking the agreement terms…",
    ]


def test_a_fenced_json_array_is_parsed():
    raw = '```json\n["Reviewing the account file"]\n```'
    assert sanitize_lines(raw, 4) == ["Reviewing the account file…"]


def test_newline_separated_text_is_split_and_bullets_are_stripped():
    raw = "- Looking through recent statements\n2. Checking the agreement terms\n"
    assert sanitize_lines(raw, 4) == [
        "Looking through recent statements…",
        "Checking the agreement terms…",
    ]


def test_quotes_and_trailing_punctuation_are_normalised():
    raw = '"Looking through recent statements..."\n'
    assert sanitize_lines(raw, 4) == ["Looking through recent statements…"]


@pytest.mark.parametrize(
    "bad",
    [
        "Looking up <PERSON_1> now",  # token
        "Found 3 matching documents",  # digit
        "Checking [S1] and [S2]",  # markers
        "Balance as of 2024-01-01",  # date
        "See https://example.com",  # link
        "Emailing a@b.com",  # address
        "Reading {client} file",  # braces
        "Short",  # under 7 characters
        "A" * 90,  # over 89 characters
        "Done ✅",  # emoji
        "",
        "   ",
    ],
)
def test_forbidden_shapes_are_dropped(bad):
    assert sanitize_lines(json.dumps([bad]), 4) == []


def test_bad_lines_are_dropped_and_good_ones_kept():
    raw = json.dumps(["Found 3 documents", "Looking through recent statements"])
    assert sanitize_lines(raw, 4) == ["Looking through recent statements…"]


def test_duplicates_differing_by_case_collapse_to_one():
    raw = json.dumps(["Checking the details", "checking the details", "Checking The Details"])
    assert sanitize_lines(raw, 4) == ["Checking the details…"]


def test_max_lines_is_respected():
    raw = json.dumps(["Line number one here", "Line number two here", "Line number three"])
    assert sanitize_lines(raw, 2) == ["Line number one here…", "Line number two here…"]


def test_json_that_is_not_an_array_of_strings_falls_back_to_lines():
    raw = json.dumps({"lines": ["x"]})  # a dict: not our shape → treated as plain lines
    assert sanitize_lines(raw, 4) == []  # and the one line contains braces, so it is dropped


def test_non_string_array_items_are_ignored():
    raw = json.dumps(["Looking through recent statements", 42, None, ["nested"]])
    assert sanitize_lines(raw, 4) == ["Looking through recent statements…"]


def test_garbage_and_none_yield_nothing():
    assert sanitize_lines(None, 4) == []
    assert sanitize_lines("\x00\x01", 4) == []


def test_every_survivor_matches_the_regex():
    raw = json.dumps(["Looking through recent statements", "Checking the agreement terms"])
    for line in sanitize_lines(raw, 4):
        assert STATUS_TEXT_RE.match(line)


def test_max_lines_zero_returns_nothing():
    assert sanitize_lines(json.dumps(["Looking through recent statements"]), 0) == []

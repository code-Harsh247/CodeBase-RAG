from __future__ import annotations

import pytest

from retrieval.locations import Location, dedupe, locations_from_rows


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("src/x.py", Location("src/x.py", None, None)),
        ("src/x.py:42", Location("src/x.py", 42, None)),
        ("src/x.py:42-80", Location("src/x.py", 42, 80)),
        ("  src/x.py:42  ", Location("src/x.py", 42, None)),
        ("src\\x.py:42", Location("src/x.py", 42, None)),
    ],
)
def test_parse(text, expected):
    assert Location.parse(text) == expected


def test_parse_rejects_nonsense():
    with pytest.raises(ValueError):
        Location.parse("a:b:c:d")


def test_covers_requires_the_same_file():
    assert not Location("a.py", 10).covers(Location("b.py", 10))


def test_covers_exact_line():
    assert Location("a.py", 10).covers(Location("a.py", 10))


def test_covers_within_tolerance():
    # A decorator or a slightly-off chunk boundary should not read as a miss.
    assert Location("a.py", 10).covers(Location("a.py", 12))
    assert not Location("a.py", 10).covers(Location("a.py", 40))


def test_a_range_covers_lines_inside_it():
    chunk = Location("a.py", 40, 80)
    assert chunk.covers(Location("a.py", 60))
    assert chunk.covers(Location("a.py", 40))
    assert not chunk.covers(Location("a.py", 200))


def test_whole_file_locations_match_anything_in_that_file():
    assert Location("a.py").covers(Location("a.py", 999))
    assert Location("a.py", 5).covers(Location("a.py"))


def test_str_roundtrip():
    assert str(Location("a.py")) == "a.py"
    assert str(Location("a.py", 5)) == "a.py:5"
    assert str(Location("a.py", 5, 9)) == "a.py:5-9"
    # A range that is really a point renders as a point.
    assert str(Location("a.py", 5, 5)) == "a.py:5"


def test_locations_from_rows_accepts_common_column_aliases():
    rows = [
        {"file": "a.py", "line": 10},
        {"file_path": "b.py", "start_line": 20},
        {"path": "c.py", "start": 30},
    ]
    assert locations_from_rows(rows) == [
        Location("a.py", 10),
        Location("b.py", 20),
        Location("c.py", 30),
    ]


def test_locations_from_rows_is_case_insensitive_about_column_names():
    assert locations_from_rows([{"FILE": "a.py", "START_LINE": 3}]) == [Location("a.py", 3)]


def test_locations_from_rows_skips_rows_without_a_file():
    assert locations_from_rows([{"caller": "x", "count": 3}]) == []


def test_locations_from_rows_tolerates_a_missing_line():
    assert locations_from_rows([{"file": "a.py"}]) == [Location("a.py", None, None)]


def test_locations_from_rows_treats_line_zero_as_absent():
    # `:0` is the fabricated-line-number artifact; it must not become a location.
    assert locations_from_rows([{"file": "a.py", "line": 0}]) == [Location("a.py", None, None)]


def test_dedupe_preserves_first_seen_order():
    locations = [Location("b.py", 1), Location("a.py", 2), Location("b.py", 1)]
    assert dedupe(locations) == [Location("b.py", 1), Location("a.py", 2)]

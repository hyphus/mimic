from __future__ import annotations

import pytest

from mimic.dispatch import path_matches_prefix
from mimic.modules.loader import find_path_conflicts, normalize_prefix, prefixes_conflict


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "/"),
        ("/", "/"),
        ("/v2/", "/v2"),
        ("v2", "/v2"),
        ("  /webhook/  ", "/webhook"),
        ("/a/b/", "/a/b"),
    ],
)
def test_normalize_prefix(raw, expected):
    assert normalize_prefix(raw) == expected


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("/v2", "/v2", True),
        ("/v2/", "/v2", True),
        ("/v2", "/v2/sub", True),
        ("/v2/sub", "/v2", True),
        ("/v2", "/v21", False),
        ("/v2", "/httpbin", False),
    ],
)
def test_prefixes_conflict(a, b, expected):
    assert prefixes_conflict(a, b) is expected


def test_find_path_conflicts_first_wins():
    conflicts = find_path_conflicts(
        [("a", "/v2"), ("b", "/v2/sub"), ("c", "/httpbin")]
    )
    assert conflicts == [("a", "/v2", "b", "/v2/sub")]


def test_find_path_conflicts_none():
    assert find_path_conflicts([("a", "/v2"), ("b", "/httpbin")]) == []


@pytest.mark.parametrize(
    "path,prefix,expected",
    [
        ("/v2/", "/v2/", True),
        ("/v2", "/v2/", True),
        ("/v2/_catalog", "/v2", True),
        ("/v21", "/v2", False),
        ("/anything", "/", False),
    ],
)
def test_path_matches_prefix(path, prefix, expected):
    assert path_matches_prefix(path, prefix) is expected

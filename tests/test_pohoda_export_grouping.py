"""Tests for grouping export rows by pohoda_ico (app.main)."""
from app.main import _group_by_pohoda_ico


def test_single_group_when_ico_matches():
    rows = [{"id": "1", "pohoda_ico": "12345678"}, {"id": "2", "pohoda_ico": "12345678"}]
    groups = _group_by_pohoda_ico(rows)
    assert list(groups.keys()) == ["12345678"]
    assert len(groups["12345678"]) == 2


def test_blank_and_none_ico_share_one_group():
    rows = [{"id": "1", "pohoda_ico": None}, {"id": "2", "pohoda_ico": ""}, {"id": "3"}]
    groups = _group_by_pohoda_ico(rows)
    assert list(groups.keys()) == [""]
    assert len(groups[""]) == 3


def test_ico_is_stripped():
    rows = [{"id": "1", "pohoda_ico": "  12345678  "}]
    groups = _group_by_pohoda_ico(rows)
    assert list(groups.keys()) == ["12345678"]


def test_distinct_icos_split_into_multiple_groups():
    rows = [
        {"id": "1", "pohoda_ico": "11111111"},
        {"id": "2", "pohoda_ico": "22222222"},
        {"id": "3", "pohoda_ico": ""},
    ]
    groups = _group_by_pohoda_ico(rows)
    assert set(groups.keys()) == {"11111111", "22222222", ""}


def test_empty_rows_yields_empty_groups():
    assert _group_by_pohoda_ico([]) == {}

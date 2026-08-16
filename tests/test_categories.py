"""normalize_category: exact case-insensitive match against the fixed
CATEGORIES vocabulary, plus tolerance for a trailing-s plural/singular
mismatch in either direction. Not fuzzy matching — an unrelated word must
still be rejected so /log and /setbudget can't drift into inventing
untracked categories.
"""

from __future__ import annotations

from app.categories import normalize_category


def test_exact_case_insensitive_match():
    assert normalize_category("food") == "Food"
    assert normalize_category("FOOD") == "Food"
    assert normalize_category("  Transport  ") == "Transport"


def test_plural_input_matches_singular_canonical_label():
    assert normalize_category("others") == "Other"
    assert normalize_category("Others") == "Other"
    assert normalize_category("foods") == "Food"


def test_singular_input_matches_plural_canonical_label():
    assert normalize_category("bill") == "Bills"
    assert normalize_category("Bill") == "Bills"


def test_unrelated_word_is_still_rejected():
    assert normalize_category("travel") is None
    assert normalize_category("groceries") is None
    assert normalize_category("") is None

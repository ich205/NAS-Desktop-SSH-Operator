import pytest

from jfo.core.quoting import bash_quote, bash_array_literal, QuoteError


def test_bash_quote_simple():
    assert bash_quote("abc") == "'abc'"


def test_bash_quote_spaces_and_stars():
    assert bash_quote("A* B") == "'A* B'"


def test_bash_quote_single_quote():
    # a'b -> 'a'"'"'b'
    assert bash_quote("a'b") == "'a'\"'\"'b'"


def test_bash_quote_reject_nul():
    with pytest.raises(QuoteError):
        bash_quote("a\x00b")


def test_bash_array_literal():
    arr = bash_array_literal(["/a b", "c"]) 
    assert arr.startswith("(") and arr.endswith(")")
    assert "'/a b'" in arr

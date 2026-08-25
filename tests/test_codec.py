"""What a store can hold as a value, and what it reads back."""

from enum import IntEnum

import pytest

from cachefy.codec import as_written, exact
from cachefy.errors import UnwritableValue
from cachefy.store.base import VALUE_LIMIT, WHOLE_CEILING, WHOLE_FLOOR


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 7, 1.5, "", "five", [], {}, [1, "two", None], {"deep": {"er": [1.5]}}, "grüßen 😀", WHOLE_CEILING, WHOLE_FLOOR])
def test_a_value_every_store_holds_comes_back_unchanged(value):
    assert as_written(value, "the value") == value


def test_a_tuple_comes_back_as_the_list_a_store_answers_with():
    assert as_written(("a", "b"), "the value") == ["a", "b"]


def test_a_key_that_is_not_a_string_comes_back_as_the_string_one_answers_with():
    assert as_written({7: "seven"}, "the value") == {"7": "seven"}


@pytest.mark.parametrize("value", [{1, 2}, object(), lambda: 1])
def test_a_value_no_serialiser_takes_is_refused(value):
    with pytest.raises(UnwritableValue, match="not something a store can write down"):
        as_written(value, "the value")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), [float("nan")]])
def test_a_number_only_python_writes_as_a_word_is_refused(value):
    """SQLite and Redis hold what MySQL and PostgreSQL refuse, so no store is left to disagree."""
    with pytest.raises(UnwritableValue):
        as_written(value, "the value")


def test_a_character_no_store_holds_inside_a_value_is_refused():
    with pytest.raises(UnwritableValue):
        as_written({"path": "\udcff"}, "the value")


def test_a_value_that_holds_itself_is_refused_rather_than_walked_for_ever():
    looping = []
    looping.append(looping)

    with pytest.raises(UnwritableValue):
        as_written(looping, "the value")


@pytest.mark.parametrize("value", [WHOLE_CEILING + 1, WHOLE_FLOOR - 1, [10**40], {"n": 10**40}, [[10**40]]])
def test_a_whole_number_one_store_would_read_back_as_another_number_is_refused(value):
    """MySQL turns a whole number either side of the range into a double, and nothing anywhere says so."""
    with pytest.raises(UnwritableValue, match="past the whole numbers"):
        as_written(value, "the value")


def test_a_boolean_is_never_read_as_the_whole_number_python_says_it_is():
    assert exact(True, "the value") is None
    assert as_written({"ok": True}, "the value") == {"ok": True}


def test_a_key_that_wide_is_settled_rather_than_refused():
    """JSON names every key with a string, so a value keyed by a number that wide is one every store already agrees on."""
    assert as_written({10**40: "wide"}, "the value") == {"1" + "0" * 40: "wide"}


def test_a_value_past_what_a_store_keeps_one_as_is_refused():
    with pytest.raises(UnwritableValue, match="characters written down"):
        as_written("x" * (VALUE_LIMIT + 1), "the value")


def test_a_value_right_up_to_the_limit_is_taken():
    assert as_written("x" * (VALUE_LIMIT - 2), "the value") == "x" * (VALUE_LIMIT - 2)


class Big(int):
    """A whole number of a caller's own, which is what an id or a quantity is very often written as."""


class Counted(IntEnum):
    ONE = 1


@pytest.mark.parametrize("value", [Big(WHOLE_CEILING + 1), Big(WHOLE_FLOOR - 1), [Big(10**40)], {"n": Big(10**40)}])
def test_a_whole_number_of_a_callers_own_is_answered_for_like_any_other(value):
    """Json writes an int subclass out as the number it is, so one past the range slipped through where a plain int was refused."""
    with pytest.raises(UnwritableValue, match="past the whole numbers"):
        as_written(value, "the value")


@pytest.mark.parametrize("value", [Big(7), Counted.ONE, [Big(7)], {"n": Counted.ONE}])
def test_a_whole_number_of_a_callers_own_inside_the_range_is_kept(value):
    assert as_written(value, "the value") == value

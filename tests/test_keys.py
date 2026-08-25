"""What names an entry, and what nothing could name one by."""

from enum import Enum

import pytest

from cachefy.errors import CacheError
from cachefy.keys import DIGEST, about, built, digested, holdable, joined, named, plain, spaced, writable


class Renamed(str, Enum):
    """A name written the way a lot of code writes one, whose text and whose rendering are not the same."""

    USERS = "users"


class Lying(str):
    def __str__(self) -> str:
        return "somebody else"


@pytest.mark.parametrize("value", ["42", "grüßen 😀", "a" * 255])
def test_text_every_store_can_write_is_taken(value):
    assert writable(value, "the key") is None


@pytest.mark.parametrize("value", [None, 7, b"bytes", ["a"]])
def test_what_is_not_text_is_refused_before_it_reaches_an_encoder(value):
    with pytest.raises(CacheError, match="text"):
        plain(value, "the key")


def test_a_character_no_store_can_write_is_refused():
    """A lone surrogate is what a posix path carries when the bytes behind it were never utf-8."""
    with pytest.raises(CacheError, match="no store can write down"):
        writable("path/\udcff/file", "the key")


def test_a_nul_byte_is_refused_because_one_store_refuses_it_alone():
    with pytest.raises(CacheError, match="nul byte"):
        writable("with\x00nul", "the key")


def test_a_name_of_no_characters_names_nothing():
    with pytest.raises(CacheError, match="empty"):
        holdable("", 10, "the key")


def test_a_name_past_what_a_column_holds_is_refused():
    with pytest.raises(CacheError, match="10 characters and a store keeps 5"):
        holdable("x" * 10, 5, "the key")


def test_a_key_is_answered_for_by_what_every_store_can_hold():
    assert named("42") == "42"

    with pytest.raises(CacheError):
        named("x" * 256)


def test_a_space_may_not_hold_the_character_that_joins_it_to_a_key():
    assert spaced("users") == "users"

    with pytest.raises(CacheError, match="colon"):
        spaced("users:live")


def test_two_pairs_never_spell_one_name():
    """A space called 'user' holding 'a:b' and one called 'user:a' holding 'b' must never read each other's entries."""
    assert joined("user", "a:b") != joined("usera", "b")
    assert joined("user", "a") == "4:user:a"


def test_a_digest_is_the_same_in_every_process():
    """Python salts `hash` per process, so a key built with that one names an entry only the process that wrote it can read."""
    assert digested("anything") == digested("anything")
    assert len(digested("anything")) == DIGEST * 2
    assert digested("a") != digested("b")


def test_a_call_is_named_by_its_arguments_sorted():
    assert built({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert built({"a": 1, "b": 2}) == built({"b": 2, "a": 1})


def test_a_call_too_long_to_write_out_is_named_by_its_digest():
    long = built({"tags": [f"tag-{index}" for index in range(200)]})

    assert len(long) == DIGEST * 2
    assert long != built({"tags": ["one"]})


def test_a_call_nothing_could_name_says_so():
    with pytest.raises(CacheError, match="cannot be written down"):
        built({"who": object()})

    with pytest.raises(CacheError, match="cannot be written down"):
        built({"how_many": float("nan")})


def test_a_call_carrying_a_character_no_store_writes_is_refused():
    with pytest.raises(CacheError, match="no store can write down"):
        built({"path": "\udcff"})


@pytest.mark.parametrize("value", ["a\udcffb", "a\x00b", "grüßen 😀\udcff"])
def test_a_refusal_about_a_name_nothing_can_write_can_itself_be_written(value):
    """A message carrying the character it is refusing is one that raises where somebody logs it or answers with it."""
    with pytest.raises(CacheError) as refused:
        named(value)

    said = str(refused.value)

    assert said.encode("utf-8"), "the message is text anything can write down"
    assert "\x00" not in said, "and holds nothing a log would cut short"
    assert "the key" in said, "while still naming what was wrong"


def test_a_refusal_about_something_that_is_not_a_name_never_tries_to_spell_it():
    with pytest.raises(CacheError, match="the key is int"):
        named(7)

    assert about("the key", 7) == "the key"
    assert about("the key", "42") == "the key '42'"


@pytest.mark.parametrize("value", [Renamed.USERS, Lying("users"), "users"])
def test_a_name_is_answered_as_the_text_it_holds(value):
    """A str subclass may render as something else entirely, and what travels to a store has to be the text."""
    assert named(value) == "users" and type(named(value)) is str
    assert spaced(value) == "users" and type(spaced(value)) is str
    assert plain(value, "the key") == "users"

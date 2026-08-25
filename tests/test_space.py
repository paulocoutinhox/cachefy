"""Every call a space answers, against every store."""

import asyncio
from datetime import timedelta

import pytest

from cachefy.entry import MISS
from cachefy.errors import CacheError, UnwritableValue
from cachefy.store.base import BATCH_LIMIT

# Long enough that nothing dies while a test is looking at it, and short enough that a test can wait one out.
BRIEF = timedelta(milliseconds=30)
WHILE = 0.09


async def test_a_name_answers_what_was_written_under_it(app):
    users = app.space("users")

    assert await users.set("42", {"name": "paulo"}) is not None
    assert await users.get("42") == {"name": "paulo"}


async def test_a_name_nobody_wrote_answers_the_default(app):
    users = app.space("users")

    assert await users.get("42") is MISS
    assert await users.get("42", default=None) is None
    assert await users.get("42", default="nothing") == "nothing"


async def test_a_value_of_none_is_told_apart_from_a_name_holding_nothing(app):
    users = app.space("users")
    await users.set("42", None)

    assert await users.get("42") is None
    assert await users.get("43") is MISS


async def test_a_value_stops_being_answered_once_it_has_died(app):
    users = app.space("users", ttl=timedelta(hours=1))
    await users.set("42", 1)

    assert await users.get("42") == 1

    # The lifetime is brought forward rather than waited out, so what this asks about is the instant and never how fast the machine is.
    assert await users.touch("42", ttl=BRIEF) is True

    await asyncio.sleep(WHILE)

    assert await users.get("42") is MISS


async def test_a_call_may_keep_a_value_for_longer_than_the_space_says(app):
    users = app.space("users", ttl=BRIEF)
    await users.set("42", 1, ttl=timedelta(hours=1))

    await asyncio.sleep(WHILE)

    assert await users.get("42") == 1


async def test_a_call_may_keep_a_value_until_somebody_drops_it(app):
    users = app.space("users", ttl=BRIEF)
    await users.set("42", 1, ttl=None)

    await asyncio.sleep(WHILE)

    assert await users.get("42") == 1


async def test_the_whole_entry_is_there_for_a_caller_writing_a_value_back(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    written = await users.set("42", 1)

    held = await users.entry("42")

    assert (held.space, held.key, held.value, held.version) == ("users", "42", 1, written.version)
    assert held.expires_at is not None
    assert await users.entry("nothing") is None


async def test_adding_takes_a_name_only_while_it_holds_nothing(app):
    users = app.space("users")

    assert await users.add("42", 1) is not None
    assert await users.add("42", 2) is None
    assert await users.get("42") == 1


async def test_a_value_written_back_lands_only_while_nobody_wrote_in_between(app):
    users = app.space("users")
    written = await users.set("42", 1)

    assert await users.swap("42", 2, written.version) is not None
    assert await users.swap("42", 3, written.version) is None
    assert await users.get("42") == 2


async def test_dropping_takes_the_name_away(app):
    users = app.space("users")
    await users.set("42", 1)

    assert await users.drop("42") is True
    assert await users.get("42") is MISS
    assert await users.drop("42") is False


async def test_touching_keeps_a_value_that_was_about_to_die(app):
    # The instant it dies at is read rather than waited for, because a space that dies in thirty milliseconds is a race the slowest store loses.
    users = app.space("users", ttl=timedelta(seconds=30))
    await users.set("42", 1)
    dying = (await users.entry("42")).expires_at

    assert await users.touch("42", ttl=timedelta(hours=1)) is True

    moved = await users.entry("42")

    assert moved.expires_at > dying, "the instant it dies at was never moved out"
    assert moved.value == 1, "touching changed what the name holds"
    assert await users.touch("nothing") is False


async def test_many_names_are_read_at_once(app):
    users = app.space("users")
    await users.set("a", 1)
    await users.set("b", None)

    assert await users.get_many(["a", "b", "never"]) == {"a": 1, "b": None}
    assert await users.get_many([]) == {}


async def test_many_names_are_read_in_batches_no_statement_would_refuse(app):
    """A statement naming everything a caller asked for is one a database refuses, so what reaches the store is bounded."""
    users = app.space("users")
    batches = []
    reading = app.store.read_many

    async def counted(space, keys, moment):
        batches.append(len(keys))

        return await reading(space, keys, moment)

    app.store.read_many = counted

    try:
        await users.set("a", 1)

        assert await users.get_many([f"k{index}" for index in range(2500)] + ["a"]) == {"a": 1}
    finally:
        app.store.read_many = reading

    assert batches == [BATCH_LIMIT, BATCH_LIMIT, 501], "every batch is one a statement may name"


async def test_a_count_starts_at_nothing_and_adds_up(app):
    limits = app.space("limits")

    assert await limits.incr("paulo") == 1
    assert await limits.incr("paulo", 4) == 5
    assert await limits.incr("paulo", -2) == 3
    assert await limits.get("paulo") == 3


async def test_a_name_holding_something_that_is_not_a_count_answers_nothing(app):
    limits = app.space("limits")
    await limits.set("paulo", "five")

    assert await limits.incr("paulo") is None


async def test_a_count_dies_with_the_window_it_was_first_given(app):
    limits = app.space("limits", ttl=timedelta(hours=1))
    await limits.incr("paulo")
    await limits.incr("paulo")

    assert await limits.touch("paulo", ttl=BRIEF) is True

    await asyncio.sleep(WHILE)

    assert await limits.incr("paulo") == 1


async def test_clearing_a_space_forgets_everything_it_held(app):
    users = app.space("users")
    posts = app.space("posts")
    await users.set("a", 1)
    await users.set("b", 2)
    await posts.set("c", 3)

    assert await users.clear() == 2
    assert await users.get("a") is MISS
    assert await posts.get("c") == 3


async def test_a_space_says_how_much_it_holds(app):
    users = app.space("users")
    await users.set("a", 1)
    await users.set("b", 2)

    assert await users.count() == 2

    await users.drop("a")

    assert await users.count() == 1


async def test_two_spaces_never_read_each_others_names(app):
    users = app.space("users")
    posts = app.space("posts")
    await users.set("42", "a user")
    await posts.set("42", "a post")

    assert await users.get("42") == "a user"
    assert await posts.get("42") == "a post"


@pytest.mark.parametrize("key", ["", "x" * 256, None, 7, "with\x00nul"])
async def test_a_key_no_store_could_tell_apart_is_refused_where_it_is_written(app, key):
    users = app.space("users")

    with pytest.raises(CacheError):
        await users.get(key)


@pytest.mark.parametrize("value", [{1, 2}, object(), float("nan"), float("inf"), 10**40, {"path": "\udcff"}])
async def test_a_value_no_store_could_write_is_refused_where_it_is_written(app, value):
    users = app.space("users")

    with pytest.raises(UnwritableValue):
        await users.set("42", value)


async def test_a_value_that_holds_itself_is_refused_and_never_walked_for_ever(app):
    users = app.space("users")
    looping = {}
    looping["self"] = looping

    with pytest.raises(UnwritableValue):
        await users.set("42", looping)


async def test_a_value_past_what_a_store_keeps_one_as_is_refused(app):
    users = app.space("users")

    with pytest.raises(UnwritableValue):
        await users.set("42", "x" * (2 * 1024 * 1024))


async def test_a_value_survives_the_trip_as_every_store_reads_it_back(app):
    """A tuple comes back as a list and a key that is not a string comes back as one, so what is kept is settled where it is written."""
    users = app.space("users")
    await users.set("42", {"tags": ("a", "b"), 7: "seven"})

    assert await users.get("42") == {"tags": ["a", "b"], "7": "seven"}


async def test_a_caller_that_changes_what_it_read_never_changes_the_entry(app):
    """A value handed out is one many callers hold at once, so one of them changing it must not change what the others read."""
    users = app.space("users")
    await users.set("42", {"tags": ["a"]})
    await users.set("43", {"tags": ["a"]})

    one = await users.get("42")
    one["tags"].append("b")

    many = await users.get_many(["43"])
    many["43"]["tags"].append("b")

    assert await users.get("42") == {"tags": ["a"]}
    assert await users.get("43") == {"tags": ["a"]}, "reading many names hands out copies exactly as reading one does"


async def test_a_caller_that_goes_on_changing_what_it_wrote_never_changes_the_entry(app):
    users = app.space("users")
    handed = {"name": "paulo"}
    await users.set("42", handed)
    handed["name"] = "somebody else"

    assert await users.get("42") == {"name": "paulo"}


async def test_a_counter_is_moved_by_a_whole_number_of_a_callers_own(app):
    """A `set` takes an int subclass, so an `incr` refusing one would be an inconsistency with no reason behind it."""
    limits = app.space("limits")

    class Step(int):
        pass

    assert await limits.incr("paulo", Step(5)) == 5

    with pytest.raises(CacheError, match="what a counter moves by is a whole number"):
        await limits.incr("paulo", True)

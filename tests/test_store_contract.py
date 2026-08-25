"""One suite, every store. What a store promises is the same wherever the entries live, and this is what keeps that true."""

from datetime import datetime, timedelta, timezone

import pytest

from cachefy.clock import now
from cachefy.entry import Entry, minted
from cachefy.store.base import COUNTER_CEILING, COUNTER_FLOOR, Store

SPACE = "users"


def an_entry(**overrides) -> Entry:
    return Entry(**{"space": SPACE, "key": "42", "value": {"name": "paulo"}} | overrides)


async def test_a_written_name_holds_what_it_was_given(store):
    moment = now()
    written = an_entry()

    assert (await store.write(written, moment)).version == written.version
    assert (await store.read(SPACE, "42", moment)).value == {"name": "paulo"}
    assert (await store.read(SPACE, "42", moment)).version == written.version


async def test_a_name_nobody_wrote_holds_nothing(store):
    assert await store.read(SPACE, "nothing", now()) is None


async def test_a_value_of_none_is_a_value_and_not_a_name_holding_nothing(store):
    """A function that legitimately answers nothing is one whose answer must still be kept."""
    moment = now()
    await store.write(an_entry(value=None), moment)

    held = await store.read(SPACE, "42", moment)

    assert held is not None
    assert held.value is None


async def test_every_write_carries_a_version_no_other_write_of_that_name_has(store):
    """A version seen twice is a value written back over one nobody read, so no two writes may ever share one."""
    moment = now()
    seen = set()

    for round_number in range(20):
        written = await store.write(an_entry(value=round_number), moment)
        seen.add(written.version)

        assert (await store.read(SPACE, "42", moment)).version == written.version

    assert len(seen) == 20


async def test_a_write_over_a_name_whose_value_died_carries_a_version_of_its_own(store):
    """A version that started again wherever a name came back from holding nothing is one a swap could be fooled by."""
    moment = now()
    first = await store.write(an_entry(value=1, expires_at=moment - timedelta(seconds=1)), moment)
    second = await store.write(an_entry(value=2), moment)

    assert second.version != first.version


async def test_a_value_stops_being_there_at_the_instant_it_dies(store):
    moment = now()
    dies = moment + timedelta(minutes=5)
    await store.write(an_entry(expires_at=dies), moment)

    assert await store.read(SPACE, "42", dies - timedelta(microseconds=1)) is not None
    assert await store.read(SPACE, "42", dies) is None


async def test_a_name_written_with_no_lifetime_is_kept_until_somebody_drops_it(store):
    moment = now()
    await store.write(an_entry(), moment)

    assert await store.read(SPACE, "42", moment + timedelta(days=3650)) is not None


async def test_every_field_of_an_entry_survives_the_trip_to_the_store_and_back(store):
    """A field the store forgets is a policy the cache silently stops honouring."""
    moment = now()
    dies, stales = moment + timedelta(minutes=5), moment + timedelta(minutes=1)
    await store.write(an_entry(value=[1, "two", None], expires_at=dies, stale_at=stales, created_at=moment), moment)

    held = await store.read(SPACE, "42", moment)

    assert (held.space, held.key, held.value) == (SPACE, "42", [1, "two", None])
    assert (held.expires_at, held.stale_at, held.created_at) == (dies, stales, moment)


async def test_a_name_reads_back_only_under_the_space_it_was_written_in(store):
    moment = now()
    await store.write(an_entry(), moment)

    assert await store.read("elsewhere", "42", moment) is None


async def test_two_names_that_only_read_alike_are_two_names(store):
    """A collation folding case away behind the primary key is two callers reading and overwriting one value."""
    moment = now()
    await store.write(an_entry(key="Bob", value=1), moment)
    await store.write(an_entry(key="bob", value=2), moment)

    assert (await store.read(SPACE, "Bob", moment)).value == 1
    assert (await store.read(SPACE, "bob", moment)).value == 2
    assert await store.read(SPACE, "café", moment) is None


async def test_adding_takes_a_name_nothing_holds(store):
    moment = now()
    taken = an_entry(value=1)

    assert (await store.add(taken, moment)).version == taken.version
    assert await store.add(an_entry(value=2), moment) is None
    assert (await store.read(SPACE, "42", moment)).value == 1


async def test_adding_takes_a_name_whose_value_has_died(store):
    moment = now()
    first = await store.write(an_entry(value=1, expires_at=moment - timedelta(seconds=1)), moment)
    taken = an_entry(value=2)

    assert (await store.add(taken, moment)).version == taken.version
    assert taken.version != first.version, "a name taken back is never taken under a version the one before it had"
    assert (await store.read(SPACE, "42", moment)).value == 2


async def test_swapping_writes_only_over_the_version_the_caller_read(store):
    moment = now()
    written = await store.write(an_entry(value=1), moment)
    coming = an_entry(value=2)

    assert await store.swap(an_entry(value=3), minted(), moment) is None
    assert (await store.swap(coming, written.version, moment)).version == coming.version
    assert (await store.read(SPACE, "42", moment)).value == 2
    assert await store.swap(an_entry(value=4), written.version, moment) is None, "the version that was read is gone once it has been written back"


async def test_swapping_never_writes_over_a_name_that_holds_nothing(store):
    """A swap is a value written back and never a value written, so a name nobody holds is not one it may take."""
    moment = now()

    assert await store.swap(an_entry(value=2), minted(), moment) is None

    dying = await store.write(an_entry(value=1, expires_at=moment - timedelta(seconds=1)), moment)

    assert await store.swap(an_entry(value=2), dying.version, moment) is None
    assert await store.read(SPACE, "42", moment) is None


async def test_dropping_takes_the_name_away(store):
    moment = now()
    await store.write(an_entry(), moment)

    assert await store.drop(SPACE, "42") is True
    assert await store.read(SPACE, "42", moment) is None
    assert await store.drop(SPACE, "42") is False


async def test_dropping_answers_for_a_name_whose_value_had_already_died(store):
    moment = now()
    await store.write(an_entry(expires_at=moment - timedelta(seconds=1)), moment)

    assert await store.drop(SPACE, "42") is True


async def test_touching_moves_when_a_living_value_dies(store):
    moment = now()
    written = await store.write(an_entry(expires_at=moment + timedelta(seconds=1)), moment)
    later, moved = moment + timedelta(hours=1), minted()

    assert await store.touch(SPACE, "42", later, None, moved, moment) is True

    held = await store.read(SPACE, "42", moment)

    assert (held.expires_at, held.value) == (later, {"name": "paulo"})
    assert held.version == moved != written.version, "moving when a value dies is a change to the entry, so the version moves with it"


async def test_touching_a_name_that_holds_nothing_changes_nothing(store):
    moment = now()

    assert await store.touch(SPACE, "42", moment + timedelta(hours=1), None, minted(), moment) is False

    await store.write(an_entry(expires_at=moment - timedelta(seconds=1)), moment)

    assert await store.touch(SPACE, "42", moment + timedelta(hours=1), None, minted(), moment) is False


async def test_a_count_starts_at_nothing_and_adds_up(store):
    moment = now()

    assert await store.bump(SPACE, "hits", 1, None, minted(), moment) == 1
    assert await store.bump(SPACE, "hits", 4, None, minted(), moment) == 5
    assert await store.bump(SPACE, "hits", -2, None, minted(), moment) == 3
    assert (await store.read(SPACE, "hits", moment)).value == 3


async def test_a_count_keeps_the_window_it_was_first_given(store):
    """A rate limit is a window that starts on the first call, and never one that moves with every call after it."""
    moment = now()
    await store.bump(SPACE, "hits", 1, moment + timedelta(minutes=1), minted(), moment)
    await store.bump(SPACE, "hits", 1, moment + timedelta(hours=9), minted(), moment)

    assert (await store.read(SPACE, "hits", moment)).expires_at == moment + timedelta(minutes=1)


async def test_a_count_that_has_died_starts_again_at_nothing(store):
    moment = now()
    await store.bump(SPACE, "hits", 7, moment - timedelta(seconds=1), minted(), moment)

    assert await store.bump(SPACE, "hits", 1, None, minted(), moment) == 1


@pytest.mark.parametrize("value", ["five", True, 1.5, None, [1], {"a": 1}])
async def test_a_name_holding_something_that_is_not_a_count_cannot_be_counted_in(store, value):
    moment = now()
    await store.write(an_entry(key="hits", value=value), moment)

    assert await store.bump(SPACE, "hits", 1, None, minted(), moment) is None
    assert (await store.read(SPACE, "hits", moment)).value == value


@pytest.mark.parametrize("edge, step", [(COUNTER_CEILING, 1), (COUNTER_FLOOR, -1)])
async def test_a_count_never_leaves_what_every_store_adds_up_exactly(store, edge, step):
    moment = now()
    await store.write(an_entry(key="hits", value=edge), moment)

    assert await store.bump(SPACE, "hits", step, None, minted(), moment) is None
    assert await store.bump(SPACE, "hits", 0, None, minted(), moment) == edge


async def test_many_names_are_read_in_one_answer(store):
    moment = now()
    await store.write(an_entry(key="a", value=1), moment)
    await store.write(an_entry(key="b", value=2), moment)
    await store.write(an_entry(key="dead", value=3, expires_at=moment - timedelta(seconds=1)), moment)

    found = await store.read_many(SPACE, ("a", "b", "dead", "never"), moment)

    assert {key: entry.value for key, entry in found.items()} == {"a": 1, "b": 2}
    assert await store.read_many(SPACE, (), moment) == {}


async def test_clearing_a_space_takes_everything_it_held(store):
    moment = now()
    await store.write(an_entry(key="a", value=1), moment)
    await store.write(an_entry(key="b", value=2, expires_at=moment - timedelta(seconds=1)), moment)
    await store.write(an_entry(space="other", key="c", value=3), moment)

    assert await store.clear(SPACE) == 2
    assert await store.read(SPACE, "a", moment) is None
    assert (await store.read("other", "c", moment)).value == 3
    assert await store.clear("nowhere") == 0


async def test_sweeping_drops_what_was_already_dead(store):
    moment = now()
    await store.write(an_entry(key="gone", value=1, expires_at=moment - timedelta(minutes=5)), moment)
    await store.write(an_entry(key="here", value=2, expires_at=moment + timedelta(minutes=5)), moment)
    await store.write(an_entry(key="always", value=3), moment)

    assert await store.purge(moment, 100) == 1
    assert await store.purge(moment, 100) == 0
    assert (await store.read(SPACE, "here", moment)).value == 2
    assert (await store.read(SPACE, "always", moment)).value == 3


async def test_a_sweep_takes_an_entry_that_died_at_that_very_instant(store):
    """A read answers an entry dying exactly then as gone, so a sweep stopping a microsecond short leaves one row of every write behind for ever."""
    moment = now()
    dies = moment + timedelta(minutes=5)
    await store.write(an_entry(expires_at=dies), moment)

    assert await store.purge(dies - timedelta(microseconds=1), 100) == 0, "one that is still there is not one to drop"
    assert await store.purge(dies, 100) == 1


async def test_a_sweep_takes_no_more_than_the_batch_it_was_asked_for(store):
    moment = now()

    for index in range(5):
        await store.write(an_entry(key=f"gone-{index}", value=index, expires_at=moment - timedelta(minutes=5)), moment)

    assert await store.purge(moment, 2) == 2
    assert await store.purge(moment, 100) == 3


async def test_a_swept_name_can_be_written_again(store):
    moment = now()
    await store.write(an_entry(expires_at=moment - timedelta(minutes=5)), moment)
    await store.purge(moment, 100)

    written = await store.write(an_entry(value="back"), moment)

    assert (await store.read(SPACE, "42", moment)).value == "back"
    assert (await store.read(SPACE, "42", moment)).version == written.version


async def test_the_depth_of_the_cache_is_countable_by_space(store):
    moment = now()
    await store.write(an_entry(key="a", value=1), moment)
    await store.write(an_entry(key="dead", value=2, expires_at=moment - timedelta(seconds=1)), moment)
    await store.write(an_entry(space="other", key="c", value=3), moment)

    assert await store.count(SPACE, moment) == 1
    assert await store.count("other", moment) == 1
    assert await store.count(None, moment) == 2
    assert await store.count("nowhere", moment) == 0


async def test_every_method_of_the_interface_is_answered(store):
    """A store that forgets one of these fails here rather than in production."""
    promised = set(Store.__abstractmethods__)

    assert promised <= set(dir(store))
    assert not [name for name in promised if getattr(type(store), name) is getattr(Store, name)]


async def test_a_sweep_takes_an_entry_that_died_before_the_epoch(store):
    """A store that sweeps from the epoch rather than from the beginning leaves those lying there for ever, and no other store does."""
    moment = now()
    ancient = datetime(1960, 1, 1, tzinfo=timezone.utc)
    await store.write(Entry(space=SPACE, key="ancient", value=1, expires_at=ancient, created_at=ancient), moment)

    assert await store.purge(moment, 100) == 1
    assert await store.drop(SPACE, "ancient") is False

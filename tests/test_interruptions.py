"""Every round trip a store makes, refused one at a time, and what the call left behind read after each cut.

The coverage gate counts the lines a test reached and every one of these was reached, and load on its own breaks nothing at a named point.
Each round trip is refused twice over: once by a refusal that ends the call, and once by the one this library asks again after.
"""

from contextlib import contextmanager
from datetime import timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from cachefy.clock import now
from cachefy.entry import Entry, minted
from cachefy.store.memory import MemoryStore
from cachefy.store.sqlalchemy import SqlAlchemyStore
from tests.test_contention import refusal
from tests.test_differential import readable

# The most round trips one call is walked through, which is well past the three the longest of them makes.
TRIPS = 32

SPACE = "users"


class Cut(ConnectionError):
    """A connection going away mid call, which is how a driver that went away and a pool with nothing left both arrive."""


def gone():
    """Answers the refusal that ends the call it was made in."""
    return Cut("the connection went away")


def deadlocked():
    """Answers the refusal InnoDB rolls one side of a deadlock back with, which is the one this library asks again after."""
    return refusal(1213)


REFUSALS = (gone, deadlocked)


class Counter:
    """Counts the round trips one call makes, refusing the nth of them and letting every other one through."""

    def __init__(self, nth: int, refused) -> None:
        self.nth = nth
        self.refused = refused
        self.made = 0

    def asked(self) -> None:
        self.made += 1

        if self.made == self.nth:
            raise self.refused()


@contextmanager
def interrupted(store, nth: int, refused):
    """Counts what crosses the process boundary: a statement and a commit for a database, and a command for Redis."""
    counter = Counter(nth, refused)

    if isinstance(store, SqlAlchemyStore):
        execute, commit, scalar = AsyncSession.execute, AsyncSession.commit, AsyncSession.scalar

        async def cut_execute(self, *arguments, **options):
            counter.asked()

            return await execute(self, *arguments, **options)

        async def cut_commit(self, *arguments, **options):
            counter.asked()

            return await commit(self, *arguments, **options)

        async def cut_scalar(self, *arguments, **options):
            counter.asked()

            return await scalar(self, *arguments, **options)

        AsyncSession.execute, AsyncSession.commit, AsyncSession.scalar = cut_execute, cut_commit, cut_scalar

        try:
            yield counter
        finally:
            AsyncSession.execute, AsyncSession.commit, AsyncSession.scalar = execute, commit, scalar

        return

    command = Redis.execute_command

    async def cut_command(self, *arguments, **options):
        counter.asked()

        return await command(self, *arguments, **options)

    Redis.execute_command = cut_command

    try:
        yield counter
    finally:
        Redis.execute_command = command


def crossing(store, refused) -> None:
    """Leaves out the stores a refusal of this shape never reaches."""
    if isinstance(store, MemoryStore):
        pytest.skip("a store that lives in this process crosses nothing, so there is no round trip to cut")

    if refused is deadlocked and not isinstance(store, SqlAlchemyStore):
        pytest.skip("asking again after a deadlock is what a database asks for, and no other store here answers a refusal that way")


async def every_round_trip(store, refused, prepared, called, checked) -> None:
    """Answers one call once for every round trip it makes, cutting each in turn and reading what that cut left behind."""
    for nth in range(1, TRIPS + 1):
        ready = await prepared(nth)

        with interrupted(store, nth, refused) as counter:
            answer = None

            try:
                answer = await called(ready)
            except (Cut, DBAPIError):
                pass

        await checked(nth, ready, answer)

        # The sweep ends where a cut no longer fires, which is one round trip past the whole of a call.
        if counter.made < nth:
            return

    raise AssertionError(f"a call was still making round trips after {TRIPS} of them, so this sweep never reached the end of one")


@pytest.mark.parametrize("refused", REFUSALS)
async def test_a_write_that_was_cut_left_the_old_value_or_the_new_one(store, refused):
    """An entry is a value and every instant it is read by, so a store writing those over more than one round trip could leave half of each."""
    crossing(store, refused)

    moment = now()
    coming = minted()

    async def prepared(nth):
        key = f"k{nth}"
        await store.write(Entry(space=SPACE, key=key, value={"was": nth}, expires_at=moment + timedelta(hours=1), created_at=moment), moment)

        return key, readable(await store.read(SPACE, key, moment))

    async def called(ready):
        key, _ = ready

        return await store.write(Entry(space=SPACE, key=key, value={"now": 2}, stale_at=moment + timedelta(minutes=1), created_at=moment, version=coming), moment)

    async def checked(nth, ready, answer):
        key, before = ready
        left = readable(await store.read(SPACE, key, moment))
        landed = (SPACE, key, {"now": 2}, None, moment + timedelta(minutes=1), moment, coming)

        if answer is not None:
            assert left == landed, f"cutting round trip {nth} answered that the write landed and left the entry it never reached"

            return

        assert left in (before, landed), f"cutting round trip {nth} left an entry that is neither the one it found nor the one it was asked for"

    await every_round_trip(store, refused, prepared, called, checked)


@pytest.mark.parametrize("refused", REFUSALS)
async def test_a_name_taken_by_a_cut_call_is_held_by_that_caller_or_by_nobody(store, refused):
    """A name held by a caller that never came away with it is one nobody ever computes the value behind."""
    crossing(store, refused)

    moment = now()

    async def prepared(nth):
        return f"k{nth}"

    async def called(key):
        return await store.add(Entry(space=SPACE, key=key, value="held", expires_at=moment + timedelta(hours=1), created_at=moment), moment)

    async def checked(nth, key, answer):
        held = await store.read(SPACE, key, moment)

        if answer is not None:
            assert held is not None and held.value == "held", f"cutting round trip {nth} answered that the name was taken and left it holding nothing"

            return

        if held is None:
            assert await store.add(Entry(space=SPACE, key=key, value="again", created_at=moment), moment) is not None, f"cutting round trip {nth} left '{key}' held by nobody and takeable by nobody"

            return

        assert held.value == "held", f"cutting round trip {nth} left '{key}' holding something nobody wrote"

    await every_round_trip(store, refused, prepared, called, checked)


@pytest.mark.parametrize("refused", REFUSALS)
async def test_a_count_that_was_cut_is_the_one_before_it_or_the_one_after(store, refused):
    """A rate limit that counted half a call is one that lets more through than it was asked to."""
    crossing(store, refused)

    moment = now()

    async def prepared(nth):
        key = f"k{nth}"
        await store.bump(SPACE, key, 10, moment + timedelta(hours=1), minted(), moment)

        return key

    async def called(key):
        return await store.bump(SPACE, key, 5, moment + timedelta(hours=1), minted(), moment)

    async def checked(nth, key, answer):
        left = (await store.read(SPACE, key, moment)).value

        if answer is not None:
            assert left == 15 == answer, f"cutting round trip {nth} answered with a count the name does not hold"

            return

        assert left in (10, 15), f"cutting round trip {nth} left a count that is neither the one before it nor the one after"

    await every_round_trip(store, refused, prepared, called, checked)


@pytest.mark.parametrize("refused", REFUSALS)
async def test_a_value_written_back_by_a_cut_call_landed_whole_or_not_at_all(store, refused):
    crossing(store, refused)

    moment = now()

    async def prepared(nth):
        key = f"k{nth}"
        written = await store.write(Entry(space=SPACE, key=key, value="was", created_at=moment), moment)

        return key, written.version

    async def called(ready):
        key, version = ready

        return await store.swap(Entry(space=SPACE, key=key, value="now", created_at=moment), version, moment)

    async def checked(nth, ready, answer):
        key, was = ready
        left = await store.read(SPACE, key, moment)

        if answer is not None:
            assert (left.value, left.version) == ("now", answer.version), f"cutting round trip {nth} answered that the value landed and left the one before it"

            return

        assert (left.value, left.version) in (("was", was), ("now", left.version)), f"cutting round trip {nth} left an entry nobody wrote"

    await every_round_trip(store, refused, prepared, called, checked)


@pytest.mark.parametrize("refused", REFUSALS)
async def test_a_sweep_that_was_cut_dropped_exactly_what_it_answered_for(store, refused):
    """A count is what an operator reads, so a sweep that dropped rows and then stopped is a number nothing can be told from."""
    crossing(store, refused)

    moment = now()

    async def prepared(nth):
        for index in range(3):
            await store.write(Entry(space=f"s{nth}", key=f"k{index}", value=index, expires_at=moment - timedelta(minutes=1), created_at=moment), moment)

        return nth

    async def called(nth):
        return await store.purge(moment, 100)

    async def checked(nth, ready, answer):
        # What the cut sweep dropped and what a whole one still finds have to be the three that were written, and nothing else.
        left = await store.purge(moment, 100)

        assert (answer or 0) + left == 3, f"cutting round trip {nth} answered for {answer or 0} while {left} were still lying there"

    await every_round_trip(store, refused, prepared, called, checked)


@pytest.mark.parametrize("refused", REFUSALS)
async def test_a_space_cleared_by_a_cut_call_dropped_exactly_what_it_answered_for(store, refused):
    crossing(store, refused)

    moment = now()

    async def prepared(nth):
        space = f"s{nth}"

        for index in range(3):
            await store.write(Entry(space=space, key=f"k{index}", value=index, created_at=moment), moment)

        return space

    async def called(space):
        return await store.clear(space)

    async def checked(nth, space, answer):
        left = await store.count(space, moment)

        assert 3 - left == (answer or 0), f"cutting round trip {nth} dropped {3 - left} entries while answering for {answer or 0}"

    await every_round_trip(store, refused, prepared, called, checked)


@pytest.mark.parametrize("refused", REFUSALS)
async def test_a_name_taken_away_by_a_cut_call_is_gone_or_is_all_still_there(store, refused):
    crossing(store, refused)

    moment = now()

    async def prepared(nth):
        key = f"k{nth}"
        await store.write(Entry(space=SPACE, key=key, value={"was": nth}, expires_at=moment + timedelta(hours=1), created_at=moment), moment)

        return key, readable(await store.read(SPACE, key, moment))

    async def called(ready):
        key, _ = ready

        return await store.drop(SPACE, key)

    async def checked(nth, ready, answer):
        key, before = ready
        left = readable(await store.read(SPACE, key, moment))

        if answer:
            assert left is None, f"cutting round trip {nth} answered that the name was taken away and left it holding something"

            return

        assert left in (before, None), f"cutting round trip {nth} left an entry that is neither the one it found nor gone"

    await every_round_trip(store, refused, prepared, called, checked)


@pytest.mark.parametrize("refused", REFUSALS)
async def test_the_instants_a_cut_call_moved_are_the_old_ones_or_the_new_ones(store, refused):
    """An entry is read by two instants, so a store writing them over more than one round trip could leave one of each."""
    crossing(store, refused)

    moment = now()
    later, fresher = moment + timedelta(hours=2), moment + timedelta(hours=1)

    async def prepared(nth):
        key = f"k{nth}"
        await store.write(Entry(space=SPACE, key=key, value=nth, expires_at=moment + timedelta(minutes=5), created_at=moment), moment)

        return key, readable(await store.read(SPACE, key, moment))

    async def called(ready):
        key, _ = ready

        return await store.touch(SPACE, key, later, fresher, minted(), moment)

    async def checked(nth, ready, answer):
        key, before = ready
        left = await store.read(SPACE, key, moment)

        if answer:
            assert (left.expires_at, left.stale_at) == (later, fresher), f"cutting round trip {nth} answered that the instants moved and left the ones before them"

            return

        assert (left.expires_at, left.stale_at) in ((before[3], before[4]), (later, fresher)), f"cutting round trip {nth} left one instant of each"

    await every_round_trip(store, refused, prepared, called, checked)

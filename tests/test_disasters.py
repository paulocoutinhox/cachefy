"""Clocks that disagree, processes that die mid computation, and values nothing was meant to hold."""

import asyncio
from datetime import timedelta

import pytest

from cachefy.clock import now
from cachefy.entry import MISS, Entry
from cachefy.errors import UnwritableValue

BRIEF = timedelta(milliseconds=30)


async def test_a_value_written_by_a_clock_running_ahead_is_still_read_by_this_one(store):
    """Every instant is decided in one place, so a machine an hour ahead writes an entry this one can still read."""
    ahead = now() + timedelta(hours=1)
    await store.write(Entry(space="users", key="42", value=1, expires_at=ahead + timedelta(hours=1), created_at=ahead), ahead)

    assert (await store.read("users", "42", now())).value == 1


async def test_a_value_a_clock_running_behind_already_buried_is_gone_for_everybody(store):
    behind = now() - timedelta(hours=1)
    await store.write(Entry(space="users", key="42", value=1, expires_at=behind, created_at=behind), behind)

    assert await store.read("users", "42", now()) is None


async def test_a_caller_that_died_mid_computation_costs_one_lease_and_never_a_hang(app):
    """A process killed while it holds a name must never leave every caller after it waiting for ever."""
    users = app.space("users", lease=BRIEF)

    assert await users.take("42", now()) is not None, "the process that is about to die holds the name"

    async with asyncio.timeout(10):
        assert await users.fetch("42", lambda: "computed") == "computed"


async def test_a_caller_cancelled_mid_computation_lets_the_name_go(app):
    users = app.space("users")
    holding = asyncio.Event()

    async def never_answers():
        await holding.wait()

        return "never"

    computing = asyncio.create_task(users.fetch("42", never_answers))

    await asyncio.sleep(0.05)
    computing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await computing

    assert await users.fetch("42", lambda: "computed") == "computed"


async def test_a_producer_that_asks_the_process_to_stop_is_the_callers_own_code(app):
    """A library calling `sys.exit` inside a producer is a failure the caller has to see, and never one a cache swallows."""
    users = app.space("users")

    def unhelpful():
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        await users.fetch("42", unhelpful)

    assert await users.get("42") is MISS
    assert await users.fetch("42", lambda: "computed") == "computed", "and the name it held was let go of"


async def test_a_value_a_producer_answered_that_no_store_holds_is_never_the_end_of_the_request(app):
    users = app.space("users")
    told = []

    app.on_error(lambda what, failure: told.append(type(failure).__name__))

    handed = {"when": object()}

    assert await users.fetch("42", lambda: handed) is handed
    assert told == ["UnwritableValue"]
    assert await users.get("42") is MISS


async def test_a_value_a_caller_wrote_that_no_store_holds_is_told_at_once(app):
    """An explicit write is the one place a refusal belongs, because the caller asked for it to be kept."""
    users = app.space("users")

    with pytest.raises(UnwritableValue):
        await users.set("42", {"when": object()})


async def test_a_name_swept_while_a_caller_was_reading_it_is_a_miss_and_never_a_failure(store):
    moment = now()
    await store.write(Entry(space="users", key="42", value=1, expires_at=moment - timedelta(minutes=1), created_at=moment), moment)

    assert await store.purge(moment, 100) == 1
    assert await store.read("users", "42", moment) is None
    assert await store.drop("users", "42") is False


async def test_two_callers_counting_the_same_name_never_lose_a_count(app):
    """A rate limit two callers each read as one is a rate limit that lets twice as much through."""
    limits = app.space("limits")
    counts = await asyncio.gather(*[limits.incr("paulo") for _ in range(25)])

    assert sorted(counts) == list(range(1, 26))


async def test_a_space_cleared_while_a_caller_was_computing_still_keeps_what_it_computed(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    holding = asyncio.Event()

    async def slowly():
        await holding.wait()

        return "computed"

    computing = asyncio.create_task(users.fetch("42", slowly))

    await asyncio.sleep(0.05)
    await users.clear()
    holding.set()

    assert await computing == "computed"
    assert await users.get("42") == "computed"


async def test_a_sweep_by_a_clock_running_ahead_takes_what_is_still_alive_by_this_one(store):
    """A sweep drops what died before the moment it was given, so a janitor an hour fast decides an hour of living entries are dead."""
    moment = now()
    await store.write(Entry(space="users", key="42", value=1, expires_at=moment + timedelta(minutes=5), created_at=moment), moment)

    assert await store.purge(moment + timedelta(hours=1), 100) == 1
    assert await store.read("users", "42", moment) is None, "what a fast janitor costs is the entry, and never a wrong value in its place"


async def test_a_sweep_by_a_clock_running_behind_leaves_what_this_one_has_already_buried(store):
    """The field is what says an entry is dead and the moment is what it is read against, so a slow janitor only sweeps late."""
    moment = now()
    await store.write(Entry(space="users", key="42", value=1, expires_at=moment - timedelta(minutes=5), created_at=moment - timedelta(hours=2)), moment)

    assert await store.purge(moment - timedelta(hours=1), 100) == 0
    assert await store.read("users", "42", moment) is None, "it is gone to every reader whatever the janitor thinks"

    assert await store.purge(moment, 100) == 1

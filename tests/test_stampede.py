"""One caller computes and the rest are served, which is what stops a cold name becoming a stampede."""

import asyncio
from datetime import timedelta

import pytest

from cachefy.clock import now
from cachefy.entry import MISS
from tests.conftest import wait_until

CALLERS = 12

BRIEF = timedelta(milliseconds=30)
WHILE = 0.09


class Counted:
    """A producer that says how many times it was actually called."""

    def __init__(self, value, waiting: float = 0.0) -> None:
        self.value = value
        self.waiting = waiting
        self.calls = 0

    async def __call__(self):
        self.calls += 1

        if self.waiting:
            await asyncio.sleep(self.waiting)

        return self.value


async def test_a_name_that_holds_nothing_is_computed_and_kept(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    load = Counted({"name": "paulo"})

    assert await users.fetch("42", load) == {"name": "paulo"}
    assert await users.fetch("42", load) == {"name": "paulo"}
    assert load.calls == 1


async def test_a_producer_that_answers_none_is_kept_and_never_computed_twice(app):
    users = app.space("users")
    load = Counted(None)

    assert await users.fetch("42", load) is None
    assert await users.fetch("42", load) is None
    assert load.calls == 1


async def test_many_callers_at_once_compute_it_once_between_them(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    holding, calls = asyncio.Event(), []

    async def load():
        calls.append(1)
        await holding.wait()

        return "computed"

    asked = asyncio.gather(*[users.fetch("42", load) for _ in range(CALLERS)])

    await wait_until(lambda: len(calls) == 1)
    holding.set()

    assert await asked == ["computed"] * CALLERS
    assert calls == [1], "one caller computes and the rest are handed what it wrote"


async def test_a_stale_value_is_served_while_one_caller_refreshes_it(app):
    users = app.space("users", ttl=timedelta(minutes=5), stale=BRIEF)
    first = Counted("old")
    await users.fetch("42", first)

    await asyncio.sleep(WHILE)

    holding, started = asyncio.Event(), asyncio.Event()

    async def slowly():
        started.set()
        await holding.wait()

        return "new"

    refreshing = asyncio.create_task(users.fetch("42", slowly))

    await started.wait()

    third = Counted("never")

    assert await users.fetch("42", third) == "old", "everybody else is served what is there"
    assert third.calls == 0

    holding.set()

    assert await refreshing == "new"
    assert await users.get("42") == "new", "and what that caller computed is what is kept"


async def test_the_name_is_let_go_as_soon_as_the_value_is_written(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    load = Counted("computed")
    await users.fetch("42", load)
    await users.drop("42")

    again = Counted("again")

    assert await users.fetch("42", again) == "again", "a caller after the first never waits a lease out"
    assert again.calls == 1


async def test_a_producer_that_raises_is_raised_at_whoever_asked(app):
    users = app.space("users")

    async def broken():
        raise RuntimeError("the database is gone")

    try:
        await users.fetch("42", broken)
        raise AssertionError("the producer is the caller's own code, so what it raises reaches the caller")
    except RuntimeError as broke:
        assert str(broke) == "the database is gone"

    assert await users.get("42") is MISS


async def test_the_name_is_let_go_when_the_producer_raised(app):
    users = app.space("users")

    async def broken():
        raise RuntimeError("boom")

    for _ in range(3):
        try:
            await users.fetch("42", broken)
        except RuntimeError:
            pass

    load = Counted("computed")

    assert await users.fetch("42", load) == "computed", "a producer that raised never leaves the name held"


async def test_a_caller_waiting_computes_it_itself_when_the_holder_never_writes(app):
    """A process that died mid computation costs one lease and never a request that hangs."""
    users = app.space("users", lease=BRIEF)
    holder = await users.take("42", now())

    assert holder is not None

    load = Counted("computed")

    assert await users.fetch("42", load) == "computed"
    assert load.calls == 1


async def test_a_plain_producer_is_called_where_the_caller_is(app):
    users = app.space("users")
    calls = []

    def load():
        calls.append(1)

        return "computed"

    assert await users.fetch("42", load) == "computed"
    assert calls == [1]


async def test_an_answer_no_store_can_hold_is_handed_back_uncached(app):
    """A cache must never be the reason a request fails."""
    users = app.space("users")
    seen = []

    app.on_error(lambda what, failure: seen.append(what))

    handed = object()

    assert await users.fetch("42", lambda: handed) is handed
    assert await users.get("42") is MISS
    assert seen == ["keep 'users:42'"]


async def test_a_refresh_that_broke_is_served_from_what_is_already_there(app):
    """A value that is only stale is still a value, so a producer that failed must not fail the request behind it."""
    users = app.space("users", ttl=timedelta(minutes=5), stale=BRIEF)
    told = []

    app.on_error(lambda what, failure: told.append(what))

    await users.fetch("42", Counted("old"))

    await asyncio.sleep(WHILE)

    async def broken():
        raise RuntimeError("the database is gone")

    assert await users.fetch("42", broken) == "old"
    assert told == ["refresh 'users:42'"]


async def test_a_producer_that_broke_with_nothing_to_serve_is_still_raised(app):
    users = app.space("users", ttl=timedelta(minutes=5), stale=BRIEF)

    async def broken():
        raise RuntimeError("the database is gone")

    with pytest.raises(RuntimeError):
        await users.fetch("42", broken)


async def test_two_spaces_caching_one_key_never_wait_on_each_other(app):
    """The name a caller holds is drawn from the space and the key together, or one space would hold the other's up."""
    users = app.space("users", lease=timedelta(hours=1))
    posts = app.space("posts", lease=timedelta(hours=1))
    holding = asyncio.Event()

    async def slowly():
        await holding.wait()

        return "a user"

    computing = asyncio.create_task(users.fetch("42", slowly))

    await asyncio.sleep(0.05)

    async with asyncio.timeout(5):
        assert await posts.fetch("42", lambda: "a post") == "a post"

    holding.set()

    assert await computing == "a user"


async def test_a_stale_value_is_served_from_the_read_that_found_it_and_never_waited_for(app):
    """Everybody else is served what is there while one caller refreshes, so nobody asks the store a second time."""
    users = app.space("users", ttl=timedelta(minutes=5), stale=BRIEF)
    await users.fetch("42", Counted("old"))

    await asyncio.sleep(WHILE)

    started, holding = asyncio.Event(), asyncio.Event()

    async def slowly():
        started.set()
        await holding.wait()

        return "new"

    refreshing = asyncio.create_task(users.fetch("42", slowly))

    await started.wait()

    reads = []
    reading = app.store.read

    async def counted(*arguments, **options):
        reads.append(1)

        return await reading(*arguments, **options)

    app.store.read = counted

    try:
        assert await users.fetch("42", Counted("never")) == "old"
    finally:
        app.store.read = reading

    assert len(reads) == 1, "what is there is answered from the one read that found it"

    holding.set()

    assert await refreshing == "new"


async def test_a_value_that_is_still_fresh_is_answered_without_computing_it_again(app):
    """A freshness is what tells a caller to recompute, so a value that has not reached one must never be recomputed."""
    users = app.space("users", ttl=timedelta(minutes=5), stale=timedelta(minutes=1))
    load = Counted("computed")

    for _ in range(3):
        assert await users.fetch("42", load) == "computed"

    assert load.calls == 1, "nothing had gone stale, so nothing was computed twice"

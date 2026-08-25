"""What a cancelled caller leaves behind, which is the one thing holding a name makes easy to get wrong."""

import asyncio
import threading
from contextlib import suppress
from datetime import timedelta

import pytest

from cachefy.app import Cachefy
from cachefy.clock import now
from cachefy.entry import MISS, Entry
from cachefy.janitor import Janitor
from cachefy.keys import digested, joined
from cachefy.space import LOCKS
from cachefy.store.memory import MemoryStore
from tests.conftest import wait_until

# The most round trips one call is walked through, which is well past what any of these really make.
TRIPS = 12


class Cancelling(MemoryStore):
    """Cancels whoever is calling at the nth round trip it is asked for."""

    def __init__(self, nth: int) -> None:
        super().__init__()

        self.nth = nth
        self.made = 0
        self.caller = None

    def asked(self) -> None:
        self.made += 1

        if self.made == self.nth and self.caller is not None:
            self.caller.cancel()

    async def read(self, *arguments, **options):
        self.asked()

        return await super().read(*arguments, **options)

    async def read_many(self, *arguments, **options):
        self.asked()

        return await super().read_many(*arguments, **options)

    async def write(self, *arguments, **options):
        self.asked()

        return await super().write(*arguments, **options)

    async def add(self, *arguments, **options):
        self.asked()

        return await super().add(*arguments, **options)

    async def swap(self, *arguments, **options):
        self.asked()

        return await super().swap(*arguments, **options)


def holding(store: MemoryStore) -> list:
    """Answers the names a caller is still holding while it computes."""
    moment = now()

    return [name for name, entry in store.entries.items() if name[0] == LOCKS and entry.alive(moment)]


async def cut_at(nth: int, work) -> Cancelling:
    """Runs the work with the store cancelling it at its nth round trip, and answers the store it left behind."""
    store = Cancelling(nth)
    await store.setup()

    running = asyncio.create_task(work(Cachefy(store)))
    store.caller = running

    with suppress(asyncio.CancelledError):
        await running

    return store


@pytest.mark.parametrize("nth", range(1, TRIPS + 1))
async def test_a_cancelled_fetch_leaves_no_name_held(nth):
    """A name left held is every caller after it waiting out a lease for a value nobody is computing."""

    async def work(app):
        async def load():
            await asyncio.sleep(0)

            return "computed"

        return await app.space("users", lease=timedelta(hours=1)).fetch("42", load)

    store = await cut_at(nth, work)

    assert holding(store) == [], f"cancelling round trip {nth} left a name held for a whole lease"

    # The caller after it is what proves the name is free, because that is what a held one would refuse.
    users = Cachefy(store).space("users", lease=timedelta(hours=1))

    async with asyncio.timeout(5):
        assert await users.fetch("42", lambda: "after") in ("computed", "after")


@pytest.mark.parametrize("nth", range(1, TRIPS + 1))
async def test_a_cancelled_read_of_many_names_leaves_nothing_behind(nth):
    async def work(app):
        return await app.space("users").get_many([f"k{index}" for index in range(2500)])

    store = await cut_at(nth, work)

    assert holding(store) == []


@pytest.mark.parametrize("nth", range(1, TRIPS + 1))
async def test_a_cancelled_caller_that_was_waiting_leaves_no_name_held(nth):
    """A caller that lost the name and was asking again is the one furthest from where it started."""

    async def work(app):
        users = app.space("users", lease=timedelta(milliseconds=60))
        await users.take("42", now())

        return await users.fetch("42", lambda: "mine")

    store = await cut_at(nth, work)
    left = holding(store)

    assert left == [] or len(left) == 1, "only the name the test itself took may be left, and never one this caller held"


async def test_a_cancelled_janitor_stops_where_it_was(app):
    moment = now()

    for index in range(30):
        await app.store.write(Entry(space="s", key=f"g{index}", value=index, expires_at=moment - timedelta(seconds=1), created_at=moment), moment)

    janitor = Janitor(app, every=timedelta(hours=1), batch=5)
    sweeping = asyncio.create_task(janitor.run())

    await asyncio.sleep(0.02)
    sweeping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await sweeping

    assert not janitor.stopping.is_set(), "a cancel is not the same thing as being asked to stop"


async def test_a_listener_being_cancelled_never_has_that_swallowed(app):
    users = app.space("users")
    told = []

    @app.on_miss
    def unhelpful(space, key):
        told.append(1)

        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await users.get("42")

    assert told == [1]


async def test_a_fetch_a_deadline_gave_up_on_leaves_no_name_held(app):
    """A caller wrapped in a deadline is cancelled exactly as one whose task was, and must leave as little behind."""
    users = app.space("users", lease=timedelta(hours=1))
    holding_it = asyncio.Event()

    async def never_answers():
        await holding_it.wait()

        return "never"

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await users.fetch("42", never_answers)

    # The release is shielded, so a caller a deadline gave up on leaves it finishing behind them rather than never.
    # What this asks is that the name is let go, and never how many seconds the slowest store takes to let go of it.
    async def let_go() -> bool:
        return await app.store.read(LOCKS, digested(joined("users", "42")), now()) is None

    await wait_until(let_go, patience=30.0)

    assert await users.fetch("42", lambda: "after") == "after"


def test_the_cache_answers_a_synchronous_caller_through_one_loop_of_its_own():
    """The bridge the Flask page shows, which is a loop on a thread and every web thread handing work to it."""
    loop = asyncio.new_event_loop()
    running = threading.Thread(target=loop.run_forever, daemon=True)
    running.start()

    def waited(work):
        return asyncio.run_coroutine_threadsafe(work, loop).result(timeout=30)

    app = Cachefy(MemoryStore())
    users = app.space("users", ttl=timedelta(minutes=5))

    try:
        waited(app.setup())
        waited(users.set("42", {"name": "Paulo"}))

        assert waited(users.get("42")) == {"name": "Paulo"}
        assert waited(users.fetch("43", lambda: "computed")) == "computed"
        assert waited(users.get("nothing")) is MISS

        answers = []
        threads = [threading.Thread(target=lambda: answers.append(waited(users.get("42")))) for _ in range(12)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(timeout=30)

        assert answers == [{"name": "Paulo"}] * 12, "every web thread was answered by the one loop"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        running.join(timeout=30)
        loop.close()


async def test_a_caller_cancelled_a_second_time_still_lets_the_name_go(app):
    """A shutdown that cancels, waits and cancels harder lands the second one inside the release itself."""
    users = app.space("users", lease=timedelta(hours=1))
    started, holding, releasing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def never_answers():
        started.set()
        await holding.wait()

        return "never"

    swapping = app.store.swap

    async def slowly(*arguments, **options):
        releasing.set()
        await asyncio.sleep(0.1)

        return await swapping(*arguments, **options)

    app.store.swap = slowly

    try:
        running = asyncio.create_task(users.fetch("42", never_answers))

        await started.wait()
        running.cancel()

        await releasing.wait()
        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running

        async def let_go() -> bool:
            return await app.store.read(LOCKS, digested(joined("users", "42")), now()) is None

        await wait_until(let_go, patience=10.0)
    finally:
        app.store.swap = swapping

    async with asyncio.timeout(10):
        assert await users.fetch("42", lambda: "after") == "after"

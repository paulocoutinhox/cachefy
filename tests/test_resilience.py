"""A store that cannot be reached is a miss and never an exception.

A cache is what makes an application faster and never what makes it work, so nothing a store does may reach whoever asked.
"""

import asyncio
from datetime import timedelta

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from cachefy.app import Cachefy
from cachefy.entry import MISS
from cachefy.janitor import Janitor
from cachefy.store.base import Store
from cachefy.store.memory import MemoryStore
from cachefy.store.redis import RedisStore
from cachefy.store.sqlalchemy import SqlAlchemyStore
from cachefy.store.sqlalchemy import metadata as server_metadata
from tests.conftest import REDIS_URL, SERVERS, STORES, wait_until

# Ports nothing is listening on, which is what a store that is simply not there looks like.
NOWHERE_REDIS = "redis://127.0.0.1:6399/0"
NOWHERE_POSTGRES = "postgresql+asyncpg://nobody:nobody@127.0.0.1:5399/nothing"


class Gone(ConnectionError):
    """What a connection that went away raises."""


class Broken(Store):
    """A store that refuses every call, which is what every store looks like from far enough away."""

    async def setup(self) -> None:
        raise Gone("the store is not there")

    async def read(self, space, key, moment):
        raise Gone("the store is not there")

    async def read_many(self, space, keys, moment):
        raise Gone("the store is not there")

    async def write(self, entry, moment):
        raise Gone("the store is not there")

    async def add(self, entry, moment):
        raise Gone("the store is not there")

    async def swap(self, entry, version, moment):
        raise Gone("the store is not there")

    async def drop(self, space, key):
        raise Gone("the store is not there")

    async def touch(self, space, key, expires_at, stale_at, version, moment):
        raise Gone("the store is not there")

    async def bump(self, space, key, amount, expires_at, version, moment):
        raise Gone("the store is not there")

    async def clear(self, space):
        raise Gone("the store is not there")

    async def purge(self, before, limit):
        raise Gone("the store is not there")

    async def count(self, space, moment):
        raise Gone("the store is not there")


@pytest.fixture
def broken():
    return Cachefy(Broken())


async def test_every_call_of_a_space_answers_rather_than_raises(broken):
    users = broken.space("users", ttl=timedelta(minutes=5))

    assert await users.get("42") is MISS
    assert await users.get("42", default="nothing") == "nothing"
    assert await users.entry("42") is None
    assert await users.get_many(["a", "b"]) == {}
    assert await users.set("42", 1) is None
    assert await users.add("42", 1) is None
    assert await users.swap("42", 1, 1) is None
    assert await users.drop("42") is False
    assert await users.touch("42") is False
    assert await users.incr("42") is None
    assert await users.clear() == 0
    assert await users.count() == 0


async def test_a_value_is_still_computed_and_handed_back_with_no_store_at_all(broken):
    users = broken.space("users")
    calls = []

    async def load():
        calls.append(1)

        return "computed"

    assert await users.fetch("42", load) == "computed"
    assert calls == [1], "a caller never waits on a name a store cannot tell it about"


async def test_a_broken_store_never_makes_a_caller_wait_a_lease_out(broken):
    users = broken.space("users", lease=timedelta(hours=1))

    async with asyncio.timeout(5):
        assert await users.fetch("42", lambda: "computed") == "computed"


async def test_a_memoized_call_is_answered_with_no_store_at_all(broken):
    calls = []

    @broken.cached("profile")
    async def profile(user_id: int) -> dict:
        calls.append(user_id)

        return {"id": user_id}

    assert await profile(7) == {"id": 7}
    assert await profile(7) == {"id": 7}
    assert calls == [7, 7], "nothing was kept, so everything is computed, and nothing ever fails"
    assert await profile.invalidate(7) is False
    assert await profile.clear() == 0


async def test_every_failure_is_told_to_whoever_is_listening(broken):
    seen = []
    broken.on_error(lambda what, failure: seen.append((what, type(failure).__name__)))

    users = broken.space("users")
    await users.get("42")
    await users.set("42", 1)

    assert seen == [("read 'users:42'", "Gone"), ("write 'users:42'", "Gone")]


async def test_a_setup_that_could_not_reach_the_store_is_the_one_thing_that_is_raised(broken):
    """Building the store is a start-up step and not a request, so a process that cannot do it must never come up pretending it did."""
    with pytest.raises(Gone):
        await broken.setup()


async def test_a_janitor_goes_on_sweeping_with_no_store_at_all(broken):
    janitor = Janitor(broken, every=timedelta(milliseconds=10))
    sweeping = asyncio.create_task(janitor.run())
    swept = []

    broken.on_error(lambda what, failure: swept.append(what))

    await wait_until(lambda: len(swept) >= 3)

    janitor.stop()
    await sweeping

    assert set(swept) == {"sweep what has died"}


async def test_a_listener_that_breaks_breaks_alone(broken):
    users = broken.space("users")

    @broken.on_error
    def unhelpful(what, failure):
        raise RuntimeError("the metric is down too")

    assert await users.get("42") is MISS


async def test_a_store_that_comes_back_is_read_again_without_anything_being_restarted():
    """Nothing here holds a broken flag, so the pass after a store answers again is an ordinary one."""
    store = MemoryStore()
    await store.setup()
    app = Cachefy(store)
    users = app.space("users")

    await users.set("42", 1)

    reading = store.read

    async def refuse(*arguments, **options):
        raise Gone("the store went away")

    store.read = refuse

    assert await users.get("42") is MISS

    store.read = reading

    assert await users.get("42") == 1


async def test_a_redis_nobody_is_listening_on_is_a_miss_and_never_a_failure():
    client = Redis.from_url(NOWHERE_REDIS, socket_connect_timeout=1)
    store = RedisStore(client)
    await store.setup()
    app = Cachefy(store)
    users = app.space("users")

    assert await users.get("42") is MISS
    assert await users.set("42", 1) is None
    assert await users.fetch("42", lambda: "computed") == "computed"

    await client.aclose()


async def test_a_database_nobody_is_listening_on_is_a_miss_and_never_a_failure():
    engine = create_async_engine(NOWHERE_POSTGRES, connect_args={"timeout": 1})
    app = Cachefy(SqlAlchemyStore(engine))
    users = app.space("users")

    assert await users.get("42") is MISS
    assert await users.set("42", 1) is None
    assert await users.fetch("42", lambda: "computed") == "computed"

    await engine.dispose()


async def test_a_connection_lost_part_way_through_is_a_miss_and_never_a_failure(app):
    """A store that answered a moment ago and then went away is the ordinary shape of an outage."""
    users = app.space("users")
    await users.set("42", 1)

    reading = app.store.read

    async def half_way(*arguments, **options):
        await asyncio.sleep(0)

        raise Gone("the connection went away mid call")

    app.store.read = half_way

    assert await users.get("42") is MISS
    assert await users.fetch("42", lambda: "computed") == "computed"

    app.store.read = reading

    assert await users.get("42") == "computed"


@pytest.mark.skipif("postgres" not in STORES, reason="a postgres nobody can reach is not a store this suite collects")
async def test_a_pool_with_no_connection_left_degrades_rather_than_raising():
    """A cache is called on every request, so its pool running out is likelier than its network going away."""
    engine = create_async_engine(SERVERS["postgres"], pool_size=1, max_overflow=0, pool_timeout=1)

    async with engine.begin() as connection:
        await connection.run_sync(server_metadata.drop_all)

    app = Cachefy(SqlAlchemyStore(engine))
    await app.setup()
    users = app.space("users")
    told = []
    app.on_error(lambda what, failure: told.append(type(failure).__name__))

    await users.set("42", 1)

    held = await engine.connect()

    try:
        assert await users.get("42") is MISS
        assert await users.fetch("42", lambda: "computed") == "computed"
        assert told == ["read 'users:42'", "read 'users:42'"] or "TimeoutError" in told
    finally:
        await held.close()
        await engine.dispose()


@pytest.mark.skipif("redis" not in STORES, reason="a redis nobody can reach is not a store this suite collects")
async def test_somebody_else_at_one_of_our_key_names_is_a_miss_and_never_a_failure():
    """A prefix shared with another application is a value of theirs where a hash of ours belongs, and redis refuses every command against it."""
    client = Redis.from_url(REDIS_URL)
    await client.flushdb()
    store = RedisStore(client)
    await store.setup()

    app = Cachefy(store)
    users = app.space("users")
    told = []
    app.on_error(lambda what, failure: told.append(what))

    await client.set("cachefy:entry:users:42", "a value of somebody else's")
    await client.lpush("cachefy:names:users", "a list where a set belongs")

    assert await users.get("42") is MISS
    assert await users.set("42", 1) is None
    assert await users.clear() == 0
    assert await users.count() == 0
    assert told == ["read 'users:42'", "write 'users:42'", "clear 'users'", "count 'users'"]

    await client.aclose()

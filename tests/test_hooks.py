"""Being told about every hit, every miss and everything a store could not answer."""

import asyncio
from datetime import timedelta

from cachefy.entry import MISS


async def test_a_hit_and_a_miss_are_each_told_apart(app):
    users = app.space("users")
    hits, misses = [], []

    app.on_hit(lambda space, key: hits.append((space, key)))
    app.on_miss(lambda space, key: misses.append((space, key)))

    await users.get("42")
    await users.set("42", 1)
    await users.get("42")

    assert hits == [("users", "42")]
    assert misses == [("users", "42")]


async def test_a_name_holding_none_is_a_hit_and_never_a_miss(app):
    users = app.space("users")
    hits, misses = [], []

    app.on_hit(lambda space, key: hits.append(key))
    app.on_miss(lambda space, key: misses.append(key))

    await users.set("42", None)
    await users.get("42")

    assert (hits, misses) == (["42"], [])


async def test_reading_many_names_tells_the_listeners_about_each_of_them(app):
    users = app.space("users")
    hits, misses = [], []

    app.on_hit(lambda space, key: hits.append(key))
    app.on_miss(lambda space, key: misses.append(key))

    await users.set("a", 1)
    await users.get_many(["a", "b"])

    assert (hits, misses) == (["a"], ["b"])


async def test_computing_a_value_is_a_miss_and_reading_it_again_is_a_hit(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    told = []

    app.on_hit(lambda space, key: told.append("hit"))
    app.on_miss(lambda space, key: told.append("miss"))

    await users.fetch("42", lambda: 1)
    await users.fetch("42", lambda: 1)

    assert told == ["miss", "hit"]


async def test_a_listener_may_be_a_coroutine(app):
    users = app.space("users")
    seen = []

    @app.on_miss
    async def remember(space, key):
        await asyncio.sleep(0)
        seen.append(key)

    await users.get("42")

    assert seen == ["42"]


async def test_a_listener_that_breaks_never_takes_the_value_with_it(app):
    users = app.space("users")
    await users.set("42", 1)

    @app.on_hit
    def unhelpful(space, key):
        raise RuntimeError("the metric is down")

    assert await users.get("42") == 1


async def test_a_listener_that_exits_the_process_breaks_alone(app):
    """A library calling `sys.exit` deep inside a listener must never end the request that was only reading a cache."""
    users = app.space("users")

    @app.on_miss
    def unhelpful(space, key):
        raise SystemExit(1)

    assert await users.get("42") is MISS


async def test_every_listener_is_told_even_when_one_before_it_broke(app):
    users = app.space("users")
    seen = []

    app.on_miss(lambda space, key: 1 / 0)
    app.on_miss(lambda space, key: seen.append(key))

    await users.get("42")

    assert seen == ["42"]

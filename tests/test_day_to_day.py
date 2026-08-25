"""The things an application actually asks a cache for, answered end to end."""

import asyncio
from datetime import timedelta

from cachefy.entry import MISS
from tests.conftest import wait_until

WINDOW = timedelta(milliseconds=40)
WHILE = 0.11


async def test_a_request_reads_what_the_request_before_it_computed(app):
    """The whole point of the thing: the second caller never touches the database."""
    users = app.space("users", ttl=timedelta(minutes=5))
    reads = []

    async def load(user_id: str) -> dict:
        reads.append(user_id)

        return {"id": user_id, "name": "Paulo"}

    for _ in range(5):
        assert await users.fetch("42", lambda: load("42")) == {"id": "42", "name": "Paulo"}

    assert reads == ["42"]


async def test_writing_a_record_is_what_invalidates_the_value_behind_it(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    record = {"id": "42", "name": "Paulo"}

    async def load() -> dict:
        return dict(record)

    assert await users.fetch("42", load) == {"id": "42", "name": "Paulo"}

    record["name"] = "Coutinho"
    await users.drop("42")

    assert await users.fetch("42", load) == {"id": "42", "name": "Coutinho"}


async def test_a_page_of_records_is_read_in_one_round_trip(app):
    users = app.space("users", ttl=timedelta(minutes=5))

    for index in range(10):
        await users.set(str(index), {"id": index})

    found = await users.get_many([str(index) for index in range(15)])

    assert len(found) == 10
    assert found["7"] == {"id": 7}


async def test_a_rate_limit_lets_a_burst_through_and_then_stops_it(app):
    limits = app.space("limits", ttl=timedelta(minutes=1))
    address = "203.0.113.7"
    allowed = []

    for _ in range(6):
        allowed.append(await limits.incr(address) <= 3)

    assert allowed == [True, True, True, False, False, False]

    # The window is brought forward rather than waited out, so what this asks about is the window and never how fast the machine is.
    await limits.touch(address, ttl=WINDOW)
    await asyncio.sleep(WHILE)

    assert await limits.incr(address) == 1, "the window starts again once it has run out"


async def test_a_whole_family_of_values_is_invalidated_at_once(app):
    posts = app.space("posts", ttl=timedelta(minutes=5))

    for index in range(5):
        await posts.set(f"post-{index}", {"id": index})

    assert await posts.count() == 5
    assert await posts.clear() == 5
    assert await posts.get("post-0") is MISS


async def test_a_value_two_writers_race_for_is_written_once_and_read_by_both(app):
    users = app.space("users")
    answers = await asyncio.gather(users.add("42", "first"), users.add("42", "second"))

    assert sorted(answer is None for answer in answers) == [False, True], "one of them took the name"
    assert await users.get("42") in ("first", "second")


async def test_a_slow_writer_never_writes_over_what_somebody_wrote_after_it(app):
    """Reading, working something out and writing it back is three steps with the world between them."""
    users = app.space("users")
    read = await users.set("42", {"visits": 1})

    await users.set("42", {"visits": 99})

    assert await users.swap("42", {"visits": 2}, read.version) is None
    assert await users.get("42") == {"visits": 99}


async def test_a_dashboard_reads_the_depth_of_every_space_it_watches(app):
    users = app.space("users")
    posts = app.space("posts")

    await users.set("a", 1)
    await posts.set("b", 2)
    await posts.set("c", 3)

    assert (await users.count(), await posts.count()) == (1, 2)


async def test_a_cold_start_of_many_workers_computes_each_value_once(app):
    """Ten processes coming up together against one cold cache is the moment a stampede happens."""
    users = app.space("users", ttl=timedelta(minutes=5))
    holding, calls = asyncio.Event(), []

    # A store that could not answer leaves that caller computing, so a second producer here is either a broken promise or a store that blinked, and the message has to say which.
    blinked = []
    app.on_error(lambda what, failure: blinked.append(f"{what}: {type(failure).__name__}"))

    async def load():
        calls.append(1)
        await holding.wait()

        return {"name": "Paulo"}

    asked = asyncio.gather(*[users.fetch("42", load) for _ in range(20)])

    await wait_until(lambda: len(calls) == 1)
    holding.set()

    assert await asked == [{"name": "Paulo"}] * 20
    assert calls == [1], f"twenty callers computed {len(calls)} values, and the store answered every call: {blinked}"

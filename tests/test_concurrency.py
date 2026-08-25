"""Many callers at one name at one instant, which is where a conditional write earns its place."""

import asyncio
from datetime import timedelta

from cachefy.clock import now
from cachefy.entry import MISS, Entry, minted
from cachefy.janitor import Janitor

CALLERS = 25
SPACE = "users"


async def test_only_one_of_many_callers_takes_a_free_name(store):
    moment = now()
    taken = await asyncio.gather(*[store.add(Entry(space=SPACE, key="42", value=index, created_at=moment), moment) for index in range(CALLERS)])
    won = [entry for entry in taken if entry is not None]

    assert len(won) == 1, "a name nothing holds is taken by exactly one caller"
    assert (await store.read(SPACE, "42", moment)).value == won[0].value


async def test_only_one_of_many_callers_takes_a_name_whose_value_has_died(store):
    moment = now()
    await store.write(Entry(space=SPACE, key="42", value="dead", expires_at=moment - timedelta(seconds=1), created_at=moment), moment)

    taken = await asyncio.gather(*[store.add(Entry(space=SPACE, key="42", value=index, created_at=moment), moment) for index in range(CALLERS)])
    won = [entry for entry in taken if entry is not None]

    assert len(won) == 1, "a name whose value has died is taken back by exactly one caller"
    assert (await store.read(SPACE, "42", moment)).value == won[0].value


async def test_only_one_of_many_callers_writes_a_value_back_over_one_version(store):
    moment = now()
    read = await store.write(Entry(space=SPACE, key="42", value="was", created_at=moment), moment)
    written = await asyncio.gather(*[store.swap(Entry(space=SPACE, key="42", value=index, created_at=moment), read.version, moment) for index in range(CALLERS)])
    won = [entry for entry in written if entry is not None]

    assert len(won) == 1, "the version that was read is one only one caller writes back over"
    assert (await store.read(SPACE, "42", moment)).value == won[0].value


async def test_a_read_and_a_value_written_back_never_lose_a_change(store):
    """Reading, working something out and writing it back is three steps with the world between them."""
    moment = now()
    await store.write(Entry(space=SPACE, key="42", value=0, created_at=moment), moment)

    async def counted() -> None:
        while True:
            held = await store.read(SPACE, "42", moment)

            if await store.swap(Entry(space=SPACE, key="42", value=held.value + 1, created_at=moment), held.version, moment) is not None:
                return

            await asyncio.sleep(0)

    async with asyncio.timeout(60):
        await asyncio.gather(*[counted() for _ in range(CALLERS)])

    assert (await store.read(SPACE, "42", moment)).value == CALLERS


async def test_many_callers_counting_one_name_never_lose_a_count(store):
    moment = now()
    counts = await asyncio.gather(*[store.bump(SPACE, "hits", 1, None, minted(), moment) for _ in range(CALLERS)])

    assert sorted(counts) == list(range(1, CALLERS + 1)), "every caller was handed a number no other caller was"
    assert (await store.read(SPACE, "hits", moment)).value == CALLERS


async def test_a_sweep_running_beside_a_write_never_drops_a_living_value(store):
    moment = now()

    for index in range(40):
        await store.write(Entry(space=SPACE, key=f"gone-{index}", value=index, expires_at=moment - timedelta(minutes=1), created_at=moment), moment)

    async def writing() -> None:
        for index in range(20):
            await store.write(Entry(space=SPACE, key=f"here-{index}", value=index, expires_at=moment + timedelta(hours=1), created_at=moment), moment)

    async def sweeping() -> None:
        while await store.purge(moment, 5):
            await asyncio.sleep(0)

    await asyncio.gather(writing(), sweeping(), sweeping())

    assert await store.count(SPACE, moment) == 20, "everything living is still there and everything dead is gone"


async def test_a_space_cleared_beside_a_write_leaves_nothing_half_there(store):
    moment = now()

    for index in range(20):
        await store.write(Entry(space=SPACE, key=f"k{index}", value=index, created_at=moment), moment)

    async def clearing() -> int:
        return await store.clear(SPACE)

    async def writing() -> None:
        for index in range(20, 30):
            await store.write(Entry(space=SPACE, key=f"k{index}", value=index, created_at=moment), moment)

    gone, _ = await asyncio.gather(clearing(), writing())

    assert 0 <= gone <= 30
    assert await store.count(SPACE, moment) == 30 - gone, "what a clear answered for is exactly what it took"


async def test_a_name_dropped_beside_an_add_is_held_by_one_caller_or_by_nobody(store):
    moment = now()

    for round_number in range(20):
        key = f"k{round_number}"
        await store.write(Entry(space=SPACE, key=key, value="was", created_at=moment), moment)

        dropped, taken = await asyncio.gather(store.drop(SPACE, key), store.add(Entry(space=SPACE, key=key, value="mine", created_at=moment), moment))
        held = await store.read(SPACE, key, moment)

        if taken is not None:
            assert held is None or held.value == "mine", "a caller that took the name is the only one that wrote it"

            continue

        assert dropped is True
        assert held is None or held.value == "was"


async def test_only_one_of_many_callers_computes_a_cold_name(app):
    users = app.space("users", ttl=timedelta(minutes=5))
    holding, calls = asyncio.Event(), []

    async def load():
        calls.append(1)
        await holding.wait()

        return "computed"

    asked = asyncio.gather(*[users.fetch("42", load) for _ in range(CALLERS)])

    while not calls:
        await asyncio.sleep(0.005)

    holding.set()

    assert await asked == ["computed"] * CALLERS
    assert calls == [1]


async def test_many_callers_reading_and_writing_one_name_never_read_half_of_one(app):
    """An entry is a value and every instant it is read by, so a read must never see one write's value beside another's instants."""
    users = app.space("users")
    written = [{"who": index, "tags": [index, index]} for index in range(CALLERS)]

    async def writing(value) -> None:
        for _ in range(4):
            await users.set("42", value)

    async def reading() -> list:
        seen = []

        for _ in range(20):
            held = await users.get("42", default=None)

            if held is not None:
                seen.append(held)

        return seen

    answers = await asyncio.gather(*[writing(value) for value in written], reading(), reading())

    for seen in answers[-2:]:
        for held in seen:
            assert held in written, "a read answered something no caller ever wrote"


async def test_a_sweep_never_takes_a_value_written_while_it_was_reading(app):
    """A sweep reads the dead names and then deletes them, and what is written into one of those names in between is a living value."""
    users = app.space("users", ttl=timedelta(minutes=5))
    janitor = Janitor(app, every=timedelta(hours=1), batch=500)

    for round in range(12):
        key = f"revived-{round}"
        moment = now()
        await app.store.write(Entry(space=SPACE, key=key, value="dead", expires_at=moment - timedelta(seconds=1), created_at=moment), moment)

        await asyncio.gather(janitor.sweep_once(), users.set(key, "living"))

        assert await users.get(key) == "living", "a sweep took the value a caller wrote while it was reading the dead names"


async def test_a_drop_and_a_touch_at_one_name_never_both_win(app):
    """A drop that answered for the name and a read that still finds the value are the one pair that cannot both be true."""
    users = app.space("users", ttl=timedelta(minutes=5))

    for round in range(12):
        key = f"contested-{round}"
        await users.set(key, "value")

        dropped, _ = await asyncio.gather(users.drop(key), users.touch(key, ttl=timedelta(minutes=10)))

        if dropped:
            assert await users.get(key) is MISS, "a drop took the name and the value was still there"

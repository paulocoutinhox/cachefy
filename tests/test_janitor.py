"""Sweeping what has died, because a cache nobody sweeps is a table that only ever grows."""

import asyncio
from datetime import timedelta

import pytest

from cachefy.clock import now
from cachefy.entry import Entry
from cachefy.errors import CacheError
from cachefy.janitor import SWEEP_EVERY, SWEEP_LIMIT, Janitor
from tests.conftest import wait_until

BRIEF = timedelta(milliseconds=20)


async def dead(store, space: str, key: str) -> None:
    """Writes a name whose value has already died."""
    moment = now()
    await store.write(Entry(space=space, key=key, value=1, expires_at=moment - timedelta(minutes=1), created_at=moment), moment)


def swept(store):
    """Answers a condition that holds once nothing dead is left, and that drops nothing while it asks."""

    async def nothing_left() -> bool:
        return await store.purge(now(), 100) == 0

    return nothing_left


async def test_a_sweep_drops_what_has_died_and_leaves_what_has_not(app):
    users = app.space("users")
    await users.set("here", 1)
    await dead(app.store, "users", "gone")

    assert await Janitor(app).sweep_once() == 1
    assert await users.get("here") == 1


async def test_a_sweep_takes_no_more_than_its_batch(app):
    for index in range(5):
        await dead(app.store, "users", f"gone-{index}")

    janitor = Janitor(app, batch=2)

    assert await janitor.sweep_once() == 2
    assert await janitor.sweep_once() == 2
    assert await janitor.sweep_once() == 1
    assert await janitor.sweep_once() == 0


async def test_the_loop_keeps_sweeping_until_it_is_asked_to_stop(app):
    users = app.space("users")
    await users.set("here", 1)

    for index in range(4):
        await dead(app.store, "users", f"gone-{index}")

    janitor = Janitor(app, every=BRIEF, batch=1)
    sweeping = asyncio.create_task(janitor.run())

    await wait_until(swept(app.store))

    janitor.stop()
    await sweeping

    assert janitor.stopping.is_set()
    assert await users.get("here") == 1


async def test_a_full_batch_is_followed_at_once_instead_of_waiting_the_interval_out(app):
    """A week that was never swept would otherwise take a week of intervals to catch up on."""
    for index in range(20):
        await dead(app.store, "users", f"gone-{index}")

    janitor = Janitor(app, every=timedelta(hours=1), batch=5)
    sweeping = asyncio.create_task(janitor.run())

    # An interval of an hour is what this would wait between batches if a full one were not followed at once.
    await wait_until(swept(app.store), patience=10.0)

    janitor.stop()
    await sweeping

    assert await app.store.purge(now(), 100) == 0, "the loop drained every batch without waiting an interval between any two of them"
    assert janitor.stopping.is_set()


async def test_a_janitor_asked_to_stop_returns_without_waiting_the_interval_out(app):
    janitor = Janitor(app, every=timedelta(hours=1))
    sweeping = asyncio.create_task(janitor.run())

    await asyncio.sleep(0.05)
    janitor.stop()

    # An hour is what this would wait if being asked to stop only ended the loop after the interval.
    async with asyncio.timeout(5):
        await sweeping

    assert janitor.stopping.is_set()


@pytest.mark.parametrize("every", [timedelta(0), timedelta(seconds=-1), 300, None, timedelta(days=4_000_000)])
def test_an_interval_nothing_could_sweep_on_is_refused(app, every):
    with pytest.raises(CacheError):
        Janitor(app, every=every)


@pytest.mark.parametrize("batch", [0, -1, 1.5, True, "five", None])
def test_a_batch_that_would_sweep_nothing_is_refused(app, batch):
    with pytest.raises(CacheError):
        Janitor(app, batch=batch)


def test_the_defaults_are_the_ones_a_janitor_is_built_with(app):
    janitor = Janitor(app)

    assert (janitor.waiting, janitor.batch) == (SWEEP_EVERY.total_seconds(), SWEEP_LIMIT)

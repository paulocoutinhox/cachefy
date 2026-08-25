"""The lifespan protocol every asgi framework speaks, honoured without importing one."""

from datetime import timedelta

from cachefy.asgi import lifespan_for
from cachefy.clock import now
from cachefy.entry import Entry
from cachefy.janitor import Janitor
from tests.conftest import wait_until


async def test_the_store_is_built_before_the_application_serves_a_request(app):
    built = []

    async def remember() -> None:
        built.append(1)

    app.setup = remember

    async with lifespan_for(Janitor(app))(object()):
        assert built == [1]


async def test_the_janitor_sweeps_beside_the_application_and_stops_with_it(app):
    users = app.space("users")
    moment = now()
    await app.store.write(Entry(space="users", key="gone", value=1, expires_at=moment - timedelta(minutes=1), created_at=moment), moment)
    await users.set("here", 1)

    janitor = Janitor(app, every=timedelta(milliseconds=20))

    async def swept() -> bool:
        return await app.store.purge(now(), 100) == 0

    async with lifespan_for(janitor)(object()):
        await wait_until(swept)

    assert janitor.stopping.is_set(), "leaving the block is what tells the janitor to stop"
    assert await users.get("here") == 1


async def test_the_janitor_is_stopped_even_when_the_application_broke(app):
    janitor = Janitor(app, every=timedelta(milliseconds=20))

    try:
        async with lifespan_for(janitor)(object()):
            raise RuntimeError("the application failed while it was serving")
    except RuntimeError:
        pass

    assert janitor.stopping.is_set()


async def test_the_store_is_built_before_anything_is_run_against_it(app):
    """A sweep that began before the store was built would reach one with nothing registered in it yet."""
    order = []
    building, sweeping = app.setup, app.store.purge

    async def remember() -> None:
        await building()
        order.append("built")

    async def swept(*arguments, **options):
        order.append("swept")

        return await sweeping(*arguments, **options)

    app.setup = remember
    app.store.purge = swept

    try:
        async with lifespan_for(Janitor(app, every=timedelta(milliseconds=10)))(object()):
            await wait_until(lambda: "swept" in order)
    finally:
        app.setup, app.store.purge = building, sweeping

    assert order[0] == "built", "the store was reached before it was built"

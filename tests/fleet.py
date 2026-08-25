"""The cache the processes of a fleet build, and the work each of them does against it."""

import asyncio
import json
import pathlib
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore
from cachefy.store.sqlalchemy import SqlAlchemyStore


def build_cache(url: str):
    """Answers the cache one process of a fleet works, and the call that closes what it opened."""
    if url.startswith("redis://"):
        client = Redis.from_url(url)

        return Cachefy(RedisStore(client)), client.aclose

    if not url.startswith("sqlite"):
        engine = create_async_engine(url, pool_pre_ping=True)

        return Cachefy(SqlAlchemyStore(engine)), engine.dispose

    # SQLite across processes needs both of these, which is the setup the documentation tells everybody to use.
    # Without the journal a reading process starves the writer beside it, and without the timeout a writer that met the lock is told the database is locked rather than waiting for it.
    engine = create_async_engine(url, connect_args={"timeout": 30})
    event.listen(engine.sync_engine, "connect", lambda connection, record: connection.execute("PRAGMA journal_mode=WAL"))

    return Cachefy(SqlAlchemyStore(engine)), engine.dispose


async def prepared(url: str) -> None:
    """Builds the store before the fleet starts, which is what a deploy does before the processes it is deploying come up."""
    # Four interpreters running the DDL of a fresh SQLite file in the same instant is a lock none of them can wait out.
    app, closing = build_cache(url)

    try:
        await app.setup()
    finally:
        await closing()


# How long a machine waits for the rest of the fleet to arrive before it gives up on meeting them.
GATHERING = 90.0


async def gathered(limits, name: str, machines: int) -> None:
    """Waits until every machine of the fleet has arrived, so none of them starts before the others are there."""
    # A wall clock they were each told to start at is one a loaded machine misses, and a fleet that never met proves nothing about a fleet that did.
    arrived = await limits.incr(f"{name}-ready")

    assert arrived is not None, "a machine could not even say it had arrived, so the store it was given never answered"

    ready = 0

    try:
        async with asyncio.timeout(GATHERING):
            while ready < machines:
                ready = await limits.get(f"{name}-ready", default=0)

                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"this machine arrived as number {arrived} and only {ready} of {machines} ever did") from None


async def work(settings: dict) -> None:
    """Runs one process of a fleet and writes down everything it saw."""
    # Nothing is built here: the store was built once before the fleet came up, which is what a deploy does and what the documentation tells everybody to do.
    app, closing = build_cache(settings["url"])

    users = app.space("users", ttl=timedelta(minutes=5))
    limits = app.space("limits")
    computed = []

    async def load() -> dict:
        computed.append(1)
        await asyncio.sleep(settings["computing"])

        return {"name": "Paulo"}

    try:
        await gathered(limits, settings["name"], settings["machines"])

        answers = [await users.fetch(settings["name"], load) for _ in range(settings["rounds"])]
        counts = [await limits.incr(settings["name"]) for _ in range(settings["counts"])]

        pathlib.Path(settings["output"]).write_text(json.dumps({"computed": len(computed), "answers": answers, "counts": counts}))
    finally:
        await closing()

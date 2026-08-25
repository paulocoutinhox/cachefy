import asyncio
import inspect
import os
import socket
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from cachefy.app import Cachefy
from cachefy.store.memory import MemoryStore
from cachefy.store.redis import RedisStore
from cachefy.store.sqlalchemy import SqlAlchemyStore
from cachefy.store.sqlalchemy import metadata as server_metadata

# The wait is generous because a traced run is an order of magnitude slower than an untraced one, and what makes it bounded is that it ends at all.
PATIENCE = 90.0

# The servers a developer or a runner has. A store nobody can reach is not collected, and one that is answers the same contract as every other.
SERVERS = {"redis": os.environ.get("CACHEFY_REDIS_URL", "redis://127.0.0.1:6398/0"), "mysql": os.environ.get("CACHEFY_MYSQL_URL", "mysql+aiomysql://root:root@127.0.0.1:3398/cachefy"), "postgres": os.environ.get("CACHEFY_POSTGRES_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5498/cachefy")}

PORTS = {"redis": 6398, "mysql": 3398, "postgres": 5498}

REDIS_URL = SERVERS["redis"]


# The most callers any suite here puts at one name at once, which is what the pool has to be able to answer.
CONCURRENT = 32


def sqlite_engine(path):
    """Answers an engine over a file and not a shared cache, because more than one connection has to see the same rows."""
    # The pool is sized for the concurrency the suite applies, exactly as the documentation tells everybody to size theirs.
    # Left at the default five, twenty-five callers queue behind fifteen connections while each write waits out the file lock, and on a slower interpreter they time out and the suite fails for the speed of the machine.
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30}, pool_size=CONCURRENT, max_overflow=CONCURRENT)

    # The setup the documentation tells everybody to use: without it a sweeping process starves the writer beside it.
    event.listen(engine.sync_engine, "connect", lambda connection, record: connection.execute("PRAGMA journal_mode=WAL"))

    return engine


def reachable(url: str, fallback: int) -> bool:
    """Answers whether that server is there to be tested against."""
    parts = urlsplit(url)

    try:
        with socket.socket() as probe:
            probe.settimeout(1.0)

            return probe.connect_ex((parts.hostname or "127.0.0.1", parts.port or fallback)) == 0
    except OSError:
        return False


STORES = ["memory", "sqlalchemy"] + [name for name in ("redis", "mysql", "postgres") if reachable(SERVERS[name], PORTS[name])]


async def wait_until(condition, patience: float = PATIENCE):
    """Waits for a condition with a bound, so one that never comes is a failure anybody can read and never a suite that spins."""
    async with asyncio.timeout(patience):
        while True:
            answer = condition()
            settled = await answer if inspect.isawaitable(answer) else answer

            if settled:
                return

            await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def sql_store(tmp_path):
    engine = sqlite_engine(tmp_path / "cache.db")
    store = SqlAlchemyStore(engine)
    await store.setup()

    yield store

    await engine.dispose()


@pytest_asyncio.fixture(params=STORES)
async def store(request, tmp_path):
    """Every store answers the same contract, and the way to keep that true is to run one suite against all of them."""
    if request.param == "memory":
        built = MemoryStore()
        await built.setup()

        yield built

        return

    if request.param == "redis":
        client = Redis.from_url(REDIS_URL)
        await client.flushdb()

        built = RedisStore(client)
        await built.setup()

        yield built

        await client.aclose()

        return

    if request.param in ("mysql", "postgres"):
        # The same schema a fresh database would get, so what runs here is what runs there.
        engine = create_async_engine(SERVERS[request.param], pool_pre_ping=True)

        async with engine.begin() as connection:
            await connection.run_sync(server_metadata.drop_all)

        built = SqlAlchemyStore(engine)
        await built.setup()

        yield built

        await engine.dispose()

        return

    engine = sqlite_engine(tmp_path / "cache.db")
    built = SqlAlchemyStore(engine)
    await built.setup()

    yield built

    await engine.dispose()


@pytest.fixture
def app(store):
    return Cachefy(store)

import asyncio
from contextlib import asynccontextmanager

from cachefy.janitor import Janitor


def lifespan_for(janitor: Janitor):
    """Answers a lifespan that builds the store and sweeps beside the application, which is the protocol every asgi framework speaks."""

    @asynccontextmanager
    async def lifespan(app):
        await janitor.app.setup()
        sweeping = asyncio.create_task(janitor.run())

        try:
            yield
        finally:
            janitor.stop()
            await sweeping

    return lifespan

import asyncio
from contextlib import suppress
from datetime import timedelta

from cachefy.clock import now, spanned, waited
from cachefy.errors import CacheError
from cachefy.store.base import BATCH_LIMIT

# How often a sweep happens when the last one found nothing left to drop.
SWEEP_EVERY = timedelta(minutes=5)

# How many dead entries one sweep drops, so a week that was never swept is caught up over passes and not in one statement that holds the table.
SWEEP_LIMIT = 500


class Janitor:
    """Drops what has died, because a cache nobody sweeps is a table that only ever grows."""

    def __init__(self, app, *, every: timedelta = SWEEP_EVERY, batch: int = SWEEP_LIMIT) -> None:
        self.waiting = spanned(every, "the wait between sweeps")

        if self.waiting <= 0:
            raise CacheError(f"a wait of {self.waiting}s between sweeps is a janitor that spends a core asking")

        waited(self.waiting, "the wait between sweeps")

        if type(batch) is not int or not 1 <= batch <= BATCH_LIMIT:
            raise CacheError(f"a batch of {batch!r} is not between one name and the {BATCH_LIMIT} one statement may name, so it is a sweep that drops nothing or one no database takes")

        self.app = app
        self.batch = batch
        self.stopping = asyncio.Event()

    async def sweep_once(self) -> int:
        """Drops one batch of what has already died, and answers how much that was."""
        return await self.app.guarded(self.app.store.purge(now(), self.batch), "sweep what has died", 0)

    async def run(self) -> None:
        """Sweeps until `stop`, and never lets a store that blinked end the loop."""
        while not self.stopping.is_set():
            # A full batch means there is more of it, so the next sweep follows at once instead of waiting the interval out.
            if await self.sweep_once() < self.batch:
                await self.wait()

    async def wait(self) -> None:
        """Waits out the interval, or returns as soon as it is asked to stop."""
        with suppress(TimeoutError):
            await asyncio.wait_for(self.stopping.wait(), timeout=self.waiting)

    def stop(self) -> None:
        """Asks the loop to end after the sweep it is on."""
        self.stopping.set()

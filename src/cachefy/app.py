import asyncio
import inspect
import logging
from datetime import timedelta
from typing import Callable

from cachefy.errors import CacheError, UnknownSpace
from cachefy.memo import Memo
from cachefy.space import LEASE, LOCKS, Space, generative
from cachefy.store.base import Store

logger = logging.getLogger(__name__)


class Cachefy:
    """What an application holds: the spaces it knows, and the store their entries live in."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.spaces: dict[str, Space] = {}
        self.hitting: list[Callable] = []
        self.missing: list[Callable] = []
        self.failing: list[Callable] = []

    def space(self, name: str, *, ttl: timedelta | None = None, stale: timedelta | None = None, lease: timedelta = LEASE) -> Space:
        """Declares a family of names and the policy every entry of it is written under."""
        if name in self.spaces:
            raise CacheError(f"'{name}' is declared twice, and a space has to mean one thing")

        # The names held while one caller computes live in this one, so an application sharing it would share them.
        if name == LOCKS:
            raise CacheError(f"'{name}' is where the names held while one caller computes live, so it is not a space an application may declare")

        self.spaces[name] = Space(self, name, ttl=ttl, stale=stale, lease=lease)

        return self.spaces[name]

    def space_for(self, name: str) -> Space:
        """Answers the space that name was declared as."""
        if name not in self.spaces:
            raise UnknownSpace(f"nothing here is called '{name}'")

        return self.spaces[name]

    def cached(self, name: str, *, ttl: timedelta | None = None, stale: timedelta | None = None, lease: timedelta = LEASE, key: Callable | None = None) -> Callable:
        """Declares a space and remembers what the function under it answered, keyed by the arguments each call was made with."""

        def declare(handler: Callable) -> Memo:
            # A classmethod is the one of these that is not callable, and reading its signature raises where the class is written under a name nothing in this family catches.
            if not callable(handler):
                raise CacheError(f"'{name}' is a {type(handler).__name__} rather than something that can be called, so put that decorator above this one and what is memoized is the function itself")

            # Calling a generator runs none of its body, so what would be kept under that name is the generator itself.
            if generative(handler):
                raise CacheError(f"'{name}' is a generator, and calling one runs none of what it was written to do")

            return Memo(self.space(name, ttl=ttl, stale=stale, lease=lease), handler, key)

        return declare

    async def setup(self) -> None:
        """Builds whatever the store needs to hold entries."""
        await self.store.setup()

    def on_hit(self, listener: Callable) -> Callable:
        """Registers a listener called with the space and the key of every name that held something."""
        self.hitting.append(listener)

        return listener

    def on_miss(self, listener: Callable) -> Callable:
        """Registers a listener called with the space and the key of every name that held nothing."""
        self.missing.append(listener)

        return listener

    def on_error(self, listener: Callable) -> Callable:
        """Registers a listener called with what was being done and what broke, for every call the store could not answer."""
        self.failing.append(listener)

        return listener

    async def announce(self, listeners: list[Callable], *arguments) -> None:
        """Tells the listeners, and lets one that breaks break alone."""
        for listener in listeners:
            try:
                outcome = listener(*arguments)

                # A plain function is a listener too, and awaiting what it returned would be the error nobody reads.
                if inspect.isawaitable(outcome):
                    await outcome
            except asyncio.CancelledError:
                # This one is the shutdown asking, and swallowing it would leave a loop nobody can stop.
                raise
            except BaseException:
                # A library calling `sys.exit` deep inside a listener is not an `Exception`, and it must never end the request that was only reading a cache.
                # This one keeps its traceback where a store failure does not, because a listener that raises is a bug in the calling code rather than a state anything here expects.
                logger.exception("a listener failed")

    async def told(self, space: str, key: str, hit: bool) -> None:
        """Tells the listeners whether that name held something."""
        await self.announce(self.hitting if hit else self.missing, space, key)

    async def guarded(self, work, what: str, fallback):
        """Answers what the store said, or the fallback when the store could not be reached at all."""
        # A cache is what makes an application faster and never what makes it work, so nothing a store does is ever raised at whoever asked.
        try:
            return await work
        except Exception as failure:
            # What is written down is the failure and never its traceback, because this is on the path of every request.
            # Measured: a thousand reads against a store that is gone wrote nine thousand lines and four hundred kilobytes, which is a log pipeline drowned at the one moment anybody needs it.
            # Whoever wants the traceback registers `on_error`, which is handed the failure itself.
            logger.warning("could not %s: %s: %s", what, type(failure).__name__, failure)
            await self.announce(self.failing, what, failure)

            return fallback

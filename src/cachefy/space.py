import asyncio
import inspect
import logging
from contextvars import ContextVar
from datetime import datetime, timedelta
from time import monotonic

from cachefy.clock import now, spanned, waited
from cachefy.codec import as_written
from cachefy.entry import MISS, VERSION_BITS, Entry, minted
from cachefy.errors import CacheError, UnwritableValue
from cachefy.keys import digested, joined, named, spaced
from cachefy.store.base import BATCH_LIMIT, COUNTER_CEILING, COUNTER_FLOOR

logger = logging.getLogger(__name__)

# Where the names held while one caller computes live, kept apart from every space an application declares.
LOCKS = "cachefy-locks"

# How long a caller may hold a name while it computes, which is also the longest anybody waits on it.
LEASE = timedelta(seconds=30)

# How often a caller waiting on another one asks whether the value has landed.
WAITING = 0.02

# The names this caller is already computing, so a producer that asks for one of them again is told rather than left waiting out a lease it can never be let go of.
computing: ContextVar[frozenset] = ContextVar("computing", default=frozenset())


class Declared:
    """What a call means when it says nothing about how long a value lives, which is that the space it belongs to decides."""

    def __repr__(self) -> str:
        return "DECLARED"


DECLARED = Declared()


class Unanswered:
    """What a read means when the store could not be reached at all, which is not the same thing as a name holding nothing."""

    def __repr__(self) -> str:
        return "UNANSWERED"


UNANSWERED = Unanswered()


def lived(span, what: str) -> timedelta | None:
    """Refuses a span a value could never be kept for, and answers it back."""
    if span is None:
        return None

    seconds = spanned(span, what)

    if seconds <= 0:
        raise CacheError(f"{what} is {seconds}s, which is a value already dead where it is written")

    waited(seconds, what)

    return span


def policed(ttl, stale, what: str) -> tuple[timedelta | None, timedelta | None]:
    """Refuses a lifetime and a freshness that could not both hold, and answers them back."""
    living = lived(ttl, f"the lifetime {what}")
    fresh = lived(stale, f"the freshness {what}")

    if fresh is not None and living is not None and fresh >= living:
        raise CacheError(f"the freshness {what} is {fresh} and the lifetime is {living}, so the value would die before anything ever refreshed it")

    return living, fresh


def leased(span, what: str) -> timedelta:
    """Refuses a span a name could not be held for while one caller computes, and answers it back."""
    # A name held for ever is one a caller that died never lets go of, so every caller after it waits on a value nobody is computing.
    if span is None:
        raise CacheError(f"{what} is nothing at all, and a name held while one caller computes has to be let go of")

    return lived(span, what)


def whole(value) -> bool:
    """Answers whether this is a whole number, which a boolean is to Python and is not to a store."""
    return isinstance(value, int) and type(value) is not bool


def number(value) -> int:
    """Answers the whole number it holds, past whatever it answers for itself."""
    # An int subclass compares and converts however it likes, and what a store is handed is the number behind all of that.
    return int.__index__(value)


def numbered(version, what: str) -> int:
    """Refuses a version no entry could ever have been written under, and answers the number it is."""
    # Left through, a version a store cannot compare answers 'somebody wrote in between' rather than saying what was wrong with it.
    if not whole(version) or not 0 <= number(version) < 2**VERSION_BITS:
        raise CacheError(f"{what} is {version!r}, and a version is the whole number an entry was written under")

    return number(version)


def generative(handler) -> bool:
    """Answers whether calling this runs none of its body and hands back a generator instead."""
    # An object is asked about its `__call__` and never about itself, because what `inspect` reads is the code of what it is handed and an instance has none.
    return any(inspect.isgeneratorfunction(shape) or inspect.isasyncgenfunction(shape) for shape in (handler, getattr(handler, "__call__", None)))


def produces(producer, what: str) -> None:
    """Refuses a producer that could never compute a value."""
    # Left through, it raises from inside the call and is answered around by whatever value happened to be there.
    if not callable(producer):
        raise CacheError(f"{what} is {type(producer).__name__}, and what computes a value is something that can be called")

    # Calling a generator runs none of its body, so what is handed back is the generator itself and no store can hold one.
    if generative(producer):
        raise CacheError(f"{what} is a generator, and calling one runs none of what it was written to do")


def stepped(amount, what: str) -> int:
    """Refuses an amount a counter could not be moved by, and answers the number it is."""
    if not whole(amount):
        raise CacheError(f"{what} is {amount!r}, and what a counter moves by is a whole number")

    if not COUNTER_FLOOR <= number(amount) <= COUNTER_CEILING:
        raise CacheError(f"{what} is {number(amount)}, and a count runs from {COUNTER_FLOOR} to {COUNTER_CEILING}")

    return number(amount)


class Space:
    """One family of names and the policy every entry of it is written under."""

    def __init__(self, app, name: str, *, ttl: timedelta | None = None, stale: timedelta | None = None, lease: timedelta = LEASE) -> None:

        self.ttl, self.stale = policed(ttl, stale, f"'{name}' was declared with")
        self.lease = leased(lease, f"the lease of '{name}'")

        self.app = app
        self.name = spaced(name)

    def spans(self, ttl, stale) -> tuple[timedelta | None, timedelta | None]:
        """Answers the two spans a value written by this call lives and stays fresh for."""
        if ttl is DECLARED and stale is DECLARED:
            return self.ttl, self.stale

        return policed(self.ttl if ttl is DECLARED else ttl, self.stale if stale is DECLARED else stale, f"this call gives '{self.name}'")

    def lifetime(self, ttl) -> timedelta | None:
        """Answers the span a value written by this call lives for, which is all a count is ever written under."""
        if ttl is DECLARED:
            return self.ttl

        return lived(ttl, f"the lifetime this call gives '{self.name}'")

    def entry_for(self, key: str, value, living, fresh, moment: datetime) -> Entry:
        """Answers the entry a write of this value puts under the name."""
        return Entry(space=self.name, key=key, value=as_written(value, f"the value of '{self.name}:{key}'"), expires_at=None if living is None else moment + living, stale_at=None if fresh is None else moment + fresh, created_at=moment)

    async def held(self, key: str, moment: datetime) -> Entry | None:
        """Answers the entry this name holds, and nothing at all when it holds nothing or the store could not say."""
        return await self.app.guarded(self.app.store.read(self.name, key, moment), f"read '{self.name}:{key}'", None)

    async def get(self, key: str, default=MISS):
        """Answers what this name holds, and `default` when it holds nothing."""
        key = named(key)

        moment = now()
        found = await self.held(key, moment)

        await self.app.told(self.name, key, found is not None)

        return default if found is None else found.value

    async def entry(self, key: str) -> Entry | None:
        """Answers the whole entry this name holds, which is what a caller writing a value back against a version needs."""
        key = named(key)

        return await self.held(key, now())

    async def get_many(self, keys) -> dict:
        """Answers what each of these names holds, in as few round trips as the store can answer in."""
        # The names are walked more than once, so what is handed in is read out whole before anything asks for it, and each is answered for and settled where it is read.
        asked = tuple(named(key) for key in keys)

        # One name asked for twice is still one name: one lookup, one answer, and one hit or miss told for it.
        wanted = tuple(dict.fromkeys(asked))

        moment = now()
        found = {}

        for start in range(0, len(wanted), BATCH_LIMIT):
            batch = wanted[start : start + BATCH_LIMIT]
            found |= await self.app.guarded(self.app.store.read_many(self.name, batch, moment), f"read {len(batch)} names of '{self.name}'", {})

        for key in wanted:
            await self.app.told(self.name, key, key in found)

        return {key: entry.value for key, entry in found.items()}

    async def set(self, key: str, value, *, ttl=DECLARED, stale=DECLARED) -> Entry | None:
        """Puts the value under the name whatever was there, and answers nothing when the store could not take it."""
        key = named(key)

        living, fresh = self.spans(ttl, stale)
        moment = now()

        return await self.app.guarded(self.app.store.write(self.entry_for(key, value, living, fresh, moment), moment), f"write '{self.name}:{key}'", None)

    async def add(self, key: str, value, *, ttl=DECLARED, stale=DECLARED) -> Entry | None:
        """Puts the value under the name only while it holds nothing, and answers nothing when it is already held."""
        key = named(key)

        living, fresh = self.spans(ttl, stale)
        moment = now()

        return await self.app.guarded(self.app.store.add(self.entry_for(key, value, living, fresh, moment), moment), f"add '{self.name}:{key}'", None)

    async def swap(self, key: str, value, version: int, *, ttl=DECLARED, stale=DECLARED) -> Entry | None:
        """Puts the value under the name only while it still holds the version the caller read, and answers nothing when somebody wrote in between."""
        key = named(key)
        version = numbered(version, f"the version this call writes '{self.name}:{key}' back over")

        living, fresh = self.spans(ttl, stale)
        moment = now()

        return await self.app.guarded(self.app.store.swap(self.entry_for(key, value, living, fresh, moment), version, moment), f"swap '{self.name}:{key}'", None)

    async def drop(self, key: str) -> bool:
        """Takes the name away, and answers false for one that held nothing."""
        key = named(key)

        return await self.app.guarded(self.app.store.drop(self.name, key), f"drop '{self.name}:{key}'", False)

    async def touch(self, key: str, *, ttl=DECLARED, stale=DECLARED) -> bool:
        """Moves the instants a living entry dies and goes stale at, without touching what it holds."""
        key = named(key)

        living, fresh = self.spans(ttl, stale)
        moment = now()

        return await self.app.guarded(self.app.store.touch(self.name, key, None if living is None else moment + living, None if fresh is None else moment + fresh, minted(), moment), f"touch '{self.name}:{key}'", False)

    async def incr(self, key: str, amount: int = 1, *, ttl=DECLARED) -> int | None:
        """Answers the count under this name after adding to it, and nothing at all when the name cannot be counted in."""
        key = named(key)
        amount = stepped(amount, f"what this call moves '{self.name}:{key}' by")

        # A count is answered for by its lifetime alone, because a freshness is what tells a caller to recompute and nothing ever recomputes a count.
        living = self.lifetime(ttl)
        moment = now()

        return await self.app.guarded(self.app.store.bump(self.name, key, amount, None if living is None else moment + living, minted(), moment), f"count '{self.name}:{key}'", None)

    async def clear(self) -> int:
        """Drops everything this space holds, and answers how much that was."""
        return await self.app.guarded(self.app.store.clear(self.name), f"clear '{self.name}'", 0)

    async def count(self) -> int:
        """Answers how many living entries this space holds."""
        return await self.app.guarded(self.app.store.count(self.name, now()), f"count '{self.name}'", 0)

    async def fetch(self, key: str, producer, *, ttl=DECLARED, stale=DECLARED):
        """Answers what this name holds, computing it once however many callers ask for it at the same instant."""
        key = named(key)
        produces(producer, f"what this call computes '{self.name}:{key}' with")

        if (self.name, key) in computing.get():
            raise CacheError(f"'{self.name}:{key}' is being computed by the very call that asked for it, and a caller cannot wait for a name it holds itself")

        living, fresh = self.spans(ttl, stale)
        moment = now()
        found = await self.held(key, moment)

        await self.app.told(self.name, key, found is not None)

        if found is not None and not found.stale(moment):
            return found.value

        holder = await self.take(key, moment)

        # Another caller is already computing this name, so what is there is served as it stands and a name holding nothing is waited on.
        if holder is None:
            return found.value if found is not None else await self.awaited(key, producer, living, fresh)

        try:
            return await self.produced(key, producer, living, fresh)
        except CacheError:
            # A refusal is a caller mistake and never a bad minute, so it is never answered around with whatever value happened to be there.
            raise
        except Exception as refusal:
            # A value that is only stale is still a value, so a refresh that broke is served from what is there rather than failed at the caller.
            if found is None:
                raise

            logger.warning("could not refresh '%s:%s', so what is there is served instead: %s", self.name, key, refusal)
            await self.app.announce(self.app.failing, f"refresh '{self.name}:{key}'", refusal)

            return found.value
        finally:
            # Letting the name go is shielded, because a caller cancelled a second time is cancelled again inside this very await — and a release that never landed is the name held for a whole lease.
            await asyncio.shield(self.freed(holder, now()))

    async def take(self, key: str, moment: datetime) -> Entry | None:
        """Holds the name while this caller computes, and answers nothing when another caller already holds it."""
        holder = Entry(space=LOCKS, key=digested(joined(self.name, key)), expires_at=moment + self.lease, created_at=moment)

        # A store that could not answer leaves this caller computing rather than waiting on a name nothing can tell it about.
        return await self.app.guarded(self.app.store.add(holder, moment), f"take '{self.name}:{key}'", holder)

    async def freed(self, holder: Entry, moment: datetime) -> None:
        """Lets the name go, so the caller after this one never waits a lease out for a value that is already there."""
        held = holder.version

        holder.expires_at = moment
        holder.stale_at = None
        holder.version = minted()

        # What this is written against is the version this caller drew when it took the name, so it can never let go of one a second caller took after the lease ran out.
        await self.app.guarded(self.app.store.swap(holder, held, moment), f"free '{self.name}'", None)

    async def awaited(self, key: str, producer, living, fresh):
        """Waits for the caller holding the name to write the value, and computes it here when that never happens."""
        deadline = monotonic() + self.lease.total_seconds()

        while monotonic() < deadline:
            await asyncio.sleep(WAITING)

            found = await self.app.guarded(self.app.store.read(self.name, key, now()), f"read '{self.name}:{key}'", UNANSWERED)

            # A caller must never wait on a store that has stopped answering, so this one stops waiting and computes.
            if found is UNANSWERED:
                break

            if found is not None:
                return found.value

        return await self.produced(key, producer, living, fresh)

    async def produced(self, key: str, producer, living, fresh):
        """Calls the code the caller gave and keeps what it answered."""
        marked = computing.set(computing.get() | {(self.name, key)})

        try:
            value = producer()

            if inspect.isawaitable(value):
                value = await value
        finally:
            computing.reset(marked)

        moment = now()

        try:
            entry = self.entry_for(key, value, living, fresh, moment)
        except UnwritableValue as refusal:
            # A cache must never be the reason a request fails, so an answer no store can hold is handed back uncached.
            logger.warning("could not keep what '%s:%s' answered: %s", self.name, key, refusal)
            await self.app.announce(self.app.failing, f"keep '{self.name}:{key}'", refusal)

            return value

        await self.app.guarded(self.app.store.write(entry, moment), f"write '{self.name}:{key}'", None)

        # What is answered is the value as every store reads it back, or the caller that computed it would be handed a shape nobody after it ever sees.
        return entry.value

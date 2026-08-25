from abc import ABC, abstractmethod
from datetime import datetime

from cachefy.entry import Entry

# What every store sizes the two columns that name an entry for.
SPACE_LIMIT = 64
KEY_LIMIT = 255

# The most a store keeps one value as, counted in the characters it is written down as.
# A cache is memory somebody else is paying for, and one entry able to take all of it is a cache that refuses every write after it.
VALUE_LIMIT = 1024 * 1024

# How many names one statement may name, which bounds both a read of many and a sweep.
# Measured: PostgreSQL refuses a statement naming about twenty thousand while SQLite and MySQL take it, so this leaves twenty times the room.
BATCH_LIMIT = 1000

# The whole numbers MySQL keeps inside a JSON value as whole numbers, turning everything either side of them into a double.
# A value of 10**40 is read back off MySQL as 9.999999999999998e+39 while every other store reads back the number that was written.
WHOLE_FLOOR = -(2**63)
WHOLE_CEILING = 2**64 - 1

# What a count stays exact through wherever it is added up, which is the whole numbers a double holds.
# Redis adds a counter up in Lua, and Lua counts in doubles: one step past the range is still exact there, and one step past a range reaching 2**53 is not.
COUNTER_FLOOR = -(2**53 - 1)
COUNTER_CEILING = 2**53 - 1


class Store(ABC):
    """Where entries live. Every method that changes one is conditional on the state it was in, which is what makes two callers safe without a lock anywhere.

    A version is drawn where the entry is built and written down unchanged, so what tells two writes apart is the same number wherever the entries live.

    An entry arriving here is already settled: the space and the key are plain strings and the value is something json writes down, so a store validates none of it.
    """

    @abstractmethod
    async def setup(self) -> None:
        """Builds whatever the store needs to hold entries, and does nothing when it is already there."""

    @abstractmethod
    async def read(self, space: str, key: str, moment: datetime) -> Entry | None:
        """Answers what this name holds at that instant, and nothing at all when it holds nothing or what it holds has died."""

    @abstractmethod
    async def read_many(self, space: str, keys: tuple[str, ...], moment: datetime) -> dict[str, Entry]:
        """Answers the same for many names in one round trip, keyed by the names that held something."""

    @abstractmethod
    async def write(self, entry: Entry, moment: datetime) -> Entry:
        """Puts the value under the name whatever was there, and answers with the version it minted."""

    @abstractmethod
    async def add(self, entry: Entry, moment: datetime) -> Entry | None:
        """Writes only while the name is free or what holds it has died, and answers nothing when it is held."""

    @abstractmethod
    async def swap(self, entry: Entry, version: int, moment: datetime) -> Entry | None:
        """Writes only while the name still holds the living version the caller read, and answers nothing when somebody wrote in between."""

    @abstractmethod
    async def drop(self, space: str, key: str) -> bool:
        """Takes the name away, and answers false for one that held nothing."""

    @abstractmethod
    async def touch(self, space: str, key: str, expires_at: datetime | None, stale_at: datetime | None, version: int, moment: datetime) -> bool:
        """Moves the instants a living entry dies and goes stale at, under a new version, without touching what it holds."""

    @abstractmethod
    async def bump(self, space: str, key: str, amount: int, expires_at: datetime | None, version: int, moment: datetime) -> int | None:
        """Answers the count after adding to it in one atomic step, under a new version, and nothing at all for a name this cannot count in."""

    @abstractmethod
    async def clear(self, space: str) -> int:
        """Drops everything one space holds, and answers how much that was."""

    @abstractmethod
    async def purge(self, before: datetime, limit: int) -> int:
        """Drops entries that were already dead at that instant, longest dead first, up to `limit` of them."""

    @abstractmethod
    async def count(self, space: str | None, moment: datetime) -> int:
        """Answers how many living entries there are, in one space or in all of them."""

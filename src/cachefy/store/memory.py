from copy import deepcopy
from datetime import datetime

from cachefy.entry import Entry
from cachefy.store.base import COUNTER_CEILING, COUNTER_FLOOR, Store


class MemoryStore(Store):
    """Entries held in the process that owns it, which makes it right for tests and for a single process and wrong for two."""

    def __init__(self) -> None:
        # Every method here changes an entry without awaiting, so nothing can interleave inside one and there is no lock to hold.
        self.entries: dict[tuple[str, str], Entry] = {}

    async def setup(self) -> None:
        return None

    def living(self, space: str, key: str, moment: datetime) -> Entry | None:
        """Answers the entry this name holds while it is still there, and nothing at all otherwise."""
        held = self.entries.get((space, key))

        return held if held is not None and held.alive(moment) else None

    def put(self, entry: Entry) -> Entry:
        """Writes the entry under its name, whatever was there."""
        # The store keeps a copy of its own, because a caller that goes on changing what it wrote must never change the entry.
        self.entries[(entry.space, entry.key)] = deepcopy(entry)

        return entry

    async def read(self, space: str, key: str, moment: datetime) -> Entry | None:
        held = self.living(space, key, moment)

        return deepcopy(held) if held is not None else None

    async def read_many(self, space: str, keys: tuple[str, ...], moment: datetime) -> dict[str, Entry]:
        found = {}

        for key in keys:
            held = self.living(space, key, moment)

            if held is not None:
                found[key] = deepcopy(held)

        return found

    async def write(self, entry: Entry, moment: datetime) -> Entry:
        return self.put(entry)

    async def add(self, entry: Entry, moment: datetime) -> Entry | None:
        if self.living(entry.space, entry.key, moment) is not None:
            return None

        return self.put(entry)

    async def swap(self, entry: Entry, version: int, moment: datetime) -> Entry | None:
        held = self.living(entry.space, entry.key, moment)

        if held is None or held.version != version:
            return None

        return self.put(entry)

    async def drop(self, space: str, key: str) -> bool:
        return self.entries.pop((space, key), None) is not None

    async def touch(self, space: str, key: str, expires_at: datetime | None, stale_at: datetime | None, version: int, moment: datetime) -> bool:
        held = self.living(space, key, moment)

        if held is None:
            return False

        held.expires_at = expires_at
        held.stale_at = stale_at
        held.version = version

        return True

    async def bump(self, space: str, key: str, amount: int, expires_at: datetime | None, version: int, moment: datetime) -> int | None:
        held = self.living(space, key, moment)
        counted = 0 if held is None else held.value

        if type(counted) is not int or not COUNTER_FLOOR <= counted <= COUNTER_CEILING:
            return None

        total = counted + amount

        if not COUNTER_FLOOR <= total <= COUNTER_CEILING:
            return None

        # The instants are taken only where this makes the name, because a counter is asked for a window that starts on the first call.
        if held is None:
            self.put(Entry(space=space, key=key, value=total, expires_at=expires_at, created_at=moment, version=version))

            return total

        held.value = total
        held.version = version

        return total

    async def clear(self, space: str) -> int:
        gone = [name for name in self.entries if name[0] == space]

        for name in gone:
            del self.entries[name]

        return len(gone)

    async def purge(self, before: datetime, limit: int) -> int:
        # The instant itself is taken, because an entry dying exactly then is one every read already answers as gone.
        dead = [name for name, entry in self.entries.items() if entry.expires_at is not None and entry.expires_at <= before]

        # What has been dead longest goes first, or a batch that always picks the same end leaves the rest lying there for ever.
        gone = sorted(dead, key=lambda name: (self.entries[name].expires_at, name))[:limit]

        for name in gone:
            del self.entries[name]

        return len(gone)

    async def count(self, space: str | None, moment: datetime) -> int:
        return len([entry for name, entry in self.entries.items() if (space is None or name[0] == space) and entry.alive(moment)])

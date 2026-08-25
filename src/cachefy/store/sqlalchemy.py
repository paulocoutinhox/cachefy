import asyncio
import json
import random
import sqlite3
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Column, DateTime, Index, MetaData, String, Table, Text, TypeDecorator, and_, delete, func, insert, or_, select, tuple_, update
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME
from sqlalchemy.dialects.mysql import VARCHAR as MYSQL_VARCHAR
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cachefy.clock import as_utc, naive_utc
from cachefy.entry import Entry
from cachefy.errors import CacheError
from cachefy.store.base import COUNTER_CEILING, COUNTER_FLOOR, KEY_LIMIT, SPACE_LIMIT, Store

# The store keeps its own metadata, so building its table never touches a table of the application around it.
metadata = MetaData()

# InnoDB answers contention by rolling one side back and asking for the transaction again, which is the handling MySQL documents.
# A deadlock is 1213 and a lock it waited too long for is 1205.
CONTENDED = frozenset({1205, 1213})

# SQLite answers the same thing where a second connection holds the write lock, and in WAL it answers at once rather than waiting the busy timeout out.
# It reports what happened as a code of its own rather than as a number in `args`, which is why it is asked for separately.
LOCKED = frozenset({sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED})

TRIES = 8

# The wait is short to begin with because the other side only has to finish the statement it is already in, and it doubles because contention comes in bursts.
BACKOFF = 0.005

# The wait is drawn as well, or every transaction InnoDB rolled back comes back in lockstep to deadlock on the others again.
SPREAD = 1.0

# How many times a write is asked again after the name it found nothing under turned out to be taken by the time it wrote.
# What this bounds is never that coincidence but a store answering both ways for ever, which would hang the call of whoever made it.
REWRITES = 3

# How many times a count is written back after another caller wrote the same name in between.
# Every refusal means somebody else's count landed, so a name is never stuck: what this bounds is one caller waiting behind a name hotter than the store can serialize.
COUNTS = 64


def identifier(length: int) -> String:
    """Answers a column that names an entry, compared code point by code point wherever the entries live."""
    # MySQL builds one under `utf8mb4_0900_ai_ci` unless it is told otherwise, which folds case and accents away before it compares.
    return String(length).with_variant(MYSQL_VARCHAR(length, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql")


def contended(error: DBAPIError) -> bool:
    """Answers whether the database asked for this write to be made again."""
    reported = getattr(error.orig, "sqlite_errorcode", None)

    if reported is not None:
        return reported in LOCKED

    codes = getattr(error.orig, "args", ())

    return bool(codes) and codes[0] in CONTENDED


async def spread_out(waiting: float) -> None:
    """Waits before asking again, drawn so callers refused over one row do not come back in lockstep."""
    await asyncio.sleep(waiting * (1 + random.uniform(0, SPREAD)))


async def under_contention(work):
    """Runs a write again when the database asked for it, and lets anything else through untouched."""
    for attempt in range(TRIES - 1):
        try:
            return await work()
        except DBAPIError as error:
            if not contended(error):
                raise

            await spread_out(BACKOFF * (2**attempt))

    return await work()


class JsonText(TypeDecorator):
    """Holds a value as the json text it is, for a database that would otherwise decide what type that text looks like."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value, dialect):
        return json.loads(value)


class UtcDateTime(TypeDecorator):
    """Holds naive UTC in the database and answers aware UTC to the application."""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        # MySQL rounds a datetime with no fractional precision, so an entry dying at 10:00:00.9 would be stored dying at 10:00:01.
        if dialect.name == "mysql":
            return dialect.type_descriptor(MYSQL_DATETIME(fsp=6))

        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value, dialect):
        return naive_utc(value)

    def process_result_value(self, value, dialect):
        return as_utc(value)


entries = Table(
    "cachefy_entry",
    metadata,
    # The name is the identity and there is no surrogate id anywhere, because what tells one entry from another is what a caller asks for it by.
    Column("space", identifier(SPACE_LIMIT), primary_key=True),
    Column("key", identifier(KEY_LIMIT), primary_key=True),
    # A value of `None` is written down as the json null it is, or a function that legitimately answers nothing would be read back as no entry at all.
    # On SQLite the column is declared as text, because a column typed `JSON` there takes numeric affinity and reads 2**64-1 back as 1.8446744073709552e+19.
    Column("value", JSON(none_as_null=False).with_variant(JsonText(), "sqlite"), nullable=False),
    Column("expires_at", UtcDateTime, nullable=True),
    Column("stale_at", UtcDateTime, nullable=True),
    Column("created_at", UtcDateTime, nullable=False),
    # Drawn where the entry is built and written down unchanged, because a version seen twice is a value written back over one nobody read.
    Column("version", BigInteger, nullable=False),
    # What a sweep asks for, so dropping a day of dead entries is not a scan of everything ever cached.
    Index("cachefy_entry_dying", "expires_at"),
)


def to_entry(row) -> Entry:
    """Answers the entry a row holds."""
    return Entry(space=row.space, key=row.key, value=row.value, expires_at=row.expires_at, stale_at=row.stale_at, created_at=row.created_at, version=row.version)


class SqlAlchemyStore(Store):
    """Entries held in the database the application already has, which is what lets many processes read one cache without any of them talking to the others."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.sessions = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def setup(self) -> None:
        try:
            async with self.engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
        except DBAPIError:
            # Asking whether the table is there and creating it are a question and a statement with a gap between them, and replicas booting together land in it.
            # Nothing here reads an error message: with the table there this does nothing, and with it still missing this raises for whatever the real reason was.
            async with self.engine.begin() as connection:
                await connection.run_sync(metadata.create_all)

    def at(self, space: str, key: str):
        """Answers the condition that names one entry."""
        return and_(entries.c.space == space, entries.c.key == key)

    def living(self, moment: datetime):
        """Answers the condition an entry still there at that instant meets."""
        return or_(entries.c.expires_at.is_(None), entries.c.expires_at > moment)

    def dead(self, moment: datetime):
        """Answers the condition an entry already gone at that instant meets."""
        return and_(entries.c.expires_at.is_not(None), entries.c.expires_at <= moment)

    def changes(self, entry: Entry) -> dict:
        """Answers what an update writes over an entry that is already there."""
        return {"value": entry.value, "expires_at": entry.expires_at, "stale_at": entry.stale_at, "created_at": entry.created_at, "version": entry.version}

    def values_of(self, entry: Entry) -> dict:
        """Answers what an insert writes for a name nothing has ever held."""
        return {"space": entry.space, "key": entry.key} | self.changes(entry)

    async def write(self, entry: Entry, moment: datetime) -> Entry:
        return await under_contention(lambda: self.replace(entry))

    async def replace(self, entry: Entry) -> Entry:
        """Writes the value under the name whatever was there."""
        # Nothing here reads the row, because the version is one the entry already carries — so there is no statement whose answer this has to wait for and read back.
        for _ in range(REWRITES):
            async with self.sessions() as session:
                if await self.moved(session, entry.space, entry.key, self.changes(entry)) is None and not await self.started(session, entry):
                    continue

                await session.commit()

                return entry

        raise CacheError(f"'{entry.space}:{entry.key}' was found holding nothing and then found taken, {REWRITES} times over")

    async def moved(self, session, space: str, key: str, values: dict, *conditions) -> bool | None:
        """Answers true when the write took the row, and nothing at all when the row was not in the state those conditions name."""
        return True if (await session.execute(update(entries).where(self.at(space, key), *conditions).values(**values))).rowcount == 1 else None

    async def started(self, session, entry: Entry) -> bool:
        """Answers whether the name nothing had ever held is now this entry."""
        try:
            await session.execute(insert(entries).values(**self.values_of(entry)))
        except IntegrityError:
            # Another caller wrote this name between the update that found nothing and the insert, so what is there now is a row the update would have taken.
            await session.rollback()

            return False

        return True

    async def add(self, entry: Entry, moment: datetime) -> Entry | None:
        return await under_contention(lambda: self.reserve(entry, moment))

    async def reserve(self, entry: Entry, moment: datetime) -> Entry | None:
        """Writes the value only over a name that is free or already dead."""
        async with self.sessions() as session:
            # A duplicate on the insert is another caller holding the name, which is exactly the answer this call exists to give.
            if await self.moved(session, entry.space, entry.key, self.changes(entry), self.dead(moment)) is None and not await self.started(session, entry):
                return None

            await session.commit()

            return entry

    async def swap(self, entry: Entry, version: int, moment: datetime) -> Entry | None:
        return await under_contention(lambda: self.exchange(entry, version, moment))

    async def exchange(self, entry: Entry, version: int, moment: datetime) -> Entry | None:
        """Writes the value only while the name still holds the living version the caller read."""
        async with self.sessions() as session:
            if await self.moved(session, entry.space, entry.key, self.changes(entry), entries.c.version == version, self.living(moment)) is None:
                return None

            await session.commit()

            return entry

    async def read(self, space: str, key: str, moment: datetime) -> Entry | None:
        async with self.sessions() as session:
            row = (await session.execute(select(entries).where(self.at(space, key), self.living(moment)))).one_or_none()

            return to_entry(row) if row is not None else None

    async def read_many(self, space: str, keys: tuple[str, ...], moment: datetime) -> dict[str, Entry]:
        if not keys:
            return {}

        async with self.sessions() as session:
            found = (await session.execute(select(entries).where(entries.c.space == space, entries.c.key.in_(keys), self.living(moment)))).all()

            return {row.key: to_entry(row) for row in found}

    async def drop(self, space: str, key: str) -> bool:
        async def once() -> bool:
            async with self.sessions() as session:
                gone = (await session.execute(delete(entries).where(self.at(space, key)))).rowcount
                await session.commit()

                return gone == 1

        return await under_contention(once)

    async def touch(self, space: str, key: str, expires_at: datetime | None, stale_at: datetime | None, version: int, moment: datetime) -> bool:
        async def once() -> bool:
            async with self.sessions() as session:
                moved = await self.moved(session, space, key, {"expires_at": expires_at, "stale_at": stale_at, "version": version}, self.living(moment))
                await session.commit()

                return moved is not None

        return await under_contention(once)

    async def bump(self, space: str, key: str, amount: int, expires_at: datetime | None, version: int, moment: datetime) -> int | None:
        return await under_contention(lambda: self.counted(space, key, amount, expires_at, version, moment))

    async def counted(self, space: str, key: str, amount: int, expires_at: datetime | None, version: int, moment: datetime) -> int | None:
        """Reads a count and writes it back against the very version it read."""
        # Arithmetic inside a json value is a statement each of these databases spells differently, so the count is read and written back instead.
        # The row is held where the dialect has a lock for it, and the condition on the version is what makes a lost update impossible where it does not: SQLite starts no transaction for a read at all.
        for _ in range(COUNTS):
            async with self.sessions() as session:
                held = (await session.execute(select(entries).where(self.at(space, key)).with_for_update())).one_or_none()
                standing = held is not None and to_entry(held).alive(moment)
                counted = held.value if standing else 0

                if type(counted) is not int or not COUNTER_FLOOR <= counted <= COUNTER_CEILING:
                    return None

                total = counted + amount

                if not COUNTER_FLOOR <= total <= COUNTER_CEILING:
                    return None

                if await self.added_up(session, space, key, held, standing, total, expires_at, version, moment) is not None:
                    await session.commit()

                    return total

                await spread_out(BACKOFF)

        raise CacheError(f"'{space}:{key}' was written by somebody else every one of the {COUNTS} times this tried to count in it")

    async def added_up(self, session, space: str, key: str, held, standing: bool, total: int, expires_at: datetime | None, version: int, moment: datetime) -> bool | None:
        """Writes the count back, and answers nothing at all when another caller wrote that name in between."""
        if held is None:
            return True if await self.started(session, Entry(space=space, key=key, value=total, expires_at=expires_at, created_at=moment, version=version)) else None

        if standing:
            return await self.moved(session, space, key, {"value": total, "version": version}, entries.c.version == held.version, self.living(moment))

        # The instants are taken because the name held nothing, and a counter is asked for a window that starts on the first call.
        return await self.moved(session, space, key, {"value": total, "expires_at": expires_at, "stale_at": None, "created_at": moment, "version": version}, entries.c.version == held.version, self.dead(moment))

    async def clear(self, space: str) -> int:
        async def once() -> int:
            async with self.sessions() as session:
                gone = (await session.execute(delete(entries).where(entries.c.space == space))).rowcount
                await session.commit()

                return gone

        return await under_contention(once)

    async def purge(self, before: datetime, limit: int) -> int:
        async def once() -> int:
            async with self.sessions() as session:
                dead = and_(entries.c.expires_at.is_not(None), entries.c.expires_at <= before)

                # The names are read first and the delete names them by primary key, or the database drives it off the index the condition reads by.
                # That locks a secondary entry and then reaches for the row, while every write locks the row and then reaches for that same entry, which is a deadlock.
                # What has been dead longest goes first, or a batch that always picks the same end leaves the rest lying there for ever.
                taken = [(row.space, row.key) for row in (await session.execute(select(entries.c.space, entries.c.key).where(dead).order_by(entries.c.expires_at, entries.c.space, entries.c.key).limit(limit))).all()]

                if not taken:
                    return 0

                # The state those rows were read for is asserted again by the delete itself, or an entry another caller wrote while this waited on a lock is a living value dropped out from under them.
                gone = (await session.execute(delete(entries).where(tuple_(entries.c.space, entries.c.key).in_(taken), dead))).rowcount
                await session.commit()

                return gone

        return await under_contention(once)

    async def count(self, space: str | None, moment: datetime) -> int:
        wanted = [self.living(moment)]

        if space is not None:
            wanted.append(entries.c.space == space)

        async with self.sessions() as session:
            return await session.scalar(select(func.count()).select_from(entries).where(*wanted))

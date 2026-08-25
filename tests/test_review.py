"""One test per bug a line-by-line reading found, each named after the behaviour and not the fix."""

import ast
import asyncio
import pathlib
import random
import sqlite3
import threading
from datetime import timedelta
from enum import Enum

import pytest
from redis.asyncio import Redis
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

from cachefy.app import Cachefy
from cachefy.clock import now
from cachefy.codec import as_written
from cachefy.entry import MISS, Entry, Missing, minted
from cachefy.errors import CacheError, UnwritableValue
from cachefy.janitor import Janitor
from cachefy.keys import KEY_LIMIT, digested, joined, named, spaced
from cachefy.space import LOCKS, UNANSWERED
from cachefy.store.base import BATCH_LIMIT, COUNTER_CEILING, COUNTER_FLOOR, WHOLE_FLOOR, Store
from cachefy.store.memory import MemoryStore
from cachefy.store.redis import RedisStore, stamp
from cachefy.store.sqlalchemy import COUNTS, REWRITES, SqlAlchemyStore, contended, entries
from cachefy.store.sqlalchemy import metadata as server_metadata
from tests.conftest import REDIS_URL, STORES


def refused(statement: str) -> DBAPIError:
    """Answers what a database says when another process got there first."""
    return DBAPIError(statement, {}, Exception("somebody else got there first"))


def test_a_name_holding_nothing_reads_as_itself():
    """A cache answering `None` for both a missing name and a stored `None` cannot say which it meant."""
    assert repr(MISS) == "MISS"
    assert bool(MISS) is False
    assert isinstance(MISS, Missing)


async def test_a_listener_that_is_being_cancelled_never_swallows_the_cancellation():
    """Swallowing it there would leave a loop nobody can stop."""
    app = Cachefy(MemoryStore())
    users = app.space("users")

    @app.on_miss
    def unhelpful(space, key):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await users.get("42")


async def test_a_full_batch_is_swept_again_at_once_and_never_after_the_interval():
    """A week that was never swept would otherwise take a week of intervals to catch up on."""
    store = MemoryStore()
    await store.setup()
    app = Cachefy(store)
    moment = now()

    for index in range(20):
        await store.write(Entry(space="users", key=f"gone-{index}", value=index, expires_at=moment - timedelta(minutes=1), created_at=moment), moment)

    janitor = Janitor(app, every=timedelta(hours=1), batch=5)
    sweeps = []
    sweeping = janitor.sweep_once

    async def counted() -> int:
        gone = await sweeping()
        sweeps.append(gone)

        if len(sweeps) == 4:
            janitor.stop()

        return gone

    janitor.sweep_once = counted

    async with asyncio.timeout(10):
        await janitor.run()

    assert sweeps == [5, 5, 5, 5], "an interval of an hour never passed between any two of these"


@pytest.mark.skipif("redis" not in STORES, reason="a redis nobody can reach is not a store this suite collects")
async def test_a_client_that_decodes_for_itself_is_refused_where_the_store_is_built():
    """Nothing below could tell: writing would go on working while every read raised."""
    client = Redis.from_url(REDIS_URL, decode_responses=True)

    with pytest.raises(CacheError, match="decode_responses"):
        RedisStore(client)

    await client.aclose()


async def test_a_table_another_process_created_in_the_same_instant_is_not_a_process_that_ends(sql_store, monkeypatch):
    """Asking whether the table is there and creating it are a question and a statement with a gap between them."""
    tries = []
    creating = server_metadata.create_all

    def refuse_the_first(connection, *arguments, **options):
        tries.append(1)

        if len(tries) == 1:
            raise refused("CREATE TABLE")

        return creating(connection, *arguments, **options)

    monkeypatch.setattr(server_metadata, "create_all", refuse_the_first)

    await sql_store.setup()

    assert len(tries) == 2, "the first was refused and the second one settled it"


async def test_a_setup_that_keeps_failing_raises_for_whatever_the_real_reason_was(sql_store, monkeypatch):
    """With the table still missing the second attempt raises, and nothing here reads an error message to decide that."""

    def always_refuse(connection, *arguments, **options):
        raise refused("CREATE TABLE")

    monkeypatch.setattr(server_metadata, "create_all", always_refuse)

    with pytest.raises(DBAPIError):
        await sql_store.setup()


async def test_a_write_that_keeps_finding_the_name_both_free_and_taken_says_so(sql_store, monkeypatch):
    """A store answering both of those at once would hang the call of whoever made it."""
    moment = now()
    passes = []

    async def never_taken(self, session, entry):
        passes.append(1)

        return False

    monkeypatch.setattr(SqlAlchemyStore, "started", never_taken)

    with pytest.raises(CacheError, match=f"{REWRITES} times over"):
        await sql_store.write(Entry(space="users", key="42", value=1, created_at=moment), moment)

    assert len(passes) == REWRITES


async def test_a_name_another_caller_wrote_between_two_statements_is_taken_on_the_pass_after(sql_store, monkeypatch):
    """The update found nothing and the insert was told the name was taken, so what is there is a row the update takes."""
    moment = now()
    starting = SqlAlchemyStore.started
    once = []

    async def somebody_else_wins_first(self, session, entry):
        if once:
            return await starting(self, session, entry)

        once.append(1)
        await session.execute(entries.insert().values(space=entry.space, key=entry.key, value="somebody else", expires_at=None, stale_at=None, created_at=moment, version=1))
        await session.commit()

        return False

    monkeypatch.setattr(SqlAlchemyStore, "started", somebody_else_wins_first)

    written = await sql_store.write(Entry(space="users", key="42", value="mine", created_at=moment), moment)

    assert written.value == "mine"
    assert (await sql_store.read("users", "42", moment)).value == "mine"
    assert (await sql_store.read("users", "42", moment)).version == written.version


def refusing_inserts(times: int, tries: list):
    """Answers an `execute` that tells the first `times` inserts the name was taken."""
    executing = AsyncSession.execute

    async def execute(self, statement, *arguments, **options):
        if isinstance(statement, Insert) and len(tries) < times:
            tries.append(1)

            raise IntegrityError("INSERT", {}, Exception("the name was taken"))

        return await executing(self, statement, *arguments, **options)

    return execute


async def test_a_count_whose_name_another_caller_started_lands_on_the_pass_after(sql_store, monkeypatch):
    """A name nothing has ever held has no row to lock, so the insert is what decides that race."""
    moment = now()
    tries = []

    monkeypatch.setattr(AsyncSession, "execute", refusing_inserts(1, tries))

    assert await sql_store.bump("limits", "paulo", 1, None, minted(), moment) == 1
    assert len(tries) == 1


async def test_a_count_somebody_else_keeps_winning_is_given_up_on_rather_than_asked_for_ever(sql_store, monkeypatch):
    moment = now()
    tries = []

    monkeypatch.setattr(AsyncSession, "execute", refusing_inserts(COUNTS, tries))

    with pytest.raises(CacheError, match=f"{COUNTS} times"):
        await sql_store.bump("limits", "paulo", 1, None, minted(), moment)

    assert len(tries) == COUNTS


@pytest.mark.parametrize("edge", [COUNTER_CEILING, COUNTER_FLOOR])
async def test_a_count_one_step_past_the_range_is_still_a_number_every_store_tells_apart(store, edge):
    """Lua counts in doubles, and one step past a range reaching 2**53 rounds back onto the edge it was leaving."""
    moment = now()
    await store.write(Entry(space="limits", key="paulo", value=edge, created_at=moment), moment)

    assert await store.bump("limits", "paulo", 1 if edge > 0 else -1, None, minted(), moment) is None
    assert (await store.read("limits", "paulo", moment)).value == edge


async def test_a_big_whole_number_is_read_back_as_the_number_that_was_written(store):
    """A column typed `JSON` on SQLite takes numeric affinity and reads 2**64-1 back as 1.8446744073709552e+19."""
    moment = now()
    await store.write(Entry(space="users", key="42", value={"big": 2**64 - 1}, created_at=moment), moment)

    held = (await store.read("users", "42", moment)).value["big"]

    assert held == 2**64 - 1
    assert type(held) is int


async def test_a_value_written_back_over_a_name_that_died_and_came_back_is_refused(store):
    """A version counted from the last time a name held nothing repeats, and a swap aimed at one is a value written over what nobody read."""
    moment = now()
    read = await store.write(Entry(space="users", key="42", value="was", expires_at=moment - timedelta(seconds=1), created_at=moment), moment)

    # The name died and somebody else wrote a value of their own under it, which is what the caller holding that version must never write over.
    await store.write(Entry(space="users", key="42", value="somebody else", created_at=moment), moment)

    assert await store.swap(Entry(space="users", key="42", value="mine", created_at=moment), read.version, moment) is None
    assert (await store.read("users", "42", moment)).value == "somebody else"


async def test_a_name_taken_again_after_a_lease_is_never_let_go_of_by_the_holder_before_it(store):
    """A version counted from the last time a name held nothing repeats, and the holder before would free the lock the holder after is on."""
    moment = now()
    first = await store.add(Entry(space="locks", key="42", value="first", expires_at=moment - timedelta(seconds=1), created_at=moment), moment)
    second = await store.add(Entry(space="locks", key="42", value="second", expires_at=moment + timedelta(minutes=1), created_at=moment), moment)

    assert second is not None, "the lease of the first ran out, so the name was free"

    gone = Entry(space="locks", key="42", value="first", expires_at=moment, created_at=moment)

    assert await store.swap(gone, first.version, moment) is None, "the holder before it does not let go of a name it no longer holds"
    assert (await store.read("locks", "42", moment)).value == "second"


async def test_a_sweep_takes_what_has_been_dead_longest_first(store):
    """A batch that always picks the same end leaves the rest lying there for ever."""
    moment = now()

    # What is written last has been dead longest, or a store answering in the order it was written passes this without ordering anything.
    for index in range(3):
        await store.write(Entry(space="users", key=f"k{index}", value=index, expires_at=moment - timedelta(minutes=8 + index), created_at=moment), moment)

    assert await store.purge(moment, 1) == 1
    assert await store.drop("users", "k2") is False, "the one that had been dead longest is the one that went"
    assert await store.drop("users", "k0") is True
    assert await store.drop("users", "k1") is True


async def test_a_lease_that_is_never_let_go_of_is_refused_where_the_space_is_declared():
    """A name held for ever is one a caller that died never lets go of."""
    app = Cachefy(MemoryStore())

    with pytest.raises(CacheError, match="has to be let go of"):
        app.space("users", lease=None)


async def test_a_caller_waiting_on_a_store_that_went_away_stops_waiting_and_computes():
    """A read that answers 'not there yet' for a store that is gone is a request hanging on a cache for a whole lease."""
    store = MemoryStore()
    await store.setup()
    app = Cachefy(store)
    users = app.space("users", lease=timedelta(hours=1))

    assert await users.take("42", now()) is not None, "somebody else holds the name"

    async def gone(*arguments, **options):
        raise ConnectionError("the store went away")

    store.read = gone

    async with asyncio.timeout(5):
        assert await users.fetch("42", lambda: "computed") == "computed"


async def test_the_caller_that_computed_is_answered_the_value_every_caller_after_it_gets():
    """A tuple comes back as a list, so a producer answering one would hand two shapes out of one call."""
    store = MemoryStore()
    await store.setup()
    app = Cachefy(store)
    users = app.space("users")

    assert await users.fetch("42", lambda: {"tags": ("a", "b")}) == {"tags": ["a", "b"]}
    assert await users.fetch("42", lambda: {"tags": ("a", "b")}) == {"tags": ["a", "b"]}


async def test_a_count_is_answered_for_by_its_lifetime_and_never_by_a_freshness():
    """Nothing ever recomputes a count, so a freshness the space declares is not one a shorter lifetime has to clear."""
    store = MemoryStore()
    await store.setup()
    app = Cachefy(store)
    limits = app.space("limits", ttl=timedelta(hours=1), stale=timedelta(minutes=30))

    assert await limits.incr("paulo", ttl=timedelta(minutes=10)) == 1


def test_what_a_store_that_could_not_answer_means_reads_as_itself():
    assert repr(UNANSWERED) == "UNANSWERED"
    assert UNANSWERED is not None, "a store that could not answer is never a name holding nothing"


def test_the_space_the_locks_live_in_is_not_one_an_application_may_declare():
    app = Cachefy(MemoryStore())

    with pytest.raises(CacheError, match="not a space an application may declare"):
        app.space(LOCKS)


def test_a_memo_keeps_its_own_fields_whatever_the_function_it_wraps_carries():
    """`update_wrapper` carries the wrapped function's own attributes over, and it would write across the memo's."""
    app = Cachefy(MemoryStore())

    async def profile(user_id: int) -> int:
        return user_id

    profile.space = "somebody else's"
    profile.handler = "somebody else's"

    remembered = app.cached("profile")(profile)

    assert remembered.space is app.space_for("profile")
    assert remembered.handler is profile


@pytest.mark.parametrize("batch", [1.5, True, "five", None])
def test_a_batch_is_answered_for_as_a_batch_and_never_as_a_span(batch):
    """Asking `real` of a batch answers a wrong one with a sentence about seconds."""
    app = Cachefy(MemoryStore())

    with pytest.raises(CacheError, match="one statement may name"):
        Janitor(app, batch=batch)


@pytest.mark.parametrize("version", ["1", 1.0, True, None, -1, 2**62])
async def test_a_version_no_entry_could_carry_is_refused_where_it_is_written(version):
    """Left through, one a store cannot compare answers 'somebody wrote in between' rather than saying what was wrong with it."""
    store = MemoryStore()
    await store.setup()
    users = Cachefy(store).space("users")

    with pytest.raises(CacheError, match="a version is the whole number"):
        await users.swap("42", 1, version)


async def test_a_value_a_store_can_no_longer_read_back_is_a_miss_and_never_a_failure(app):
    """A cache holding something nothing can decode must degrade to computing it again, not to failing the request."""
    users = app.space("users")
    await users.set("42", {"name": "paulo"})

    reading = app.store.read

    async def unreadable(*arguments, **options):
        raise ValueError("what is stored there is not something this can read back")

    app.store.read = unreadable
    told = []
    app.on_error(lambda what, failure: told.append(what))

    assert await users.get("42") is MISS
    assert await users.fetch("42", lambda: "computed") == "computed"
    assert told == ["read 'users:42'", "read 'users:42'"]

    app.store.read = reading


async def test_a_producer_that_asks_for_the_name_it_is_computing_is_told_rather_than_left_waiting():
    """It holds that name itself, so waiting for it is waiting out a whole lease for a value nobody else is computing."""
    store = MemoryStore()
    await store.setup()
    app = Cachefy(store)
    users = app.space("users", lease=timedelta(hours=1))

    async def asks_for_itself():
        return await users.fetch("42", lambda: "inner")

    async with asyncio.timeout(5):
        with pytest.raises(CacheError, match="cannot wait for a name it holds itself"):
            await users.fetch("42", asks_for_itself)


async def test_a_producer_may_ask_for_any_other_name(app):
    users = app.space("users")
    posts = app.space("posts")

    async def asks_for_another():
        return await posts.fetch("42", lambda: "a post")

    assert await users.fetch("42", asks_for_another) == "a post"
    assert await users.fetch("43", lambda: "another") == "another"


async def test_the_name_a_producer_was_computing_is_free_again_once_it_answered(app):
    users = app.space("users")

    assert await users.fetch("42", lambda: "first") == "first"
    await users.drop("42")

    assert await users.fetch("42", lambda: "second") == "second", "nothing is left marked as being computed"


async def test_a_task_a_producer_spawned_is_computing_what_its_caller_is():
    """A task carries the context it was spawned in, so a name held by the caller is one it must not wait on either."""
    store = MemoryStore()
    await store.setup()
    app = Cachefy(store)
    users = app.space("users", lease=timedelta(hours=1))

    async def spawns():
        return await asyncio.create_task(users.fetch("42", lambda: "inner"))

    async with asyncio.timeout(5):
        with pytest.raises(CacheError, match="cannot wait for a name it holds itself"):
            await users.fetch("42", spawns)


@pytest.mark.parametrize("producer", ["not a callable", 7, None, {"a": 1}])
async def test_something_that_could_never_be_called_is_refused_where_it_is_written(app, producer):
    users = app.space("users")

    with pytest.raises(CacheError, match="something that can be called"):
        await users.fetch("42", producer)


async def test_a_refusal_from_inside_a_producer_is_never_answered_around_with_a_stale_value(app):
    """Serving what happened to be there over a caller mistake is a bug nothing would ever report."""
    users = app.space("users", ttl=timedelta(minutes=5), stale=timedelta(milliseconds=20))
    await users.fetch("42", lambda: "old")

    await asyncio.sleep(0.06)

    async def asks_wrongly():
        return await users.get("")

    with pytest.raises(CacheError):
        await users.fetch("42", asks_wrongly)

    assert await users.get("42") == "old", "and what was there is still there"


@pytest.mark.parametrize("batch", [0, -1, BATCH_LIMIT + 1, 100000])
def test_a_sweep_no_statement_could_name_is_refused_where_it_is_written(app, batch):
    """PostgreSQL refuses a statement naming about twenty thousand names while SQLite and MySQL take it."""
    with pytest.raises(CacheError, match="one statement may name"):
        Janitor(app, batch=batch)


def test_the_batch_a_sweep_is_built_with_by_default_is_one_every_database_takes(app):
    assert Janitor(app).batch <= BATCH_LIMIT


async def test_one_name_asked_for_twice_is_one_lookup_and_one_telling(app):
    """The hooks are told about every name that held something, and a name listed twice is still one name."""
    users = app.space("users")
    told = []

    app.on_hit(lambda space, key: told.append(("hit", key)))
    app.on_miss(lambda space, key: told.append(("miss", key)))

    await users.set("there", 1)

    assert await users.get_many(["there", "there", "nothing", "nothing"]) == {"there": 1}
    assert told == [("hit", "there"), ("miss", "nothing")]


async def test_a_store_that_forgets_a_method_of_the_contract_is_refused_where_it_is_built():
    """Nothing below could tell, and a cache missing a call is one that raises the first time anything reaches it."""

    class Forgetful(Store):
        async def setup(self) -> None:
            return None

    with pytest.raises(TypeError, match="abstract"):
        Forgetful()


def test_the_contract_is_the_twelve_methods_the_documentation_names():
    """A method added or dropped without the prose moving is a store somebody writes against the wrong shape."""
    assert sorted(Store.__abstractmethods__) == ["add", "bump", "clear", "count", "drop", "purge", "read", "read_many", "setup", "swap", "touch", "write"]
    assert "twelve" in pathlib.Path("docs/stores.md").read_text()


async def test_a_name_a_caller_let_go_of_is_a_dead_row_the_sweep_collects(app):
    """Letting a name go writes one that has already died rather than taking it away, so a cache that misses often leaves them behind."""
    users = app.space("users")

    assert await users.fetch("42", lambda: "computed") == "computed"
    assert await Janitor(app).sweep_once() == 1, "the name that caller held is what the sweep found"
    assert await Janitor(app).sweep_once() == 0
    assert await users.get("42") == "computed", "and the value it computed is untouched"


async def test_the_name_a_producer_was_computing_is_free_again_when_it_raised(app):
    """A mark left behind would refuse every later call of that name as one asking for itself."""
    users = app.space("users")

    async def broken():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await users.fetch("42", broken)

    assert await users.fetch("42", lambda: "computed") == "computed"


@pytest.mark.skipif("redis" not in STORES, reason="a redis nobody can reach is not a store this suite collects")
async def test_redis_never_drops_a_hash_before_the_instant_its_field_names():
    """Redis counts an expiry in milliseconds, and rounding one down let it answer a miss up to 999 microseconds before every other store did."""
    client = Redis.from_url(REDIS_URL)
    await client.flushdb()
    store = RedisStore(client)
    await store.setup()

    moment = now()

    for remainder in (1, 500, 999):
        dies = (moment + timedelta(hours=1)).replace(microsecond=(moment.microsecond // 1000) * 1000 + remainder)
        await store.write(Entry(space="s", key=f"k{remainder}", value=1, expires_at=dies, created_at=moment), moment)

        told = int(stamp(dies))
        reclaimed = await client.pexpiretime(f"cachefy:entry:s:k{remainder}") * 1000

        assert reclaimed >= told, f"redis gives the memory back {told - reclaimed}us before the field says the entry is gone"

    await client.aclose()


@pytest.mark.skipif("redis" not in STORES, reason="a redis nobody can reach is not a store this suite collects")
async def test_the_scripts_are_there_again_after_a_restart_took_them_away():
    """A restart empties the script cache, and a store that never registered them again would raise on every call from then on."""
    client = Redis.from_url(REDIS_URL)
    await client.flushdb()
    store = RedisStore(client)
    await store.setup()

    moment = now()
    await store.write(Entry(space="s", key="before", value=1, created_at=moment), moment)
    await client.script_flush()

    await store.write(Entry(space="s", key="after", value=2, created_at=moment), moment)

    assert (await store.read("s", "after", moment)).value == 2
    assert await store.bump("s", "hits", 1, None, minted(), moment) == 1
    assert await store.purge(moment, 10) == 0

    await client.aclose()


@pytest.mark.skipif("redis" not in STORES, reason="a redis nobody can reach is not a store this suite collects")
async def test_redis_is_left_holding_nothing_no_call_could_ever_remove():
    """A listing with no hash, a hash with no listing or a space listed after it was emptied is a leak nothing would report."""
    client = Redis.from_url(REDIS_URL)
    await client.flushdb()
    store = RedisStore(client)
    await store.setup()

    dice = random.Random(0)
    base = now()
    spaces, keys, lives = ("users", "posts", "limits"), ("42", "Bob", "café", "grüßen 😀"), (None, -300, 3600)

    for step in range(600):
        moment = base + timedelta(seconds=step)
        space, key = dice.choice(spaces), dice.choice(keys)
        dies = dice.choice(lives)
        entry = Entry(space=space, key=key, value=step, expires_at=None if dies is None else base + timedelta(seconds=dies), created_at=moment)
        choice = dice.choice(("write", "write", "add", "swap", "drop", "touch", "bump", "purge", "clear"))

        if choice == "write":
            await store.write(entry, moment)
        elif choice == "add":
            await store.add(entry, moment)
        elif choice == "swap":
            await store.swap(entry, minted(), moment)
        elif choice == "drop":
            await store.drop(space, key)
        elif choice == "touch":
            await store.touch(space, key, entry.expires_at, None, minted(), moment)
        elif choice == "bump":
            await store.bump(space, key, 1, entry.expires_at, minted(), moment)
        elif choice == "purge":
            await store.purge(moment, dice.randint(1, 5))
        else:
            await store.clear(space)

    listed = {space: {name.decode() for name in await client.smembers(f"cachefy:names:{space}")} for space in spaces}
    named = {f"{space}:{key}" for space, held in listed.items() for key in held}
    hashes = {name.decode().removeprefix("cachefy:entry:") async for name in client.scan_iter(match="cachefy:entry:*")}
    dying = {name.decode() for name in await client.zrange("cachefy:dying", 0, -1)}

    assert hashes <= named, "a hash nothing lists is one no clear and no sweep would ever find"
    assert dying <= named, "and so is an instant to die at"
    assert {name.decode() for name in await client.smembers("cachefy:spaces")} == {space for space, held in listed.items() if held}

    for space in spaces:
        await store.clear(space)

    assert [name async for name in client.scan_iter(match="cachefy:*")] == [], "clearing every space leaves the database as it was found"

    await client.aclose()


@pytest.mark.parametrize("keys", [[["a"]], [{"a"}], [{"a": 1}]])
async def test_a_name_nothing_can_hash_is_refused_by_this_and_never_by_python(app, keys):
    """Reading many names deduped before it validated, so a key nothing can hash was refused by a `TypeError` that `except CacheError` never catches."""
    users = app.space("users")

    with pytest.raises(CacheError, match="what tells one entry from another is text"):
        await users.get_many(keys)


async def test_a_name_let_go_of_carries_a_version_of_its_own(app):
    """No two writes of one name carry the same version, and letting a name go is a write like any other."""
    users = app.space("users")

    moment = now()
    holder = await users.take("42", moment)
    taken = holder.version

    await users.freed(holder, now())

    assert await app.store.read(LOCKS, holder.key, now()) is None, "the name it held is one nothing holds now"
    assert holder.version != taken, "and the write that let it go was not the one that took it"


async def test_a_caller_can_never_let_go_of_a_name_it_no_longer_holds(app):
    """A holder whose lease ran out must not take the name away from the holder after it."""
    users = app.space("users", lease=timedelta(hours=1))
    moment = now()

    # The lease of the first is brought forward rather than waited out, so what this asks about is the holder and never how fast the machine is.
    first = Entry(space=LOCKS, key=digested(joined("users", "42")), expires_at=moment - timedelta(seconds=1), created_at=moment)
    await app.store.write(first, moment)

    second = await users.take("42", now())

    assert second is not None, "the lease of the first ran out, so the name was free"

    await users.freed(first, now())

    assert await app.store.read(LOCKS, second.key, now()) is not None, "the holder before it never let go of the one holding it now"


def test_the_memory_store_changes_an_entry_without_ever_awaiting():
    """Nothing can interleave inside a body that never awaits, which is why this store holds no lock — one that awaited would need one."""
    tree = ast.parse(pathlib.Path("src/cachefy/store/memory.py").read_text())

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue

        awaits = [ast.unparse(found) for found in ast.walk(node) if isinstance(found, (ast.Await, ast.AsyncWith, ast.AsyncFor))]

        assert awaits == [], f"MemoryStore.{node.name} awaits mid-body, so two callers can interleave inside it and it needs a lock again"


class Renamed(str, Enum):
    """A space named the way a lot of code names things, whose text and whose rendering are not the same."""

    USERS = "users"


class Lying(str):
    def __str__(self) -> str:
        return "somebody else"


@pytest.mark.parametrize("space", [Renamed.USERS, Lying("users")])
async def test_a_space_named_by_something_that_renders_as_something_else_is_still_that_space(app, space):
    """Redis built one key by interpolating the name and another by handing it over, so a write landed and the read of that very name answered nothing."""
    named = app.space(space)

    assert named.name == "users" and type(named.name) is str, "what travels is the text and never the object"

    await named.set("42", "written")

    assert await named.get("42") == "written"
    assert await app.store.read("users", "42", now()) is not None, "and the store holds it under the text"


@pytest.mark.parametrize("key", [Renamed.USERS, Lying("42")])
async def test_a_key_that_renders_as_something_else_is_still_that_key(app, key):
    users = app.space("users")

    await users.set(key, "written")

    assert await users.get(key) == "written"
    assert await users.get(str.__str__(key)) == "written", "one name spelled two ways is one entry"


async def test_a_name_that_renders_as_something_else_is_held_as_the_name_it_is(app):
    """The name a caller holds while it computes is drawn from the space and the key, so those have to be the text too."""
    users = app.space("users", ttl=timedelta(minutes=5))
    calls = []

    async def load():
        calls.append(1)

        return "computed"

    assert await asyncio.gather(users.fetch(Lying("42"), load), users.fetch("42", load)) == ["computed", "computed"]
    assert calls == [1], "both spellings are one name, so one caller computed it"


async def test_what_reaches_a_store_is_settled_before_it_gets_there(app):
    """A store validates nothing, so what a caller hands in has to have become plain text and plain json on the way."""
    written = []
    writing = app.store.write

    async def remember(entry, moment):
        written.append(entry)

        return await writing(entry, moment)

    app.store.write = remember

    class Named(str, Enum):
        USERS = "users"

    class Held(dict):
        pass

    try:
        await app.space(Named.USERS, ttl=timedelta(minutes=5)).set(Named.USERS, Held(a=(1, 2)))
    finally:
        app.store.write = writing

    entry = written[-1]

    assert type(entry.space) is str and type(entry.key) is str, "a store was handed a name that renders as something else"
    assert type(entry.value) is dict and entry.value == {"a": [1, 2]}, "a store was handed a value it would have had to settle itself"


async def test_every_call_of_a_memoized_method_carries_the_instance_it_was_reached_on(app):
    """A memo was an attribute and not a method, so the first argument of every call was bound to `self` and the last one was missing."""
    computed = []

    class Accounts:
        def __init__(self, tenant: str) -> None:
            self.tenant = tenant

        @app.cached("accounts", ttl=timedelta(minutes=5), key=lambda self, account_id: f"{self.tenant}:{account_id}")
        async def profile(self, account_id: int) -> dict:
            computed.append((self.tenant, account_id))

            return {"id": account_id, "tenant": self.tenant}

    holder = Accounts("one")

    assert await holder.profile(7) == {"id": 7, "tenant": "one"}
    assert holder.profile.key_for(7) == "one:7"

    assert await holder.profile.refresh(7) == {"id": 7, "tenant": "one"}
    assert computed == [("one", 7), ("one", 7)], "refreshing through the instance never carried it"

    assert await holder.profile.invalidate(7) is True, "invalidating through the instance named something else"
    assert await holder.profile(7) == {"id": 7, "tenant": "one"}
    assert len(computed) == 3

    assert await holder.profile.clear() >= 1
    assert type(Accounts.profile).__name__ == "Memo", "reached on the class it is the memo itself"


async def test_every_name_a_read_of_many_asks_for_is_settled_like_any_other(app):
    """Reading many names answered for each one and threw the settled name away, so the store and the listeners were handed the object a caller passed."""

    class Lying(str):
        def __str__(self) -> str:
            return "somebody else"

    told, asked = [], []
    reading = app.store.read_many

    async def watched(space, keys, moment):
        asked.extend(type(key) for key in keys)

        return await reading(space, keys, moment)

    app.on_hit(lambda space, key: told.append(type(key)))
    app.store.read_many = watched

    users = app.space("users", ttl=timedelta(minutes=5))

    try:
        await users.set("42", "value")
        assert await users.get_many([Lying("42")]) == {"42": "value"}
    finally:
        app.store.read_many = reading

    assert asked == [str], "a store was handed a name that renders as something else"
    assert told == [str], "a listener was told a name that renders as something else"


def test_a_decorator_put_under_one_that_hides_the_function_is_refused_where_it_is_written(app):
    """A classmethod is not callable, so reading its signature raised a TypeError where the class is written that `except CacheError` never catches."""
    with pytest.raises(CacheError, match="rather than something that can be called"):

        class Reports:
            @app.cached("reports", ttl=timedelta(minutes=5))
            @classmethod
            async def totals(cls, month: int) -> str:
                return f"{month}"


async def test_a_memoized_classmethod_is_written_with_the_class_decorator_on_the_outside(app):
    """The order the refusal points at has to be one that really works, and it is the one an application writes."""
    computed = []

    class Reports:
        @classmethod
        @app.cached("totals", ttl=timedelta(minutes=5), key=lambda cls, month: f"{cls.__name__}:{month}")
        async def totals(cls, month: int) -> str:
            computed.append(month)

            return f"{cls.__name__}-{month}"

    assert await Reports.totals(3) == "Reports-3"
    assert await Reports.totals(3) == "Reports-3"
    assert await Reports().totals(3) == "Reports-3", "reached through an instance it names the same call"

    assert computed == [3], "the class it was reached on was not carried, or the answer was never kept"


def test_a_value_goes_stale_at_the_instant_it_says_and_never_a_moment_after():
    """Nothing pinned this edge: read a microsecond late it is the twin of a sweep stopping short, and one read at that instant is a refresh that never happened."""
    moment = now()
    goes = moment + timedelta(minutes=1)
    entry = Entry(space="users", key="42", value=1, stale_at=goes, created_at=moment)

    assert not entry.stale(goes - timedelta(microseconds=1))
    assert entry.stale(goes), "a value that says it is stale now was read as fresh"
    assert entry.stale(goes + timedelta(microseconds=1))


@pytest.mark.skipif("redis" not in STORES, reason="redis is not answering")
async def test_a_redis_store_is_whole_as_soon_as_it_is_built():
    """Registering a script asks redis nothing, so holding it back until setup left a store that read from the server and dropped every write."""
    client = Redis.from_url(REDIS_URL)
    store = RedisStore(client)
    app = Cachefy(store)
    failures = []
    app.on_error(lambda what, failure: failures.append(what))

    users = app.space("built", ttl=timedelta(minutes=5))

    try:
        assert await users.set("42", "value") is not None, "a write was dropped by a store nobody had set up"
        assert await users.get("42") == "value"
        assert await users.incr("counter") == 1
    finally:
        await store.clear("built")
        await client.aclose()

    assert failures == [], f"a store that was only built could not answer: {failures}"


@pytest.mark.skipif("redis" not in STORES, reason="redis is not answering")
def test_a_synchronous_caller_over_one_kept_loop_hits_a_store_with_a_connection_pool():
    """A loop built per call closes the pool the call before it opened, so every read failed into a miss and the producer ran on every request."""
    loop = asyncio.new_event_loop()
    running = threading.Thread(target=loop.run_forever, daemon=True)
    running.start()

    def waited(work):
        return asyncio.run_coroutine_threadsafe(work, loop).result(timeout=30)

    client = Redis.from_url(REDIS_URL)
    app = Cachefy(RedisStore(client))
    accounts = app.space("bridged", ttl=timedelta(minutes=5))

    computed, failures = [], []
    app.on_error(lambda what, failure: failures.append(what))

    def read_account(account_id: int) -> dict:
        return waited(accounts.fetch(str(account_id), lambda: loaded(account_id)))

    def loaded(account_id: int) -> dict:
        computed.append(account_id)

        return {"id": account_id}

    try:
        waited(app.setup())
        waited(accounts.clear())

        for _ in range(5):
            assert read_account(7) == {"id": 7}

        assert computed == [7], "a cache reached from a synchronous caller never hit, so the producer ran on every call"
        assert failures == [], f"the store could not be reached from the kept loop: {failures}"

        answers = []
        threads = [threading.Thread(target=lambda: answers.append(read_account(9))) for _ in range(12)]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join(timeout=30)

        assert answers == [{"id": 9}] * 12
        assert computed.count(9) == 1, "twelve web threads at one cold name computed it more than once"
    finally:
        waited(accounts.clear())
        waited(client.aclose())
        loop.call_soon_threadsafe(loop.stop)
        running.join(timeout=30)
        loop.close()


def test_a_write_sqlite_refused_because_another_connection_holds_the_lock_is_asked_again(tmp_path):
    """SQLite reports a busy database as a code of its own, and the retry only knew the numbers MySQL puts in `args`, so it was let through as a failure and the write was dropped."""
    path = tmp_path / "locked.db"
    holder = sqlite3.connect(path, timeout=0)
    holder.execute("CREATE TABLE thing (id INTEGER)")
    holder.commit()
    holder.execute("BEGIN EXCLUSIVE")

    other = sqlite3.connect(path, timeout=0)

    try:
        with pytest.raises(sqlite3.OperationalError) as refused:
            other.execute("INSERT INTO thing VALUES (1)")
    finally:
        holder.rollback()
        holder.close()
        other.close()

    assert refused.value.sqlite_errorcode == sqlite3.SQLITE_BUSY

    assert contended(DBAPIError("INSERT", {}, refused.value)) is True, "a write the database asked to be made again was let through as a failure"


def test_a_write_sqlite_refused_for_any_other_reason_is_never_asked_again():
    """Asking again for something that will never succeed is a call that hangs for eight tries and then raises anyway."""
    broken = sqlite3.connect(":memory:")

    try:
        with pytest.raises(sqlite3.OperationalError) as refused:
            broken.execute("SELECT * FROM nothing")
    finally:
        broken.close()

    assert contended(DBAPIError("SELECT", {}, refused.value)) is False


@pytest.mark.parametrize("code, again", [(1213, True), (1205, True), (1062, False)])
def test_the_numbers_mysql_reports_are_still_read(code, again):
    """SQLite is asked for first, and a driver that reports neither must not lose the codes InnoDB does."""

    class Reported(Exception):
        pass

    assert contended(DBAPIError("UPDATE", {}, Reported(code, "reported"))) is again


async def test_a_generator_handed_straight_to_fetch_is_refused_like_one_handed_to_the_decorator(app):
    """The decorator refused it and fetch did not, so the same mistake made through the primary call answered a generator object and cached nothing, on every call."""
    users = app.space("generators", ttl=timedelta(minutes=5))

    def generating():
        yield 1

    async def generating_slowly():
        yield 1

    for producer in (generating, generating_slowly):
        with pytest.raises(CacheError, match="runs none of what it was written to do"):
            await users.fetch("42", producer)

    assert await users.get("42") is MISS, "a generator was kept under the name"


async def test_a_callable_object_that_generates_is_refused_by_what_it_would_answer(app):
    """An instance has no code of its own, so what is read is the code of its `__call__`."""
    users = app.space("callables", ttl=timedelta(minutes=5))

    class Generating:
        def __call__(self):
            yield 1

    with pytest.raises(CacheError, match="runs none of what it was written to do"):
        await users.fetch("42", Generating())


async def test_a_producer_that_computes_a_value_is_still_taken(app):
    """The guard has to refuse a generator and nothing else, or every ordinary producer is refused with it."""
    users = app.space("ordinary", ttl=timedelta(minutes=5))

    class Callable:
        async def __call__(self):
            return "from an object"

    assert await users.fetch("plain", lambda: "from a lambda") == "from a lambda"
    assert await users.fetch("object", Callable()) == "from an object"


class Shrinking(str):
    """Text that answers a length it does not have, which a `str` subclass is free to do."""

    def __len__(self) -> int:
        return 1


class Spotless(str):
    """Text that answers that it holds nothing it does not want to admit to."""

    def __contains__(self, what) -> bool:
        return False


class Standing(str):
    """Text that answers that it is there when it is not."""

    def __bool__(self) -> bool:
        return True


@pytest.mark.parametrize(
    "name, refused",
    [
        (Shrinking("k" * (KEY_LIMIT + 45)), "characters and a store keeps"),
        (Spotless("a\x00b"), "nul byte"),
        (Standing(""), "is empty"),
    ],
)
@pytest.mark.parametrize("judge", [named, spaced])
def test_a_name_is_judged_by_the_text_a_store_will_hold_and_never_by_the_object(judge, name, refused):
    """Every guard measured the object a caller passed, so a subclass answering its own length handed the store three hundred characters through a limit of 255."""
    with pytest.raises(CacheError, match=refused):
        judge(name)


def test_a_space_that_hides_the_colon_it_holds_is_still_refused():
    """It is what joins a space to a key, so one that slips through lets two different pairs spell the same entry."""
    with pytest.raises(CacheError, match="colon"):
        spaced(Spotless("users:live"))


async def test_nothing_a_lying_name_says_reaches_a_store(app):
    """What lands is `str.__str__` of the object, so a guard that believed the object judged something that never arrived."""
    users = app.space("users", ttl=timedelta(minutes=5))

    for name in (Shrinking("k" * (KEY_LIMIT + 45)), Spotless("a\x00b"), Standing("")):
        with pytest.raises(CacheError):
            await users.set(name, 1)

    assert await users.count() == 0, "a name no store could hold was written anyway"


class Hiding(dict):
    """A mapping that answers no values while holding every one of them."""

    def values(self):
        return []


@pytest.mark.parametrize("value", [Hiding(n=10**40), {"deep": [Hiding(n=10**40)]}, [Hiding(n=WHOLE_FLOOR - 1)]])
def test_a_whole_number_hidden_from_the_walk_is_still_refused(value):
    """The walk read the object and the encoder wrote the storage, so a mapping answering its own `values()` slipped a number past that MySQL alone reads back as another."""
    with pytest.raises(UnwritableValue, match="past the whole numbers"):
        as_written(value, "the value")


async def test_nothing_a_hiding_value_carries_reaches_a_store(app):
    """What a store is handed is what json wrote, so the guard has to have measured that and not what the object answered."""
    users = app.space("hiding", ttl=timedelta(minutes=5))

    with pytest.raises(UnwritableValue):
        await users.set("42", Hiding(n=10**40))

    assert await users.get("42") is MISS
    assert await users.count() == 0


def test_what_json_really_wrote_is_what_comes_back():
    """A container that hides part of itself is written as it answered, and the value read back is that same thing."""
    assert as_written(Hiding(n=1), "the value") == {"n": 1}, "the encoder writes the storage, so a hidden number is still written"


class Understating(timedelta):
    """A span that answers a minute and holds nine hundred million days."""

    def total_seconds(self) -> float:
        return 60.0


class Understated(int):
    """A number that compares small, converts small and spells small while holding none of it."""

    def __le__(self, other) -> bool:
        return True

    def __ge__(self, other) -> bool:
        return True

    def __lt__(self, other) -> bool:
        return True

    def __gt__(self, other) -> bool:
        return True

    def __int__(self) -> int:
        return 1

    def __str__(self) -> str:
        return "1"


async def test_a_span_is_measured_by_what_it_holds_and_never_by_what_it_answers(app):
    """The guard read `total_seconds` and the arithmetic read the fields behind it, so a subclass that understated itself raised OverflowError at the caller, outside the family."""
    users = app.space("spans", ttl=timedelta(minutes=5))
    lying = Understating(days=999999999)

    with pytest.raises(CacheError, match="left between now and the last instant"):
        app.space("declared-lying", ttl=lying)

    for call in (users.set("42", 1, ttl=lying), users.touch("42", ttl=lying), users.incr("c", ttl=lying), users.fetch("f", lambda: 1, ttl=lying)):
        with pytest.raises(CacheError):
            await call


async def test_a_counter_step_is_measured_by_the_number_it_holds(app):
    """It compared as small and would have been handed to the store as itself, where a count past the range is one no store adds up exactly."""
    counts = app.space("steps", ttl=timedelta(minutes=5))

    with pytest.raises(CacheError, match="a count runs from"):
        await counts.incr("42", Understated(COUNTER_CEILING * 4))

    assert await counts.get("42") is MISS


async def test_a_version_is_measured_by_the_number_it_holds(app):
    """A version a store cannot compare answers 'somebody wrote in between', which tells a caller its value was overtaken when it never was."""
    users = app.space("versions", ttl=timedelta(minutes=5))
    await users.set("42", "first")

    with pytest.raises(CacheError, match="a version is the whole number"):
        await users.swap("42", "second", Understated(2**62 + 1))

    assert await users.get("42") == "first"


async def test_what_reaches_a_store_is_the_number_and_never_the_object(app):
    """A store spells a version out to compare it, so an object that spells itself as something else is compared as that instead."""
    users = app.space("settled-numbers", ttl=timedelta(minutes=5))
    held = await users.set("42", "first")

    swapped, counted = [], []
    swapping, bumping = app.store.swap, app.store.bump

    async def watched_swap(entry, version, moment):
        swapped.append(type(version))

        return await swapping(entry, version, moment)

    async def watched_bump(space, key, amount, expires_at, version, moment):
        counted.append(type(amount))

        return await bumping(space, key, amount, expires_at, version, moment)

    app.store.swap, app.store.bump = watched_swap, watched_bump

    try:
        assert await users.swap("42", "second", Understated(held.version)) is not None, "a version that spells itself as something else was compared as that"
        assert await users.incr("c", Understated(5)) == 5
    finally:
        app.store.swap, app.store.bump = swapping, bumping

    assert swapped == [int], "the store was handed the object rather than the number it holds"
    assert counted == [int]

# 🗄️ Stores

Where the entries live. Every store answers the same contract, and one suite is run against all of
them to keep that true.

| Store | Right for | Wrong for |
| --- | --- | --- |
| `RedisStore` | almost every cache: reads and writes are both one step on the server | a setup where Redis is one more thing to run |
| `SqlAlchemyStore` | keeping the cache in the database you already have | a write-heavy cache, where a write costs a statement and a commit rather than one step |
| `MemoryStore` | tests, and a single process that shares nothing | two processes |

## 🧾 Versions

The suite runs against **Redis 7, MySQL 8.4 and PostgreSQL 16** on every push, and against whichever
SQLite the interpreter was built with.

Older releases of each are very likely to work, because nothing here reaches for anything recent — but
they are not run, so they are not promised. If you need one of them, run the suite against it.

## 🟥 RedisStore

```python
from redis.asyncio import Redis

from cachefy.store.redis import RedisStore

store = RedisStore(Redis.from_url("redis://127.0.0.1:6379/0", socket_timeout=1, socket_connect_timeout=1))
```

Every key it owns starts with `cachefy`, so it shares a database with an application without ever
meeting it. Pass `prefix=` to rename them all at once.

| Key | Holds |
| --- | --- |
| `cachefy:entry:{space}:{key}` | the entry itself, as a hash |
| `cachefy:names:{space}` | the keys one space holds, so clearing it is not a scan |
| `cachefy:dying` | every entry scored by when it dies, so a sweep is a range and not a scan |
| `cachefy:spaces` | which spaces exist, so counting the whole cache is not a scan |

Every mutation is Lua, so each of them is one atomic step on the server. Nothing here reads, decides and
then writes over three round trips with the world between them.

Things that are the way they are for a reason:

- **The expiry Redis is given is what gives the memory back, and never what says an entry is dead.** Redis runs its own clock and this library decides every instant off the caller's, so what a read answers by is the field on the hash. The expiry beside it is what keeps a cache from growing until somebody sweeps.
- **A client built with `decode_responses` is refused where the store is built.** This store reads what Redis answers as bytes, and that setting is one an application sharing its client very often has. Nothing below could tell: writing would go on working while every read raised.
- **One instance, never Redis Cluster.** Every script builds the keys it touches out of `ARGV`, because a name is joined from two halves the caller gives separately. A replica for failover is fine and sharding is not. When one Redis is not enough, shard where the data already divides: one application and one Redis per tenant.
- **A space may not hold a colon**, because that is what joins the two halves of a name. A space called `user` holding `a:b` and one called `user:a` holding `b` would otherwise spell the same key.
- **A version is compared as the text it was written down as** and never as a number, so nothing about it has to fit what a Lua double holds exactly.
- **Counting the depth walks what the spaces listed.** Redis has no index over a hash, so it is a number for an operator and never for a hot path.

## 🐘 SqlAlchemyStore

```python
from sqlalchemy.ext.asyncio import create_async_engine

from cachefy.store.sqlalchemy import SqlAlchemyStore

store = SqlAlchemyStore(create_async_engine(url, pool_pre_ping=True))
```

One table, `cachefy_entry`, under metadata of its own, so building it never touches a table of the
application around it.

| Column | Holds |
| --- | --- |
| `space`, `key` | the name, which is the primary key and the whole identity |
| `value` | the value, as json |
| `expires_at`, `stale_at` | when it dies and when it stops being fresh |
| `created_at` | when the value was worked out |
| `version` | drawn per write, and what a value written back is conditional on |

One index carries the sweep: `cachefy_entry_dying` on `expires_at`.

Things that are the way they are for a reason:

- **The name is the identity and there is no surrogate id anywhere.** What tells one entry from another is what a caller asks for it by, so a second name for the same row would be a whole index carried for nothing.
- **A value of `None` is written down as the json null it is.** Left to a nullable column, a function that legitimately answers nothing would be read back as no entry at all, and its answer computed again on every single call while the cache looked like it was working.
- **On SQLite the value column is declared as text.** A column typed `JSON` there takes numeric affinity, which reads `2**64-1` back as `1.8446744073709552e+19` while every other store reads back the number that was written.
- **A datetime keeps microseconds on MySQL.** Without the precision MySQL rounds one, and an entry dying at `10:00:00.9` would be stored dying at `10:00:01` — a second of life nobody granted it.
- **The two columns that name an entry are compared code point by code point.** MySQL builds one under a collation that folds case and accents away unless it is told otherwise, and behind the primary key that made `user:Bob` and `user:bob` one entry.
- **A sweep takes what has been dead longest first.** A batch that always picked the same end would leave the rest lying there for ever.
- **A count is read and written back against the very version it read.** Arithmetic inside a json value is a statement each of these databases spells differently, and pysqlite starts no transaction for a read at all — so a lost update is stopped by the condition and not by a lock the dialect may not have.
- **No write reads the row back.** A version is one the entry already carries, so a write is one statement and a commit rather than three round trips.

**A production engine needs `pool_pre_ping`, a timeout, and a pool sized for the concurrency.** A
connection whose network went away otherwise leaves a request waiting on a cache, and a pool that ran
out leaves it waiting the thirty seconds SQLAlchemy defaults to — see [Resilience](resilience.md).

**A write refused because another connection holds the lock is asked again.** SQLite reports that
through a code of its own, and in WAL it answers the moment a second writer tries to take the write
lock rather than waiting the busy timeout out — so the retry reads that code and asks again, spread so
the refused callers do not come back in lockstep.

**Size the pool for the concurrency you actually apply.** Callers past the pool queue behind it, and on
SQLite each one may be waiting out a file lock while it holds a connection.

**SQLite across processes needs WAL and a busy timeout:**

```python
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

engine = create_async_engine("sqlite+aiosqlite:///cache.db", connect_args={"timeout": 30})
event.listen(engine.sync_engine, "connect", lambda connection, record: connection.execute("PRAGMA journal_mode=WAL"))
```

**Build the table before the fleet comes up.** Several interpreters running the DDL of a fresh SQLite
file in the same instant is a lock none of them can wait out, which is what a deploy step is for.

## 🧠 MemoryStore

```python
from cachefy.store.memory import MemoryStore

store = MemoryStore()
```

The whole library minus anything shared. It keeps a copy of every value both ways, because a caller
that goes on changing what it wrote must never change the entry, and a value handed out is one many
callers hold at once.

It holds no lock of its own, because every method changes an entry without awaiting — and nothing can
interleave inside a body the event loop cannot switch inside.

It is the store the whole library is defined by: every other one is compared against it, operation by
operation, on every push.

## 🧩 Writing your own

Subclass `Store` and answer its twelve methods. One rule runs through all of them:

> **Every method that changes an entry is conditional on the state it was in.**

That is what makes two callers safe without a lock anywhere.

A store never works a version out. It writes down the one the entry carries, and `swap` compares what
is stored to the one the caller read — which is what lets every store agree without three separate
implementations of one rule.

An entry reaching a store is already settled: the space and the key are plain strings, and the value is
something json writes down, refused before it ever got this far. A store validates none of it. That is
why a value read back off any store is the value that was written, and why a name written under a
`str` subclass that renders as something else is still found by the text it holds.

| Method | Conditional on |
| --- | --- |
| `setup` | — builds what the store needs, and does nothing when it is already there |
| `read` / `read_many` | the entry is there and has not died |
| `write` | — replaces whatever was there |
| `add` | the name is free or what holds it has died |
| `swap` | the name still holds the living version the caller read |
| `drop` | — takes the name away, and says whether it held anything |
| `touch` | the entry is alive, and it writes the version it was handed |
| `bump` | the name holds a count, in one atomic step, under the version it was handed |
| `clear` | — drops everything one space holds |
| `purge` | the entry was already dead, longest dead first |
| `count` | — how much is alive |

Add yours to the fixture in `tests/conftest.py` and it inherits the whole suite: the contract, the
script every store is compared on, and the sweep that cuts one round trip at a time.

# Cachefy

Standing context for anybody — human or model — working on this repository. Read it before writing a
line, and keep it true when the code moves.

---

## 1. What this is

An asynchronous cache for Python. Pure `asyncio`, no background process of its own beyond an optional
sweep, no coordination between the processes that share it.

Everything reduces to a single row: **a name that holds a value until an instant.** A read is that row
before that instant. A write replaces it. A miss is that row absent or past its instant. Memoizing a
function, holding a name so exactly one caller computes, and counting a rate limit are all the same
row under different values.

The core package has **zero dependencies**. Redis and SQLAlchemy are optional extras.

| Question | Answer |
| --- | --- |
| what tells one entry from another? | the space and the key, and nothing else — there is no surrogate id anywhere |
| what stops twenty callers computing one cold value? | a name exactly one of them takes, written by the same conditional write everything else here uses |
| what happens when the store is unreachable? | a read is a miss, a write is dropped, and neither reaches the caller |
| what happens when the value cannot be written? | it is refused where it is written, because that is a bug and not a bad minute |

### What it deliberately does not do

- It is not a database. A value is bounded, entries have no relations, and nothing is found by anything but its name.
- It does not guess invalidation. Nothing watches your tables.
- It is not exactly once. A holder whose process is killed leaves a name held until its lease runs out, and the caller that takes it then computes the value a second time. Producers must be things you can run twice.

---

## 2. Layout

```
src/cachefy/
  __init__.py     empty, always — nothing is ever placed here
  app.py          Cachefy: the spaces it knows, the hooks, and the one place a store failure stops being the caller's problem
  space.py        Space: one family of names, the policy behind it, and every call a caller makes
  memo.py         Memo and Bound: a function whose answers live in a space of their own, and one reached as a method
  entry.py        Entry and MISS
  codec.py        as_written() — one value, meaning one thing, wherever the entries live
  keys.py         what a name may be, and how a call is named
  clock.py        now(), as_utc(), naive_utc(), real(), spanned(), waited(), EPOCH, WIDEST_INSTANT — the only place time is decided
  errors.py       CacheError, and UnknownSpace and UnwritableValue under it — one family, so `except CacheError` is every refusal this library raises
  janitor.py      Janitor: the sweep that drops what has died
  asgi.py         lifespan_for(janitor) — the lifespan protocol, which every asgi framework speaks
  store/
    __init__.py   empty, always
    base.py       the abstract Store contract and the constants every store shares
    memory.py     MemoryStore
    redis.py      RedisStore (Lua scripts)
    sqlalchemy.py SqlAlchemyStore (PostgreSQL, MySQL, SQLite)

tests/            pytest, asyncio_mode=auto, parametrized over every reachable store
docs/             the prose, kept honest by tests/test_docs.py
```

`src` layout, built with hatchling. `pyproject.toml` is the single source of tooling config.

---

## 3. The domain model

### Entry — one name and what it holds

The only thing any store here writes.

```
space, key, value, expires_at, stale_at, created_at, version
```

**The name is the identity.** There is no id anywhere: what tells one entry from another is what a
caller asks for it by, so a surrogate key would be a second name for the same row and a whole unique
index carried for nothing.

`alive(moment)` is `expires_at is None or expires_at > moment`. `stale(moment)` is
`stale_at is not None and stale_at <= moment`.

`MISS` is what a read answers for a name that holds nothing, and it is **never** the same thing as a
name holding `None`. A cache that answered `None` for both cannot say which it meant, so a function
that legitimately answers nothing is one whose answer is computed again on every single call while the
cache looks like it is working.

`version` is what `swap` is conditional on. It is **drawn where the entry is built and never counted by
a store**, because a count starts again wherever a name comes back from holding nothing — and a version
seen twice is a value written back over one nobody read. Counted, a caller that read an entry at version
one, watched it die and then swapped at version one **overwrote a value somebody else had written**, and
a single-flight holder whose lease had run out **released the lock the holder after it was on**.

It is drawn from the system and never from `random`, whose state a worker pool carries identically into
every process it forks. Three things fall out of drawing it rather than counting it: a store writes down
the number it was handed rather than working one out, so every store agrees by construction; the SQL
store stops reading the version back inside the write, which is **one round trip fewer on every write**;
and no `SET` clause reads the row, so MySQL working one out left to right stops mattering at all.

### Space — a family of names and the policy behind them

Declared once, through `Cachefy.space`, which is the one gate a policy passes through. `ttl`, `stale`
and `lease` are each a `timedelta` and never a number of seconds, and each is refused where it is
declared when it could never work.

`stale` has to be shorter than `ttl`, or the value would die before anything ever refreshed it. A
`stale` with no `ttl` is fine: a value kept until somebody drops it, refreshed by one caller now and
then.

### Store — where entries live

Abstract base with **twelve** methods. One rule runs through all of it:

> **Every method that changes an entry is conditional on the state it was in.**

That is what makes two callers safe without a lock anywhere.

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

**A sweep drops what has been dead longest first.** A batch that always picked the same end would
leave the rest lying there for ever, which is starvation nothing would ever report.

**A fresh value is answered without computing it again, and only a stale one is recomputed.** That is
what a freshness is for, and a `stale()` that answered true wherever one was set would turn every read
of that space into a recompute while looking exactly like a cache.

**A count is written under a lifetime and nothing else.** A freshness is what tells a caller to
recompute and nothing ever recomputes a count, so a space declaring one does not make `incr` refuse a
shorter lifetime for it.

**A count runs from `-(2**53 - 1)` to `2**53 - 1`**, and both the standing count and the total are
measured against it. Redis adds a counter up in Lua and Lua counts in doubles, so a range reaching
`2**53` is one where a single step past the edge rounds back onto it — the guard would pass and the
same number would be written down as the answer.

---

## 4. The flow

### Reading one name — `Space.get`

```
read through the guard → a store that could not answer is a miss
tell the listeners whether it held something
answer the value, or the default
```

**A hit or a miss is told once per name and never once per asking.** A name listed twice in a read of
many is still one name: one lookup, one answer, one telling. Nothing is ever told about the space the
locks live in, because a caller never asked for one of those names.

### Computing one value — `Space.fetch`

```
read → there and fresh          → answer it, and nothing else happens
     → take the name
         won  → run the producer → keep what it answered → let the name go
         lost → there but stale  → answer what is there while the holder computes
              → holding nothing  → ask again until the value lands, and compute it here if it never does
```

**A caller that waits stops the moment the store stops answering.** That one read answers whether the
store answered at all rather than going through the guard, because a guard that turns an outage into
"not there yet" is a request hanging on a cache for a whole lease.

**What computes a value is refused the same way wherever it is handed in.** A generator was refused at
the decorator and taken by `fetch`, which is the call the decorator is built on — so the same mistake
made through the primary api answered the generator object itself, kept nothing, and did it again on
every call. The predicate lives in `space.py` beside the guard that uses it, and `Cachefy.cached`
imports it to refuse the same thing earlier, where the space is declared rather than where it is read.
Both are needed: what reaches `fetch` from a memo is a lambda around the handler, and a lambda is never
a generator whatever it wraps.

**A producer may ask the cache for any other name, and never for the one it is computing.** It holds
that one itself, so waiting for it is waiting out a whole lease for a value nobody else is ever going to
write. What a caller is already computing lives in a `ContextVar`, which a producer and anything it
spawns both carry, and asking for one of those names is refused at once.

**A store failure is one short line and never a traceback.** The guard runs on the path of every
request, so an outage would otherwise write one per call — measured at a thousand reads against a store
that was gone: nine thousand lines and four hundred kilobytes. The line names the call, the entry, the
failure and what it said, and the `on_error` hook is handed the failure itself for whoever wants more.
**A listener that raises keeps its traceback**, because that is a bug in the calling code rather than a
state anything here expects.

**A key is written down and a value never is.** The name is what makes a line worth reading, so it goes
into the log and into the hook, which is why a key is not the place for a secret.

**A refusal about a name nothing can write must not itself be one.** The message names the key so the
caller knows which, and a key carrying a lone surrogate carried it straight into the message — where
printing it, logging it or answering with it raised a `UnicodeEncodeError` at the very place the refusal
was meant to be read. What goes into a message now is the name spelled as something anything can write,
with a nul byte escaped along with it, and a name that is not text is left out rather than spelled.

**A refusal is never answered around with a stale value.** The catch that serves what is there when a
refresh broke lets `CacheError` straight through, because everything under it is a mistake in the
calling code — and a bug served a value is a bug nothing ever reports.

**A cancelled caller lets the name go exactly as one that answered does**, because the release is in a
`finally` and the mark saying what this caller is computing is reset in one too. Neither is left behind
by a cancel at any round trip a fetch makes.

**And the release is shielded, because a caller cancelled twice is cancelled again inside it.** A
shutdown that cancels, waits and cancels harder lands the second one on the very await that lets the
name go — measured: the name stayed held and the caller after it waited out a lease of an hour. Shielded,
the release finishes after its caller is gone rather than never.

**Letting a name go is a write like any other, so it draws a version of its own.** Written back under
the one that took the name, the release would be two writes of one name carrying the same version —
which held only because a swap also asks whether the entry is alive. Soundness resting on a condition
that happens to be there beside it is soundness nobody can rely on.

**Letting a name go writes one that has already died rather than taking it away**, because taking it
away would let a holder whose lease had run out remove the name the holder after it is on. So a cache
that misses often leaves a dead row per miss, and what drains them is the sweep — at the speed of the
store rather than of the clock, since a full batch is followed at once.

**What the caller that computed is answered with is the settled value**, exactly as every caller after
it is. Handed the raw one, a producer answering a tuple gave that caller a tuple and everybody else the
list a store reads back — one call, two shapes.

Four rules hide in there:

- **Taking the name is `add`,** which is the same conditional write everything else uses. The store decides the winner, and nothing reads first. Letting it go is a `swap` at the version that holder drew, which is what stops a holder whose lease ran out from releasing the lock the holder after it is on.
- **A store that could not answer leaves this caller computing.** The fallback of that one guarded call is a holder nothing minted, so a broken store never leaves a caller waiting on a name nothing can tell it about — which would be a thirty second hang on every request of an outage.
- **The name is let go of however the producer ended,** in a `finally`, so a producer that raised never leaves the next caller waiting out a lease. Letting go is a `swap` at the version the holder minted, so it can never take a name somebody else holds.
- **What the producer raised is raised, unless there was a value to serve instead.** It is the caller's own code and the request has to see it — but a refresh that broke over a value that was only stale is answered with that value, logged and told to the listeners. The other thing that is not raised is an answer no store could write down: the value was computed and the caller asked for the value, so it is handed back uncached.

### Everything a store does — `Cachefy.guarded`

Every call this library makes to a store goes through it. It logs, tells the `on_error` listeners, and
answers the fallback.

**A cache is what makes an application faster and never what makes it work.** Nothing a store does is
ever raised at whoever asked. What is refused loudly is what the calling code got wrong, and that
happens before the guard is ever reached — validation is not inside it, so the two can never be
confused.

`setup` is the one call outside it. Coming up is not serving a request, and a process that could not
build what it needs must never come up pretending it did.

---

## 5. Invariants that must never be broken

1. **Every instant is UTC, decided in `clock.py` and nowhere else.** A naive datetime is the UTC instant it reads as. Never call `.timestamp()` directly in a store — go through `redis.stamp()` or `UtcDateTime`.
2. **A name is taken by a conditional write, never by read-then-write.** Let the store pick the winner.
3. **Nothing a store does reaches the caller.** A read is a miss, a write is dropped, a count is zero.
4. **What a caller got wrong is refused where it is written.** A key, a space, a value, a lifetime, an amount.
5. **A value of `None` is a value.** Nothing anywhere may fold it into a name holding nothing.
6. **What says an entry is dead is the field and never the server.** Redis is given an expiry so it can give the memory back, and every read still compares the field to the moment the caller decided.
7. **Nothing bounded is silently bounded.** Sweeps, reads of many names and retries all take batches, and every batch size is a named constant with the reason beside it.

---

## 6. Tuning constants, and why each one exists

| Constant | Where | Value | Why |
| --- | --- | --- | --- |
| `SPACE_LIMIT` / `KEY_LIMIT` | `store/base.py` | 64 / 255 | what every store sizes the two columns that name an entry for, refused where the name is written — a value past the column is a write the database refuses, or one it quietly cuts short, and two names cut to the same length are one entry where there should be two |
| `VALUE_LIMIT` | `store/base.py` | 1048576 | the most a store keeps one value as, counted in the characters it is written down as. a cache is memory somebody else is paying for, and one entry able to take all of it is a cache that answers every later write with a refusal from inside a driver |
| `WHOLE_FLOOR` / `WHOLE_CEILING` | `store/base.py` | -9223372036854775808 / 18446744073709551615 | the whole numbers MySQL keeps inside a JSON value as whole numbers. it turns everything either side of that into a double, so a value of `10**40` is read back off it as 9.999999999999998e+39 while memory, SQLite, PostgreSQL and Redis every one of them read back the number that was written. it is the one divergence that says nothing at all when it happens: nothing refuses it, nothing logs it, and the caller is handed a value nobody wrote |
| `COUNTER_FLOOR` / `COUNTER_CEILING` | `store/base.py` | -9007199254740991 / 9007199254740991 | what a count stays exact through wherever it is added up. Redis adds a counter up in Lua and Lua counts in doubles, so this is the whole numbers a double holds — and it stops one short of `2**53` on purpose, because one step past a range reaching that rounds back onto the edge it was leaving, the guard passes, and the very same number is written down as the answer |
| `DIGEST` | `keys.py` | 32 | how wide the digest of a call is, in bytes, which is sixty four characters written out. it is drawn with `blake2b` and never with `hash`, which Python salts per process — a key built with that one names an entry the process that wrote it can read and no other process ever can, which is a cache that answers a miss for ever while looking like it is working |
| `LEASE` | `space.py` | 30 seconds | how long one caller may hold a name while it computes, which is also the longest anybody waits on it. it is what makes a process killed mid computation cost one lease and never a request that hangs |
| `WAITING` | `space.py` | 0.02 | how often a caller that lost the name asks whether the value has landed |
| `BATCH_LIMIT` | `store/base.py` | 1000 | how many names one statement may name, which bounds a read of many and a sweep alike. measured: PostgreSQL refuses a statement naming about twenty thousand while SQLite and MySQL take it, so a batch past this is one that works on two stores and raises on the third |
| `LOCKS` | `space.py` | `cachefy-locks` | the space the names held while one caller computes live in, and one an application is refused where it declares a space. the key inside it is the digest of the space and the key together, length prefixed, so two spaces caching the same key never wait on each other |
| `VERSION_BITS` | `entry.py` | 62 | how wide a version is drawn, which is wide enough that one name never sees the same one twice and narrow enough for a `BigInteger` column to hold. Redis compares it as the text it was written down as rather than as a number, so nothing there has to fit what a double holds exactly |
| `SWEEP_EVERY` / `SWEEP_LIMIT` | `janitor.py` | 5 minutes / 500 | how long a sweep waits when the last one found nothing left to drop, and how many dead entries one sweep takes. a full batch is followed at once rather than after the interval, so a week that was never swept is caught up over passes instead of over a week |
| `CONTENDED` | `store/sqlalchemy.py` | {1205, 1213} | MySQL deadlock and lock-wait timeout — the documented handling is to ask again |
| `LOCKED` | `store/sqlalchemy.py` | {`SQLITE_BUSY`, `SQLITE_LOCKED`} | the same event on SQLite, which reports it as a code of its own rather than as a number in `args`. In WAL a second writer is refused **at once** rather than waiting the busy timeout out, so without this a write under concurrency is simply dropped |
| `TRIES` / `BACKOFF` / `SPREAD` | `store/sqlalchemy.py` | 8 / 0.005 / 1.0 | short to begin with because the other side only has to finish the statement it is already in, doubling because contention comes in bursts, and drawn so a herd InnoDB rolled back does not come back in lockstep to deadlock on each other again |
| `REWRITES` | `store/sqlalchemy.py` | 3 | how many times a write is asked again after the name it found nothing under turned out to be taken by the time it wrote. what this bounds is never that coincidence but a store answering both ways for ever, which would hang the call of whoever made it |
| `COUNTS` | `store/sqlalchemy.py` | 64 | how many times a count is written back after another caller wrote the same name in between. every refusal means somebody else's count landed, so a name is never stuck — what this bounds is one caller waiting behind a name hotter than the store can serialize |
| `PREFIX` | `store/redis.py` | `cachefy` | renames every key at once, so the store shares a Redis without ever meeting the application |

---

## 7. The stores

### MemoryStore

The whole library minus anything shared. Right for tests and for a single process, wrong for two. It
keeps a **deep copy** of every value both ways, because a caller that goes on changing what it wrote
must never change the entry, and a value handed out is one many callers hold at once.

**It holds no lock, because every method changes an entry without awaiting.** A lock around a body the
event loop cannot switch inside guards nothing, and one that is never contended suggests a protection
that is not there — measured: of twenty-five callers, never more than one was inside it at once. What
keeps that true is a test that reads the module and refuses an `await` anywhere in it.

It is the store the whole library is defined by: `tests/test_differential.py` compares every other one
against it, operation by operation.

### Versions

**PostgreSQL 14+, MySQL 8.0+, Redis 7.0+**, and whichever SQLite Python was built with. Both ends of
every range answer the whole suite on every push — a minimum nobody tests is a number in a table. When
a version reaches end of life, raise the floor and the CI job with it.

Verified against the floors themselves: Redis 7.0.15, MySQL 8.0.46 and PostgreSQL 14.24 answer the whole
suite and the stress suite, at 100% coverage.

### RedisStore

Where a cache belongs when reads and writes are both hot. Every mutation is one Lua step on the server,
so a write and the version it minted are never two round trips with the world between them.

| Key | Holds |
| --- | --- |
| `cachefy:entry:{space}:{key}` | the entry, as a hash |
| `cachefy:names:{space}` | the keys one space holds, so clearing it is not a scan |
| `cachefy:dying` | every entry scored by when it dies, so a sweep is a range and not a scan |
| `cachefy:spaces` | which spaces exist, so counting the whole cache is not a scan |

Things that are the way they are for a reason:

- **A store is whole as soon as it is built.** Registering a Lua script asks Redis nothing — redis-py digests the body locally and sends it on first use — so holding the registration back until `setup` left a store that read from the server and dropped every write, silently, with only a warning per call. It happens in the constructor now, and `setup` answers nothing, exactly as `MemoryStore` does.
- **The expiry Redis is given is what gives the memory back, and never what says an entry is dead.** Redis runs its own clock and this library decides every instant off the caller's. What a read answers by is the field on the hash, and `PEXPIREAT` beside it is what keeps a cache from growing until somebody sweeps. Without the field a clock that disagreed by a second would hand back a value somebody had already invalidated, and without the expiry every dead entry would sit in memory until a sweep reached it.
- **That expiry is rounded up to the millisecond Redis counts in.** An instant is held to the microsecond and Redis reclaims by the millisecond, so rounding down gave the memory back up to 999 microseconds early — a window where Redis answered a miss and memory, SQLite, MySQL and PostgreSQL all answered a hit, with nothing anywhere saying so. Rounded up, the hash outlives the instant its field names, and the field is still what every read answers by.
- **The instants a script compares are doubles.** Lua holds a whole number exactly to 2**53, which in microseconds since the epoch is the year 2255 — so `living()` is exact for every instant an application writes, and approximate only for one two centuries out, where the approximation is microseconds.
- **A name is joined out of two halves the caller gives separately**, which is why every script builds its keys from `ARGV` rather than declaring them in `KEYS`, and why this is one instance and never Redis Cluster. A replica for failover is fine.
- **A space may not hold a colon.** It is what joins the two halves, so a space called `user` holding `a:b` and one called `user:a` holding `b` would spell the same key and read each other's entries. It is also what lets a sweep split a name back into its halves at the first colon.
- **A drop answers off the listing and never off the hash.** An expiry Redis has already acted on takes the hash and leaves the name, while every other store still holds the row it is about to drop — so reading the hash would answer false for an entry every other store answers true for.
- **An entry is written into the listing whatever state it is in**, because a sweep only ever reads what was listed. One written straight into a store as already dead still has to be sweepable.
- **A count is `HINCRBY`-shaped but never `HINCRBY`.** What is there is a count only when the value was written down as a bare whole number, which is what the pattern match asks, and both the standing count and the total are measured against `COUNTER_FLOOR` and `COUNTER_CEILING` before anything is written. The answer goes back as text through `string.format('%d', ...)`, because a Lua number returned from a script is truncated on the way out and `tostring` switches to exponent notation past `1e14`.
- **A client built with `decode_responses` is refused where the store is built.** This store reads what Redis answers as bytes, and that setting is one an application sharing its client very often has. Nothing below could tell: writing would go on working while every read raised.
- **An instant is held as the whole microseconds since the epoch**, which is exact where Python reads it and a score wherever Lua sorts it.
- **Counting the depth walks what the spaces listed.** Redis has no index over a hash. It is for an operator watching what a cache holds, and never for a hot path.
- **Nothing is ever left that no call could remove.** Measured over four thousand random operations: no hash without a listing, no instant to die at without one, no space listed after it was emptied — and clearing every space leaves the database exactly as it was found.
- **`maxmemory-policy` should be `noeviction`.** An eviction takes an entry without touching the listing it was named in. Nothing here breaks over that — every script checks the hash is still there — but what an eviction costs is the value itself, which no code can give back.
- **Somebody else's value at one of our key names is a miss and never a failure.** A prefix shared with another application is a string where a hash belongs, and Redis refuses every command against it. The guard answers each as a miss, logs it and tells the listeners, so a collision costs the cache and never the request.

### SqlAlchemyStore — PostgreSQL, MySQL, SQLite

One table, `cachefy_entry`, under **metadata of its own**, so it never creates or drops anything of the
application's. One index carries the sweep: `cachefy_entry_dying` on `expires_at`.

Things that are the way they are for a reason:

- **The name is the primary key.** Point lookups and reads of one space are what the clustered index is then ordered by, which is exactly how the table is read.
- **A write is one update and, when that touched nothing, one insert.** Nothing reads the row: the version is one the entry already carries, so there is no statement whose answer the write has to wait for and read back.
- **A value of `None` is written as the json null it is**, through `none_as_null=False`. Left to a nullable column, a function that legitimately answers nothing would be read back as no entry at all, and its answer computed again on every single call while the cache looked like it was working.
- **On SQLite the value column is declared as text**, through `JsonText`. A column typed `JSON` there takes numeric affinity, and SQLite rewrites a stored number it thinks is lossless — which read `2**64-1` back as 1.8446744073709552e+19 while memory, MySQL, PostgreSQL and Redis every one of them read back the number that was written.
- **`UtcDateTime`** holds naive UTC and reads back aware UTC, because MySQL keeps no offset and a store that guesses one lets everything live an hour too long. On MySQL it becomes `DATETIME(fsp=6)` — MySQL **rounds** a datetime with no fractional precision, and an entry dying at `10:00:00.9` stored as `10:00:01` is a second of life nobody granted it.
- **There is no dialect-specific upsert anywhere.** The count of rows an update touched is what says whether the row was there, worked out by the database while it holds them, and an insert is what follows when it was not.
- **`setup` runs `create_all` twice on failure.** Asking whether the table is there and creating it are a question and a statement with a gap between them, and replicas booting together land in it. It reads no error message: with the table there the second call does nothing, and with it still missing it raises for whatever the real reason was.
- **A count is read and written back against the very version it read.** Arithmetic inside a json value is a statement each of these three databases spells differently, and `SELECT ... FOR UPDATE` is not a lock every one of them has — **pysqlite starts no transaction for a read at all**, so two callers read the same count and both wrote the one after it. Measured: twenty-five concurrent counts on SQLite landed on 2. The condition on the version is what makes that impossible, and the lock is what keeps it from ever being needed on MySQL and PostgreSQL.
- **A pool that ran out is likelier than a network that went away**, because a cache is read on every request. SQLAlchemy waits thirty seconds by default before it says so, which is a request hanging on a cache. Measured: with the pool held, a read waits out `pool_timeout` exactly and then degrades to a miss with the failure told to the hooks. Size the pool and give it a short timeout — see `docs/resilience.md`.
- **A sweep reads the names first and the delete names them by primary key.** Naming them inside the delete let the database drive it off the index the condition reads by, locking a secondary entry and then reaching for the row — while every write locks the row and then reaches for that same entry. Two orders around one row is a deadlock. The state those rows were read for is asserted again by the delete itself, or an entry another caller wrote while this waited on a lock is a living value dropped out from under them.
- **The two columns that name an entry are compared code point by code point.** MySQL builds one under `utf8mb4_0900_ai_ci` unless it is told otherwise, a collation that folds case and accents away before it compares — and behind the primary key that made `user:Bob` and `user:bob` one entry, so the second caller read and overwrote the value of the first. What `identifier` builds instead is `utf8mb4_0900_bin`, which is what memory, SQLite, PostgreSQL and Redis each already do.
- **`under_contention`** asks a write again when the database asked for it, and lets everything else through untouched. InnoDB answers a duplicate two transactions race for with a deadlock as often as with a duplicate-key error.
- **A busy SQLite is contention like any other.** It reports what happened through `sqlite_errorcode` rather than as a number in `args`, so a retry that only knew the numbers MySQL reports let it straight through and the write was dropped. In WAL a second writer is refused the moment it tries to upgrade to a write lock — SQLite answers at once instead of calling the busy handler, so the timeout the documentation asks for never even runs. Measured on the floor interpreter: twenty-five callers at one name lost writes to `database is locked` until this was read, and every caller computed the value because the store could not answer.

**A production engine needs `pool_pre_ping` and a timeout.** SQLite across processes needs WAL and a
busy timeout, and the table built once as a deploy step: several interpreters running the DDL of a
fresh file in the same instant is a lock none of them can wait out. See `docs/stores.md`.

---

## 8. Values, names and what is refused

**A value is JSON wherever the entries live.** `codec.as_written` writes it down and reads it back as
the store will, so a hit means the same thing everywhere:

- a tuple comes back as a list, and a key that is not a string comes back as one — settled rather than refused, because there is nothing wrong with either
- an object no serialiser takes, `nan`, `infinity`, a lone surrogate or a value that holds itself is **refused**
- a whole number past `WHOLE_CEILING` or under `WHOLE_FLOOR` is refused, because MySQL alone would read it back as a different number and say nothing
- and **a whole number is asked for by its interface and never by its type**: an id or a quantity written as an `int` subclass, an `IntEnum` among them, is one json writes out as the number it is, and `type(value) is int` let one past the range straight through — memory read back `10**40` where MySQL read back 9.999999999999998e+39, which is the very divergence that guard exists to stop. Where a boolean has to be refused instead, as it does for a counter step and a version, that is said outright rather than by asking the type of a number
- a value past `VALUE_LIMIT` written down is refused
- and **what every one of these judges is what json really wrote, never the object a caller passed**: the walk read the object while the encoder read the storage behind it, so a `dict` subclass answering its own `values()` hid `10**40` from the range check and handed it to the store — the one divergence that says nothing at all when it happens. The value is written down, read back and only then walked, so the guard and the store measure the same thing

**A name is refused rather than escaped.** What it names is which entry this is, so escaping one would
quietly fold two callers into a single row. Text, no nul byte, no lone surrogate, never empty, inside
the column, and no colon in a space.

**A name is settled the same way a value is.** `keys.plain` answers `str.__str__(value)`, and every
name a caller hands in goes through it before a store ever sees it. A `str` subclass may render as
something else entirely: a space declared as a `str, Enum` member renders in an f-string as
`Named.USERS` and reaches redis-py as `users`, so `RedisStore` wrote under one name and read back under
another — the write landed and the read of that same name answered nothing, for ever. `str(x)`, `f"{x}"`
and `x[:]` were each measured against `str, Enum`, `StrEnum` and a subclass that lies in `__str__`, and
only `str.__str__` answers the text in all three cases. **That is why a store validates nothing**: what
reaches one is already plain text and already json, settled at the boundary rather than twelve times
over in three stores.

**A name is settled before it is judged, and never judged as the object a caller passed.** Every guard
read the object: `len(value)` for the column, `"\x00" in value` for the nul byte, `not value` for the
empty name, `":" in space` for the joiner. A `str` subclass answers all four however it likes, and what
reaches a store is `str.__str__` of it — so one answering a length of one handed the store **three
hundred characters through a limit of 255**, where a database refuses the write or quietly cuts it
short and two names cut to the same length become one entry. Another hid a nul byte from the check that
exists because PostgreSQL refuses one, and another passed the empty-name check while being empty. The
text is taken first now, by `plain`, which owns the one rule about what is text at all, and every guard
below it measures what a store will really hold.

**Every name is settled where it is read, and never answered for and then thrown away.** Reading many
names walked what it was handed writing `key = named(key)` into the loop variable, which answers for the
name and discards the settled one — so a store and every listener were handed the object a caller
passed while every other call handed them text. It is one expression now, `tuple(named(key) for key in
keys)`, which reads a generator out whole, answers for each name before anything is deduped, and settles
it in the same step.

**A memoized method is reached as a method.** `Memo` is a descriptor, so what an instance answers is a
`Bound` carrying it, and every call made through one — the call itself, `refresh`, `invalidate`,
`key_for` — is made with that instance. Without it a memo is an attribute rather than a method: `self`
is never passed, so the first argument of every call is bound to it and the last one is missing, and
what a caller gets is a `TypeError` from inside `inspect` that `except CacheError` never catches.
Memoizing a method is the commonest thing after memoizing a function, and it did not work at all.

**What is memoized has to be something that can be called.** A `classmethod` is not, so `@cached` above
one raised a `TypeError` out of `inspect` where the class is written — outside the family
`except CacheError` catches, and saying nothing about what to do. It is refused there now, naming the
decorator and which way round the two go. A `staticmethod` is callable from 3.10 and works either way.

**What names a call on a method has to say which instance it was made on.** `self` is not something a
store can write down, so one left to name itself is refused where it is called — and naming it by the
object would name one entry per instance the process happens to build, which is a cache that never
hits. The `key` a caller gives is handed `self` exactly as the method is.

**Where the refusal lands depends on who asked.** `set` raises, because the caller asked for the value
to be kept and telling them it was not is the only honest answer. `fetch` and a memoized call do not:
the caller asked for the **value**, so an answer no store can hold is logged, told to the listeners and
handed back uncached. A cache must never be the reason a request fails.

---

## 9. Testing

```bash
make install     # venv + the package with its development tools
make servers     # redis on 6398, mysql on 3398, postgres on 5498
make test        # the suite
make coverage    # the suite with the 100% branch gate
make stress      # many processes against every server that answers
make lint        # ruff check + black --check
make format      # ruff --fix, then black — in that order
make build       # wheel and sdist
```

Rules the suite enforces on itself:

- **Coverage stays at 100%, branches included.** It is a gate, not an aspiration.
- **Every store answers the same contract.** `tests/test_store_contract.py` is written against the interface and parametrized over every reachable store. Add a backend to the fixture in `tests/conftest.py` and it inherits the whole suite.
- **And it answers it the same way.** `tests/test_differential.py` runs one seeded script of every operation against each store and against `MemoryStore`, comparing every field of every entry, every answer and every count. The suite grades eight seeds because it runs on every push, and the depth is checked apart from it: **150 seeds against each of SQLite, Redis, PostgreSQL and MySQL — ninety thousand operations a store — answered exactly as `MemoryStore` did.** Both of the drifts it found were invisible to a contract suite already passing everywhere: a SQLite column re-typing a big whole number, and MySQL reading the expiry a write had just moved.
- **A suite that is green on a store with no concurrency proves nothing about concurrency.** Measured: of twenty-five callers, `MemoryStore` never has more than one inside it, while Redis, SQLite and PostgreSQL each have all twenty-five. The races are real on every store that crosses a process boundary, and trivially sequential on the one that does not.
- **A suite that is green on a store with no concurrency proves nothing about concurrency.** Measured: of twenty-five callers, `MemoryStore` never has more than one inside it, while Redis, SQLite and PostgreSQL each have all twenty-five. The races are real on every store that crosses a process boundary, and trivially sequential on the one that does not.
- **What every suite reaches is measured, not assumed.** The interruptions sweep counted statements and commits while one call reached the database through `scalar`, which it never saw — and it swept six of the calls a store makes, leaving `drop` and `touch` uncut. The cancellation sweep lands at all four round trips a fetch makes, and the one place it cannot reach — a second cancel arriving inside the release — was found by asking where else a cancel could land rather than by running it again.
- **What that script reaches was measured, not assumed.** It aimed every swap at a version drawn out of the air, so across every seed **not one of them ever landed** — a store that wrote the wrong value on a successful swap passed the whole suite, which was demonstrated by breaking one. Half of them now aim at the version the name really carries. Living entries are weighted over dead ones for the same reason: what this suite is for is comparing seven fields, and a name holding nothing has one. Every outcome of every operation is now reached, and a store forgetting any field on the way back is caught by this suite alone.
- **And it answers a whole call or none of one.** `tests/test_interruptions.py` cuts one round trip of one call at a time — a statement, a commit or a scalar for a database, a command for Redis — and reads what that call left behind, sweeping until a cut no longer fires. Every round trip is refused twice over, once by a refusal that ends the call and once by the deadlock this library asks again after, because the second walks the rollback the first never reaches.
- **And it never raises at the caller.** `tests/test_resilience.py` asks every public call against a store that refuses everything, one pointed at a port nothing is listening on, and one that goes away halfway through a call.
- **A test that gives an operation a few milliseconds is one the slowest store loses.** It came back: a space declared with a thirty millisecond lifetime, written and then touched, needs both calls to land inside that window — and against MySQL in a loaded run they did not, so the entry died before the touch reached it and the suite failed for a reason that had nothing to do with the code. What that test asks is whether touching moves the instant an entry dies at, so it reads that instant before and after instead of racing it. The safe shape is the other way round, and it is the one the rest of the suite uses: give the entry a comfortable life and bring its death **forward** with `touch`, because a slow machine can only make it deader.
- **A test never waits on the wall clock.** Use `wait_until` from `tests/conftest.py`, and prefer an `asyncio.Event` to a sleep. Three tests here were flaky because they gave an operation thirty milliseconds to happen: what a test asks about is the behaviour, never how fast the machine is. Where an instant really has to pass, bring it forward with `touch` instead of waiting it out.
- **The suite is run in a random order as well as in its own.** Five orders, and nothing depends on what ran before it. It is not a gate, because a fixed order is what makes a failure reproducible, and this found nothing that a gate would have kept finding.
- **What the library promises is searched for counterexamples, not only sampled.** Sixteen thousand generated values and names against the settling properties — a value settled twice is the value settled once, anything settled is something json holds, a name settled twice is the name settled once — and every count stepped at eleven edges by seven amounts, compared across all five stores. No counterexample.
- **One session at a time against a server.** Every suite here owns the whole store, and the fleet suites name what they write after a fresh id — because `tmp_path` hands the same directory to the same test on every run, and a server the suite shares still holds what the run before it wrote.
- **A store nobody can reach is not collected.** Memory and SQLite always run; Redis, MySQL and PostgreSQL join when their port answers. `make coverage` needs all three.
- **Run against a real MySQL before believing anything about MySQL.** Its left-to-right `SET` clause and its default collation are invisible to SQLite and PostgreSQL.
- **The stress suite is marked `stress` and left out of every ordinary run.** The release runs it, because a version on PyPI is permanent and load is the one thing no ordinary run applies.
- **The suite is checked by breaking the code on purpose.** Sixty promises have been undone one at a time — the expiry rounded back down, a swap ignoring the version, an add writing over a living name, a sweep taking whatever end it liked or stopping a microsecond short, a store failure reaching the caller, a listener that exits taking the call with it, a producer allowed to ask for its own name, a value written without being settled, a value read handed straight out of the store, the caller that computed keeping the raw value, a refusal answered around with a stale value, a caller left waiting out a lease on a store that went away, a cancelled caller never letting a name go, a lock name that forgot which space it was for, a name travelling as the object it is rather than the text it holds, a lifespan serving before the store was built, a sweep taking a value written while it read the dead names, a memoized method reached as an attribute rather than a method, many names answered for and then thrown away unsettled, a span exactly at the last instant refused, a decorator that hides the function let through, a value stale exactly now read as fresh, a value exactly at the limit refused, a sweep taking whatever end it liked on every store at once, a sweep blind to anything that died before the epoch, a busy SQLite read as a failure rather than as contention, a readme example that no longer runs, a contract stated two ways, a documented call the library would refuse, a generator taken by the call the decorator is built on, a name judged as the object rather than as the text a store holds, a value judged as the object rather than as the json a store holds, a span measured by what it answered rather than by what it held, a counter step and a version taken at their word, many names read without being chunked, a stale value not served while one caller refreshes, and more. **Seven of them were missed the first time, and each miss was a test that passed for the wrong reason — the sweep-order test wrote its entries in the order they died, so a store answering in the order it was written passed it without ordering anything, and it only started biting once what is written last is what has been dead longest — one sweep was run against a suite that was already failing, so every catch it reported belonged to the stale test rather than to the mutation, and it had to be thrown away and run again.** A suite nobody has tried to fool is one nobody knows the strength of.
- **The contract is stated twice, so the two statements are compared.** It is written out in this document and in `docs/stores.md`, and the public one quietly said less: it omitted that `drop` answers whether the name held anything, which is exactly what somebody writing a store from that page would need. A test now reads both tables, checks each states every method of the interface, and refuses any wording that differs.
- **Every call the prose shows is read against the signature it would reach.** Building the declared policies catches a renamed `ttl` on a space and never one on `set`, so every call on a cache object in every example is checked for arguments the method does not take.
- **Every policy the documentation shows is built for real.** An example parses long after the library stopped taking what it shows, which is how a sweep batch of five thousand sat in `docs/janitor.md` after the bound came in. `tests/test_docs.py` reads every `space`, `cached` and `Janitor` the prose declares and builds one.
- **A refusal that is not a `CacheError` is a refusal outside the family.** Reading many names deduped before it validated, so a key nothing can hash was refused by a `TypeError` that `except CacheError` never catches. What a caller handed in is answered for before anything else touches it.
- **Every refusal message is read, not just raised.** A message that cannot be printed is a refusal nobody can act on, and the one about a lone surrogate carried the surrogate.
- **A documented integration nobody runs is one nobody knows works.** The three framework pages were run against the real frameworks, and one of them was wrong: the Django page reached the cache through `async_to_sync`, which builds an event loop, runs the coroutine and closes it again once per call. A connection pool belongs to the loop that opened it, so the call after it found that loop closed — measured against a real Redis, **five requests for one name computed it five times and four of them failed**, while the cache looked like it was working. The page shows the same bridge the Flask page does now: one loop the process keeps, which computed that name once. What pins it is a test over a store with a real connection pool, because `MemoryStore` has none and would answer the same either way. FastAPI and Flask were verified the same way and were already right.
- **A cancelled caller is its own suite.** `tests/test_cancellation.py` cuts a caller at each round trip it makes and reads what it left behind, because a name held by a caller that is gone is every caller after it waiting out a lease. It also runs the bridge the Flask page shows — a loop on a thread and twelve web threads handing work to it — because a documented integration nobody runs is one nobody knows works.
- **Many callers at one name is its own suite.** `tests/test_concurrency.py` puts twenty-five of them on one name for every conditional write there is: taking a free name, taking one whose value died, writing a value back over one version, counting, sweeping beside a write, clearing beside a write, and dropping beside an add. It also puts two *different* operations at one
name, which many callers at one operation never reach: a sweep that read the dead names beside a write
into one of them, and a drop beside a touch. Measured by breaking the source: with the delete no longer
asserting the state its rows were read for, a value written beside a sweep was lost 59 times out of 60.
- **100% coverage is not 100% of the interleavings.** Four of the worst bugs found so far were invisible to a suite already at 100%: SQLite re-typing a number, MySQL reading a value the same statement had written, pysqlite starting no transaction for a read, and a version counted per name repeating after that name had died. What reached them was a differential script, a real MySQL, twenty-five callers counting one name, and reading the code again with the question "what does this promise, and does it".

Files worth knowing:

| File | What it is for |
| --- | --- |
| `tests/test_store_contract.py` | the one suite every store answers |
| `tests/test_differential.py` | one seeded script, answered by each store and compared field by field against `MemoryStore` |
| `tests/test_interruptions.py` | every round trip a store makes, cut one at a time |
| `tests/test_resilience.py` | every public call against a store that is not there |
| `tests/test_stampede.py` | one caller computes, and what everybody else is served |
| `tests/test_cancellation.py` | a caller cut at each round trip it makes, and what it left held |
| `tests/test_logging.py` | what a cache writes down, which runs on the path of every request |
| `tests/test_concurrency.py` | many callers at one name at one instant, which is where a conditional write earns its place |
| `tests/test_docs.py` | the prose goes stale in silence, and every policy it shows is built for real |
| `tests/test_release.py` | what guards a version that can never be taken back — the tag gate, the order of the jobs, and the floors the documentation promises |
| `tests/test_review.py` | one test per bug a line-by-line reading found |
| `tests/test_disasters.py` | clocks that disagree, callers that die mid computation, values nothing was meant to hold |
| `tests/test_many_machines.py` | separate interpreters against one store, which is what containers are |
| `tests/test_fleet_stress.py` | `make stress` — many processes against a real server |
| `tests/fleet.py`, `machine.py` | the cache and the process a fleet test spawns, against whichever url it is given. Nothing is built inside a machine: the store is built once before the fleet comes up, which is the deploy shape the documentation tells everybody to use |

**When you fix a bug, add the test that would have caught it to `tests/test_review.py`**, named after
the behaviour and not the fix, with a docstring saying what went wrong. Then confirm it fails against
the unfixed source — a test that passes either way pins nothing.

---

## 10. CI and releasing

Three workflows, all under `.github/workflows/`.

**`test.yml`** runs on every push and pull request, with Redis, MySQL and PostgreSQL as service
containers on the same ports `make servers` uses. It declares `workflow_call`, so the release calls it
instead of repeating it. Two jobs:

- `test` — Python 3.11, 3.12 and 3.13 against the newest servers, linting and then the coverage gate.
- `oldest` — one Python against the oldest supported version of each store, which is what keeps the number in the documentation from being a number in a table.

**What a source distribution carries is named in `pyproject.toml`, anchored to the root.** Left unnamed
it takes whatever the working directory holds — a virtualenv beside the source went in, and one absolute
symlink inside it ended `python -m build` outright. Named but unanchored is worse than useless: `tests`
then matched a `tests` directory inside that virtualenv and pulled 191 of its files in while the build
still reported success. What is published is checked by reading the archive, never by reading the exit
code.

**`stress.yml`** is `make stress` on a runner, on one Python version. It runs on demand and on the
release, and not on a schedule: the code does not change at night.

**`release.yml`** runs on a `v*` tag and publishes to PyPI:

```
test    → the whole suite, called from test.yml
stress  → many processes against every server, called from stress.yml
build   → check the tag equals the version in pyproject.toml, then `python -m build`, upload the artifact
publish → download that same artifact, push it to PyPI, cut the GitHub release
```

Four things make it safe:

- **A release happens on a tag and on nothing else.** It also declared `workflow_dispatch`, and the check below was written `if: startsWith(github.ref, 'refs/tags/')` — so a run started by hand skipped the one gate that compares the tag to the version, while the job that uploads carried no condition at all and published anyway. A trigger that lets a permanent artifact out past its own gate is not a convenience. With the trigger gone both conditions were always true, so they went with it.
- **A version on PyPI is permanent**, so the suite answers for it before anything is built.
- **The tag has to equal `project.version`.** A tag that disagrees publishes under a number nobody asked for.
- **What is published is what was checked.** The publish job downloads the artifact the build job produced instead of building a second time.

It authenticates by **Trusted Publishing (OIDC)** — `id-token: write` and the `pypi` environment — so
there is no API token anywhere in the repository. The publisher registered on PyPI must say:

| Field | Value |
| --- | --- |
| PyPI Project Name | `cachefy` |
| Owner | `paulocoutinhox` |
| Repository name | `cachefy` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

To cut a release: bump `project.version` in `pyproject.toml`, commit, then push the matching tag.

```bash
git tag v1.0.1
git push origin v1.0.1
```

---

## 11. Code style — non-negotiable

Formatting is decided by `ruff` and `black` with **line length 320** and `skip-magic-trailing-comma`.
That number is not an accident: it exists so calls and signatures stay on one line.

**Layout**

- Functions, methods, constructors and calls stay on **one line**, always. Never break parameters across lines. Never format a signature vertically. However many parameters there are, they stay on one line.
- Keep it compact. Use only the blank lines that separate one context from the next, and always separate blocks of different responsibility with exactly one.
- Never leave `if`s, validations, state changes and returns visually glued together. A complex method has a beginning, a middle and an end you can see at a glance.
- Prefer early returns. No `else` after a `return`. Avoid needless nesting.
- Extract a small private method when a block is accumulating responsibility — and never just to make something shorter.
- No semicolons, ever.

**Comments**

- Rare, and only where they earn it. Well-named classes, methods and variables are the documentation.
- Every comment is a complete sentence, starting with a capital letter and ending with a full stop.
- Where a sentence would have to start with a lowercase identifier, keep its exact spelling and rewrite the sentence so it does not fall at the start.
- A comment above a function, method, class or module says **what it does for whoever calls it**, and never how it is implemented inside.
- Comments explain **why** — context and intent — never what the line already says.
- Never break one sentence across lines, and never continue a sentence on the next line. Finish it, punctuate it, then start a new one.
- No decorative comments, no section banners, no comments narrating a change that was made.

**What counts as a change**

- **A change earns its place by fixing something.** A bug, a race, a wrong result, a failure nobody records.
- **One thing is done one way.** Never add a second function that does what one already there does under another name.
- **A change that is only a comment is not a change.** Renaming, reshuffling or reformatting code nobody was fixing is not one either.

**Python**

- `__init__.py` files are **empty**. Absolutely nothing goes in them.
- No `TYPE_CHECKING`, and no `if TYPE_CHECKING:` import blocks.
- No backward compatibility and no legacy paths. There is one current version, and refactoring the whole thing to get there is expected.
- No generic fallbacks and no `else` branches invented for cases nobody understands.
- No dead code.
- Everything — code, comments, docstrings, log messages, tests — is in **English**.

**Validation**

Anything that could never work is refused **where it is written**: a key or a space no store could tell
one entry from another by, a value no store could write down, a lifetime that is already over, a
freshness longer than the lifetime behind it, a lease nobody could let go of, an amount a counter could
not be moved by, a version no entry could ever have been written under, a span given as a number where
a `timedelta` belongs, a generator handed to the decorator or straight to `fetch`, a space declared twice, or the one space the
locks live in. Each one carries a message that says what was asked for and why it cannot be.

**Every guard measures what the thing really holds, and never what it answers for itself.** A name, a
value, a span and a whole number are each a place where a caller hands in an object that can say one
thing to a guard and another to the code below it. A `str` subclass answers its own `__len__`, a `dict`
subclass its own `values()`, a `timedelta` subclass its own `total_seconds`, an `int` subclass its own
comparisons and its own `__int__`. Each is read through the base — `str.__str__`, the json a store will
really hold, `timedelta.total_seconds`, `int.__index__` — so what is judged and what is written down are
the same thing. Left as it was, a lying span passed a guard and raised `OverflowError` at the caller,
outside the family `except CacheError` catches.

**A refusal is refused and never answered around.** A version a store cannot compare, left through,
answers "somebody wrote in between" — which is a caller told its value was overtaken when what really
happened is that it asked with something that is not a version.

**Prose in `docs/`**

Every heading starts with an emoji, and no page uses the same one twice — `tests/test_docs.py` enforces
both, along with every symbol, table name, Redis key family and internal link the prose names. Keep the
voice: plain, concrete, and explaining the reason rather than the mechanism.

**No sentence begins with code.** Put a word in front of it — "The `run` loop polls", never "`run`
polls". A sentence opening on a backtick opens on a lowercase identifier, which reads as a fragment and
breaks the language rather than the code. The suite checks it.

---

## 12. The master audit checklist

What every full audit of this repository covers, and what each item is actually asking. An item is
ticked only once the work **and its validation** are both done. Where a finding came out of one, the
finding is named, because a tick with no memory of what it caught is a tick nobody can trust.

**How to use it.** Work top to bottom, on the whole tree and never only on what changed. When an item
finds something, fix the cause rather than the symptom, add the test that would have caught it, confirm
that test fails against the unfixed source, and only then tick it. Anything new goes in as its own item
with enough detail that it cannot be misread later.

### 12.1 Correctness of the domain

- [x] **Every mutating store method is conditional on the state it was in.** No read-then-write anywhere: `add` on free-or-dead, `swap` on the living version read, `touch` on alive, `bump` atomic under a version. Verified by breaking each condition in turn and watching the concurrency suite fail.
- [x] **A version is drawn and never counted.** A count restarts wherever a name comes back from holding nothing, so a version seen twice is a value written over one nobody read. Drawn with `secrets.randbits`, never `random`, whose state a forked worker pool carries identically.
- [x] **`MISS` is never the same thing as a name holding `None`.** A function that legitimately answers nothing must not be recomputed on every call.
- [x] **An entry dies at the instant its field names, and goes stale at the instant its field names.** Both edges pinned inclusively — the staleness edge was missing until a sweep found it.
- [x] **A sweep is only as right as the clock of the machine it runs on.** It drops what died before the instant it was given, so a janitor an hour fast takes an hour of living entries and one an hour slow sweeps late. What that costs is the entry and the recompute behind it and never a wrong value, because every reader still compares the field to its own instant. Pinned on every store, and said in `docs/janitor.md`.
- [x] **A sweep drops what has been dead longest first, on every store.** Starvation is the failure nothing would ever report. The test for it passed for the wrong reason until what is written last became what has been dead longest.
- [x] **A sweep sees an entry that died before the epoch.** Redis ranged from zero rather than from `-inf`, which no other store did.

### 12.2 Concurrency, cancellation and lifetimes

- [x] **Many callers at one name, for every conditional write there is.** Twenty-five of them, per store, measured to really interleave — `MemoryStore` never has more than one inside it, the others have all twenty-five.
- [x] **Two different operations at one name.** A sweep that read the dead names beside a write into one of them, and a drop beside a touch. Breaking the delete's re-assertion lost 59 of 60 written values.
- [x] **A cancelled caller lets the name go at every round trip it makes**, including a second cancel arriving inside the release, which is why that release is shielded.
- [x] **A producer that raises anything at all lets the name go**, `SystemExit` and `KeyboardInterrupt` included, and what it raised reaches the caller.
- [x] **A caller never waits on a store that stopped answering.** The poll that waits for another caller's value reads whether the store answered rather than going through the guard.
- [x] **A producer may never ask for the name it is computing.** Held in a `ContextVar` that a producer and anything it spawns both carry.
- [x] **Nothing runs before the store is built.** The lifespan builds it before it yields and before the sweep starts.
- [x] **No task, connection or file handle is left behind** over a long run, a shutdown, or a cancelled shutdown.

### 12.3 Resilience — nothing a store does reaches the caller

- [x] **Every one of the twelve store methods, against a store that refuses everything.** Measured: the broken store is really asked for 12/12, not a convenient subset.
- [x] **A store that is not there, and one that goes away halfway through a call.**
- [x] **Every redis script against a hash nothing here wrote**, and not only `read`: a missing `value`, `expires_at` or `version`, each of the three holding something no number reads, and a listed name whose hash is gone. Seven kinds of damage across every call a caller can make, and nothing raises — each is a miss with the failure told to the listeners.
- [x] **The suite runs with deprecation, pending-deprecation and runtime warnings promoted to errors**, so an api that is going away is a failure here before it is a failure on a future interpreter.
- [x] **A store holding what nothing here wrote** — json that will not parse, a version that is not a number, a field missing, a plain string where a hash belongs, somebody else's value at one of our key names. Each is a miss with the failure told to the listeners.
- [x] **A pool that ran out degrades rather than hanging**, and the documentation says to size it and give it a short timeout.
- [x] **A listener that raises breaks alone**, including one that calls `sys.exit`, and a cancel through a listener is never swallowed.
- [x] **A store failure writes one short line and never a traceback**, because this is the path of every request.

### 12.4 What is refused, and where

- [x] **Refused where it is written, never inside the guard**, so a caller mistake and a bad minute can never be confused.
- [x] **Every refusal is a `CacheError`**, so `except CacheError` is the whole family. A `TypeError` out of `inspect` for a decorator that hides the function was the last leak.
- [x] **Every refusal message is read, not just raised.** One carrying a lone surrogate could not be printed at all.
- [x] **A name is settled, not just answered for.** `str.__str__` at the boundary, so a `str` subclass that renders as something else still names the entry it holds. Reading many names answered for each and threw the settled name away.
- [x] **A value is settled the same way**, so a hit means the same thing on every store, and a whole number is asked for by its interface rather than its type.
- [x] **A span that could never work is refused**: already over, a freshness longer than the lifetime behind it, a lease nobody could let go of, a number where a `timedelta` belongs, an instant no datetime holds.

### 12.5 Dead code, legacy and duplication

- [x] **No unused public symbol.** `clock.kept` and `EARLIEST_INSTANT` were dead — a guard nothing called, with a test written for it. Coverage stayed at 100% after removing both, which is the proof they guarded nothing.
- [x] **No second implementation of anything.** One conditional write, one settling of names, one settling of values, one guard.
- [x] **No compatibility layer, no legacy path, no `TYPE_CHECKING`, no empty-`__init__` exception.**
- [x] **No two-phase construction that has no reason to be.** Registering a Lua script asks Redis nothing, so holding it back until `setup` left a store that read and dropped every write.
- [x] **No TODO, FIXME or hack anywhere in the tree.** Checked each pass, over source, tests, prose, the Makefile and the workflows.

### 12.6 What is documented is what the code does

- [x] **Every policy the prose shows is built for real**, so an example cannot outlive the bound it was written under.
- [x] **Every example parses, and every name it uses is one it imports.**
- [x] **Every documented integration is run against the real framework.** The Django page recommended `async_to_sync`, which builds and closes a loop per call — measured, five requests computed one name five times, four of them failing, while the cache looked alive.
- [x] **The README's own examples are executed, not only the pages under `docs/`.** It is the first code anybody copies, and parsing it proves only that it is python. The suite now runs it top to bottom and reads what it printed.
- [x] **Every link the documentation names resolves**, and every page under `docs/` is pointed at from the README.

### 12.7 The versions and the artifact

Running on the floor rather than only on the newest interpreter is what found two of this pass's three
findings. A suite green on one version says nothing about the versions the README promises.

- [x] **The whole suite passes on the oldest Python the project claims**, not only on the newest one it is developed against. Running it on 3.11 is what found the SQLite contention that 3.13 was simply fast enough to skate over.
- [x] **The built wheel installs into an empty environment and imports with zero dependencies**, and each extra brings what its store needs. Verified by installing it and reading, memoizing, single-flighting and counting from it.
- [x] **Nothing the source tree has is missing from the wheel**, and nothing the working directory happens to hold reaches the sdist. What it carries is named in `pyproject.toml`, anchored to the root — unanchored, `tests` matched a `tests` directory inside a virtualenv and pulled 191 of its files in.
- [x] **Both ends of every store version range answer the whole suite**, verified against Redis 7.0.15, MySQL 8.0.46 and PostgreSQL 14.24.

### 12.8 The suite, distrusted

- [x] **Every store answers one contract**, and answers it the same way, compared field by field against `MemoryStore` by a seeded differential script.
- [x] **Every round trip a store makes is cut, one at a time**, and what the call left behind is read.
- [x] **Coverage is 100% including branches, and is a gate rather than an aspiration.**
- [x] **The suite is checked by breaking the code on purpose.** Forty-six promises undone one at a time so far. Seven were missed the first time, and each miss was a test that passed for the wrong reason.
- [x] **No test waits on the wall clock.** A thirty-millisecond lifetime came back once and cost a false failure on MySQL.
- [x] **A type checker reads the package**, because an annotation that is wrong is worse than one that is missing. Mypy answers ten findings and **not one of them is a defect**: every one is the checker failing to narrow through an invariant that holds — a row read only where a boolean already proved it was there, a `SELECT COUNT(*)` that always answers a number, a lease that raises before it could ever answer nothing. Nothing is annotated to quieten it, because a checker the project does not run is not a reason to change code that is right.

### 12.9 Style, kept without exception

- [x] Functions, methods and calls on one line. Blank lines only between contexts. Early returns, no `else` after `return`.
- [x] Comments rare, each a complete sentence, capitalised, ending in a full stop, saying **why** and never what the line says.
- [x] No semicolon joining two statements, and none dividing a sentence in prose.
- [x] Everything in English — code, comments, docstrings, log lines, tests, prose.
- [x] No other project named anywhere in the tree.

### 12.10 The release, which cannot be taken back

- [x] **A release is reachable only from a tag.** Every other trigger is a way past the gate that compares the tag to the declared version, and what it publishes can never be unpublished.
- [x] **That gate carries no condition**, because a conditional gate is one some run skips.
- [x] **Nothing is built until the suite and the load have both answered**, and what is published is the artifact the build made rather than a second build nobody ran against.
- [x] **It authenticates by a token nobody stores**, through Trusted Publishing and the `pypi` environment.
- [x] **The versions the CI runs are the versions the documentation promises** — both ends of every store range, and every Python the package claims, including the `requires-python` floor.

### 12.11 The harness and the environment, distrusted

- [x] **A fleet process that died is noticed.** The spawner asserts the return code of every machine and reads every output file, so a machine that crashed can never be quietly left out of the result.
- [x] **Nothing is built inside a fleet machine.** The store is built once before the fleet comes up, which is the deploy shape the documentation tells everybody to use — a stale comment claimed each machine had to build it for Redis, which stopped being true when the scripts moved into the constructor.
- [x] **The suite imports only what `make install` declares.** Every third-party module the source and the tests reach for is one a fresh clone gets, so a green run here can never mean a red run on a runner.
- [x] **Two suites never share a server.** Measured the hard way: a full suite left running in the background drops the tables a fleet test is using, and the fleet test then fails for a reason that has nothing to do with the code.

### 12.12 The gates, every pass

- [x] `make lint` clean.
- [x] `make coverage` at 100%, three consecutive runs.
- [x] `make stress` green.
- [x] `make build` produces a wheel and an sdist.
- [x] The tree cleaned and every container removed at the end.

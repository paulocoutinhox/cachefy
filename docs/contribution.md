# 🤝 Contribution

Thanks for wanting to help. 🙌

## 🚀 Getting set up

```bash
make install
make test
```

The suite runs against memory and SQLite with nothing else installed. The other three stores take part
only when their server is reachable, and are simply not collected when it is not:

```bash
make servers
```

That starts a Redis on 6398, a MySQL on 3398 and a PostgreSQL on 5498. Point the suite somewhere else
with `CACHEFY_REDIS_URL`, `CACHEFY_MYSQL_URL` and `CACHEFY_POSTGRES_URL`.

> **Those three belong to the suite and not to the package.** They are read by `tests/conftest.py` and
> by nothing else. The library itself reads no environment variable anywhere, and is handed a client or
> an engine instead. There are three rather than one because the suite uses all three at once: every
> store answers the same contract in the same run, so a single url would quietly leave two of them
> untested.

> **A `make coverage` run needs all three.** The gate is 100%, and a store nobody could reach is a
> store whose lines nobody ran. The plain `make test` works without them.

> **The drivers come with `make install`, and they have to.** A store takes part whenever its port
> answers, and the port answering is the whole test — so with the servers up and a driver missing,
> every test of that store fails on building the engine instead of being quietly left out. That is why
> `aiomysql`, `asyncpg` and the `cryptography` MySQL 8 authenticates with are development tools here
> rather than extras of the package.

**Run the suite against a real MySQL before believing anything about MySQL.** It works a `SET` clause
out left to right and it builds a column under a collation that folds case away, and neither of those
is something SQLite or PostgreSQL will ever tell you about.

## ✅ Before you open a pull request

```bash
make format
make coverage
make lint
```

Three things the pipeline will check anyway, and it is faster to hear it from your own machine.

**Touched a store? Run `make stress` too.** It is many processes against every server that answers,
and what it reaches is the interleaving. The release runs it before publishing, because a version on
PyPI is permanent.

## 📐 What the project asks of a change

**Coverage stays at 100%, branches included.** It is a gate and not an aspiration. A line nobody
exercises is a line nobody knows the behaviour of.

**A new store answers the same contract.** `tests/test_store_contract.py` is written against the
interface and parametrized over every store — add yours to the fixture in `tests/conftest.py` and it
inherits the whole suite.

**And it answers it the same way.** A suite written by hand only ever asks the questions somebody
thought to ask, so `tests/test_differential.py` asks the ones nobody did: one seeded script of every
operation, run against each store and against `MemoryStore`, compared on every field of every entry.
Both of the drifts it found were invisible to a contract suite already passing everywhere.

**And it answers a whole call or none of one.** The sweep in `tests/test_interruptions.py` cuts one
round trip of one call at a time — a statement or a commit for a database, a command for Redis — and
reads what that call left behind. Every round trip is refused twice over: once by a refusal that ends
the call, and once by the one this library asks again after, because the second walks the rollback the
first never reaches.

**And it never raises at the caller.** `tests/test_resilience.py` asks every public call against a
store that refuses everything, one pointed at a port nothing is listening on, and one that goes away
halfway through. None of them may raise.

**A test never waits without a bound.** Use `wait_until` from `tests/conftest.py`, and prefer an
`asyncio.Event` to a sleep: a test that depends on how fast the machine is will fail on somebody
else's.

**A name means one thing.** An *entry* is one name and what it holds, a *space* is a family of them, a
*store* is where they live, and a *memo* is a function whose answers live in a space. Mixing them is
how documentation stops being true.

## 🎨 Style

Both `ruff` and `black` decide formatting, and `make format` runs them in the order that settles: the
linter first, because it removes imports the formatter already laid out.

Calls stay on one line — the line length is 320 for exactly that reason.

Comments are rare and earn their place. Each is a complete sentence, and it explains **why**. If a
comment says what the line does, the line should have been clearer instead.

## 🐛 Reporting something

An issue that shows how to reproduce is worth ten that describe. If it is a race, say how many callers
and which store — those two answer most of it.

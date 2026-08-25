<p align="center">
    <a href="https://github.com/paulocoutinhox/cachefy" target="_blank" rel="noopener noreferrer">
        <img width="420" src="extras/images/logo.png" alt="Cachefy">
    </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/cachefy/"><img src="https://img.shields.io/pypi/v/cachefy.svg" alt="PyPI version"></a>
  <a href="https://github.com/paulocoutinhox/cachefy/actions/workflows/test.yml"><img src="https://github.com/paulocoutinhox/cachefy/actions/workflows/test.yml/badge.svg" alt="Cachefy - Test"></a>
  <a href="https://codecov.io/gh/paulocoutinhox/cachefy"><img src="https://codecov.io/gh/paulocoutinhox/cachefy/graph/badge.svg" alt="Coverage"></a>
  <a href="https://github.com/paulocoutinhox/cachefy/blob/main/LICENSE.md"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org"><img src="https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue.svg" alt="Python versions"></a>
</p>

<p align="center">
Asynchronous cache for Python, where a store that blinked is never a failure the caller sees.
</p>

<br>

## 🚀 Project

Cachefy is a cache built around one row: **a name that holds a value until an instant**.

A read is that row before that instant. A write replaces it. A miss is that row absent or past its
instant. Memoizing a function, holding a name so exactly one caller computes, and counting a rate
limit are all the same row under different values.

Two promises run through the whole of it:

- **A store that cannot be reached is a miss and never an exception.** A cache is what makes an application faster, not what makes it work.
- **A value that could never be written is refused where it is written.** That is a bug in the calling code, and it is told at once.

## ✨ Features

- [x] Read, write and drop a value under a name, with a lifetime per space or per call
- [x] Memoize a function on the arguments it was called with, and invalidate one call or all of them
- [x] One caller computes while the rest wait, so a cold key never becomes a stampede
- [x] Stale-while-revalidate: one caller refreshes while everybody else is served what is there
- [x] Compare-and-set, so a slow writer never overwrites a newer value
- [x] Atomic counters for rate limits and quotas
- [x] Named spaces, cleared whole in one step
- [x] Hooks for hits, misses and every call a store could not answer
- [x] Pluggable stores: PostgreSQL, MySQL, Redis, SQLite and memory
- [x] A value of `None` is a value, told apart from a name holding nothing
- [x] No dependencies in the core
- [x] 100% branch coverage: one contract against every store, plus differential, concurrency, interrupted-call, contention, resilience and multi-process suites

## 📦 Install

```bash
pip install "cachefy[redis]"
```

Or with SQLAlchemy:

```bash
pip install "cachefy[sqlalchemy]"
```

## 🧭 The five things it does

| What you want | How you ask for it |
| --- | --- |
| read a value | `await users.get("42")` |
| write a value | `await users.set("42", profile)` |
| compute it once, however many callers ask | `await users.fetch("42", load)` |
| remember what a function answered | `@app.cached("profile", ttl=timedelta(minutes=5))` |
| count something under a name | `await limits.incr(address)` |

## 💡 How to use

```python
import asyncio
from datetime import timedelta

from redis.asyncio import Redis

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore

app = Cachefy(RedisStore(Redis.from_url("redis://127.0.0.1:6379/0")))

users = app.space("users", ttl=timedelta(minutes=5), stale=timedelta(minutes=1))


@app.cached("profile", ttl=timedelta(minutes=5))
async def profile(user_id: int) -> dict:
    return {"id": user_id, "name": "Paulo"}


async def main():
    await app.setup()

    await users.set("42", {"name": "Paulo"})
    print(await users.get("42"))
    print(await profile(42))

    await profile.invalidate(42)


asyncio.run(main())
```

## 🧱 The words it uses

Four of them, and each means one thing:

| Word | What it is |
| --- | --- |
| **Entry** | one name and the value it holds until an instant |
| **Space** | a named family of entries, and the policy they are all written under |
| **Store** | where the entries live: Redis, a database, or this process |
| **Memo** | a function whose answers live in a space of their own |

## 📚 Documentation

- [Getting started](docs/getting-started.md)
- [Spaces](docs/spaces.md) — lifetimes, freshness and every call a space answers
- [Memoizing](docs/memoizing.md) — caching what a function answered
- [Stampede](docs/stampede.md) — one caller computes, and what everybody else is served
- [Resilience](docs/resilience.md) — what happens when the store is gone
- [Stores](docs/stores.md) — Redis, SQLAlchemy, memory, and writing your own
- [Janitor](docs/janitor.md) — sweeping what has died
- [Hooks](docs/hooks.md) — hits, misses and failures
- [Frameworks](docs/frameworks.md) — the rule that covers all of them
  - [FastAPI](docs/fastapi.md) — the lifespan, and caching a response
  - [Django](docs/django.md) — the ORM from a producer, and a management command
  - [Flask](docs/flask.md) — a synchronous world, and where the loop lives
- [Contribution](docs/contribution.md) — how to help

## 🏷️ Releasing a new version

A release is a tag and nothing else. Bump `project.version` in `pyproject.toml`, commit it, then push
the matching tag:

```bash
git tag v1.0.1
git push origin v1.0.1
```

That tag is the only trigger. The workflow then runs the suite on every supported Python against real
Redis, MySQL and PostgreSQL, runs the stress suite against them, checks that
the tag agrees with `pyproject.toml`, builds the wheel and the sdist, publishes them to PyPI through
Trusted Publishing, and creates the GitHub release with its notes and the built files attached.

> **Do not publish the release from the GitHub interface.** Creating one there writes the tag, the tag
> starts the workflow, and the workflow then finds a release already sitting where it was going to
> write its own. Push the tag and let the workflow create the release.

> **A version on PyPI is permanent.** A tag that disagrees with `project.version` is refused before
> anything is built, because a number published by mistake is one PyPI never lets anybody use again.

## ☕ Buy me a coffee

Support the continuous development of this project.

<a href='https://ko-fi.com/A0A412XEV' target='_blank'><img height='36' style='border:0px;height:36px;' src='https://storage.ko-fi.com/cdn/kofi2.png?v=6' border='0' alt='Buy Me a Coffee at ko-fi.com' /></a>

## 📄 License

[MIT](http://opensource.org/licenses/MIT)

Copyright (c) 2026, Paulo Coutinho

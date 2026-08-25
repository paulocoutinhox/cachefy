# 🧩 Frameworks

Everything on this page is true of every framework. Each one then has a page of its own, because what
differs between them is not the cache — it is whether their world is synchronous or asynchronous.

## 📚 Pick yours

| Framework | Its world | Where the janitor lives |
| --- | --- | --- |
| [FastAPI and Starlette](fastapi.md) | asynchronous | inside the web process, on the lifespan |
| [Django](django.md) | synchronous at heart, asynchronous where you ask | a management command |
| [Flask](flask.md) | synchronous | a process of its own |
| Anything else | either | wherever you can run an asyncio task |

## 🧭 The one rule

**The cache is one module every process imports.** The web side and anything beside it build the same
spaces from the same code, because a space has to mean the same thing on both sides.

```python
# myapp/cache.py — imported by the web process and by everything beside it
from datetime import timedelta

from redis.asyncio import Redis

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore

from myapp.settings import REDIS_URL

app = Cachefy(RedisStore(Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1)))

users = app.space("users", ttl=timedelta(minutes=5), stale=timedelta(minutes=1))
```

**The library reads no environment variable, ever.** You build the Redis client or the engine and hand
it over, so configuration stays in the one place your application already keeps it.

**A space is declared once.** Declaring the same name twice is refused, which is what catches a module
imported under two paths.

## ⚖️ Synchronous or asynchronous

This is the only question that changes anything, and it is asked twice — once of the producer, and once
of the code that reads.

**A producer may be either.** An `async def` runs on the event loop and a plain `def` is called where
the caller is. Nothing here moves your code to a thread, so if a plain producer blocks, it blocks the
loop — make it `async def` and reach for `asyncio.to_thread` yourself.

**Reading follows the caller.** Asynchronous code awaits `get` and `fetch` like anything else.
Synchronous code hands the work to **one event loop the process keeps**, on a thread of its own, and
the framework pages show that bridge. What it must not do is build a loop per call: a connection pool
belongs to the loop that opened it, so the call after it finds that loop closed, every read fails into
a miss and the producer runs again on every single request — a cache that looks like it is working and
never hits.

## 🔌 What a producer may touch

**Anything the rest of your application can.** The two are independent in storage and joined in code:
the cache keeps its entries in Redis or in `cachefy_entry` and never looks at your tables, while a
producer is ordinary application code that may use your ORM, your models and your clients.

The value carries **what a request needs and never an object**, because it has to survive a trip
through JSON. A tuple comes back as a list and a key that is not a string comes back as one, so what a
producer answers is settled where it is written and means the same thing wherever the entries live.

## 🧹 Where the janitor goes

One per fleet is enough, and more than one is harmless. Under an asgi framework the lifespan does it:

```python
from cachefy.asgi import lifespan_for
from cachefy.janitor import Janitor

lifespan = lifespan_for(Janitor(app))
```

That builds the store before the first request and stops the sweeping when the process goes. Anywhere
else, run `Janitor(app).run()` as a task — see [Janitor](janitor.md).

## 🖥️ Standalone

No framework at all:

```python
import asyncio

from cachefy.janitor import Janitor

from myapp.cache import app, users


async def main():
    await app.setup()
    sweeping = asyncio.create_task(Janitor(app).run())

    print(await users.get("42"))


asyncio.run(main())
```

## 📈 Watching it

```python
depth = await users.count()
```

Pair it with the [hooks](hooks.md). The miss rate is what tells you the cache stopped working, and the
depth is what tells you how much it holds.

# 🚀 Getting started

## 📦 Install

```bash
pip install "cachefy[redis]"
```

The core has no dependencies. The extra pulls the Redis client, which is where most caches belong.
Use `cachefy[sqlalchemy]` instead to keep the entries in the database you already have.

## 🧱 Build the cache

A cache is a store plus the spaces you declare against it. Every process of your fleet builds the same
one from the same code.

```python
from redis.asyncio import Redis

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore

app = Cachefy(RedisStore(Redis.from_url("redis://127.0.0.1:6379/0")))
```

Calling `await app.setup()` builds whatever the store needs. On a database that is the one table this
library uses, under metadata of its own, so it never touches a table of yours. Memory and Redis need
nothing built and answer it at once — a Redis store is whole as soon as it is constructed.

## 📁 Declare a space

A space is a family of names and the policy every entry of it is written under.

```python
from datetime import timedelta

users = app.space("users", ttl=timedelta(minutes=5))
```

Declaring it is what refuses a lifetime that could never work, so a policy nobody could honour fails
where you wrote it and not on the night nothing is ever cached.

## ✂️ One space and nothing else

There is no entry without a space: what tells one from another is the space and the key together. When
you only want a cache, declare one and keep that.

```python
from datetime import timedelta

from redis.asyncio import Redis

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore

cache = Cachefy(RedisStore(Redis.from_url("redis://127.0.0.1:6379/0"))).space("cache", ttl=timedelta(minutes=5))
```

From there it is `set` and `get` and nothing else to think about, and every call below reads the same
whichever way you declared it.

**A space still knows the cache it belongs to**, as `cache.app`, which is where `setup` and the hooks
live. On a database that matters: without `await cache.app.setup()` there is no table, so every write
is dropped and every read is a miss — told to the hooks and never raised, which is a cache that looks
alive and holds nothing. Memory and Redis need nothing built.

## 📨 Read and write

```python
await users.set("42", {"name": "Paulo"})
profile = await users.get("42")
```

A name that holds nothing answers `MISS`, which is never the same thing as a name holding `None`:

```python
from cachefy.entry import MISS

if await users.get("42") is MISS:
    ...
```

## 🔁 Compute it once

Most of the time you do not want a read and a write. You want the value, computed once however many
callers ask at the same instant:

```python
async def load() -> dict:
    return {"name": "Paulo"}


profile = await users.fetch("42", load)
```

The first caller computes it, and every other caller is handed what that one wrote. Read
[Stampede](stampede.md) for what happens to the ones that wait.

## 🧠 Memoize a function

```python
@app.cached("profile", ttl=timedelta(minutes=5))
async def profile(user_id: int) -> dict:
    return {"id": user_id}


await profile(7)
await profile.invalidate(7)
```

## 🧹 Sweep what has died

Redis gives the memory back on its own, but the index it is read out of and every other store need a
sweep. Run one beside your application:

```python
import asyncio

from cachefy.janitor import Janitor

asyncio.create_task(Janitor(app).run())
```

Under an asgi framework there is a lifespan that does it for you — see [Frameworks](frameworks.md).

## 💡 The one thing to know

Nothing a store does is ever raised at whoever asked. A Redis that went away is a miss, a database
that refused a write is a value that was not kept, and your request carries on either way. What is
refused loudly is what you got wrong: a key no store could tell apart, or a value none of them could
write down. Read [Resilience](resilience.md) for where that line is drawn.

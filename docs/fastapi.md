# ⚡ FastAPI

The most direct fit: the world is already asynchronous, so nothing has to be bridged.

## 🧱 The cache module

```python
# myapp/cache.py
from datetime import timedelta

from redis.asyncio import Redis

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore

from myapp.settings import REDIS_URL

app = Cachefy(RedisStore(Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1)))

users = app.space("users", ttl=timedelta(minutes=5), stale=timedelta(minutes=1))
```

## 🔌 The lifespan

```python
# myapp/main.py
from fastapi import FastAPI

from cachefy.asgi import lifespan_for
from cachefy.janitor import Janitor

from myapp.cache import app as cache

api = FastAPI(lifespan=lifespan_for(Janitor(cache)))
```

The store is built before the first request is served, and the sweeping stops when the process goes.

**Already have a lifespan?** Nest them:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cachefy.asgi import lifespan_for
from cachefy.janitor import Janitor

from myapp.cache import app as cache


@asynccontextmanager
async def lifespan(api):
    async with lifespan_for(Janitor(cache))(api):
        yield


api = FastAPI(lifespan=lifespan)
```

## 📨 Reading in a route

```python
from fastapi import FastAPI

from myapp.cache import users
from myapp.database import load_account

api = FastAPI()


@api.get("/users/{user_id}")
async def read_user(user_id: int) -> dict:
    return await users.fetch(str(user_id), lambda: load_account(user_id))
```

That is the whole integration. The first request computes it, every request for the next five minutes
is answered from the cache, and after the first minute one request a minute refreshes it while
everybody else is served what is there.

## 🧠 Memoizing a query

```python
from datetime import timedelta

from myapp.cache import app
from myapp.database import summarize


@app.cached("report", ttl=timedelta(hours=1), stale=timedelta(minutes=5))
async def report(day: str) -> dict:
    return await summarize(day)
```

## 🗑️ Invalidating on a write

```python
from fastapi import FastAPI

from myapp.cache import users
from myapp.database import save_account

api = FastAPI()


@api.put("/users/{user_id}")
async def write_user(user_id: int, body: dict) -> dict:
    saved = await save_account(user_id, body)
    await users.drop(str(user_id))

    return saved
```

**Drop after the write commits, not before.** Dropping first leaves a window where a read computes the
old value again and caches it.

## 🚦 Rate limiting a route

```python
from fastapi import FastAPI, HTTPException, Request

from myapp.cache import app

api = FastAPI()
limits = app.space("limits", ttl=timedelta(minutes=1))


@api.get("/search")
async def search(request: Request) -> dict:
    seen = await limits.incr(request.client.host)

    if seen is not None and seen > 60:
        raise HTTPException(status_code=429)

    return {"results": []}
```

**The count is checked against `None`**, because a store nobody can reach answers nothing at all — and
a limiter that cannot reach its store should let traffic through rather than refuse all of it.

## 🩺 A health check that tells the truth

```python
from fastapi import FastAPI

from myapp.cache import users

api = FastAPI()


@api.get("/health")
async def health() -> dict:
    return {"cache": await users.count()}
```

That never fails, because nothing here does. Watch the [hooks](hooks.md) for whether it is working.

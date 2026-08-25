# 🍶 Flask

Flask is synchronous all the way down, so the whole question is where the event loop lives.

## 🧱 The cache module

```python
# myapp/cache.py
from datetime import timedelta

from redis.asyncio import Redis

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore

from myapp.settings import REDIS_URL

app = Cachefy(RedisStore(Redis.from_url(REDIS_URL, socket_timeout=1, socket_connect_timeout=1)))

users = app.space("users", ttl=timedelta(minutes=5))
```

## 🌉 One loop, and never one per request

```python
# myapp/bridge.py
import asyncio
import threading

_loop = asyncio.new_event_loop()

threading.Thread(target=_loop.run_forever, daemon=True).start()


def waited(work):
    """Answers what a coroutine answered, from the one loop this process keeps."""
    return asyncio.run_coroutine_threadsafe(work, _loop).result()
```

**A loop per request is the mistake to avoid.** Calling `asyncio.run` inside a view builds a loop,
opens a connection, closes both and throws the pool away every time — which costs more than the query
the cache was saving. One loop on one thread, opened once, is what makes this worth doing at all.

## 📨 Reading in a view

```python
from flask import Flask, jsonify

from myapp.bridge import waited
from myapp.cache import users
from myapp.database import load_account

api = Flask(__name__)


@api.get("/users/<int:user_id>")
def read_user(user_id):
    return jsonify(waited(users.fetch(str(user_id), lambda: load_account(user_id))))
```

The producer is a plain function called from the loop thread, which is where a synchronous library
belongs anyway.

## 🗑️ Invalidating on a write

```python
from flask import Flask, jsonify, request

from myapp.bridge import waited
from myapp.cache import users
from myapp.database import save_account

api = Flask(__name__)


@api.put("/users/<int:user_id>")
def write_user(user_id):
    saved = save_account(user_id, request.json)
    waited(users.drop(str(user_id)))

    return jsonify(saved)
```

**Drop after the write commits.** Dropping first leaves a window where a read computes the old value
again and caches it.

## 🧹 The janitor in a process of its own

```python
# myapp/janitor.py
import asyncio

from cachefy.janitor import Janitor

from myapp.cache import app


async def main():
    await app.setup()
    await Janitor(app).run()


asyncio.run(main())
```

```bash
python -m myapp.janitor
```

Run it beside the web processes rather than inside them. A Flask process forked by a worker manager is
one whose background threads do not survive the fork, and a sweep that quietly stopped is a cache that
quietly grows.

## 🧵 What a thread means here

Every Flask worker thread hands its work to the one loop, so the Redis client is shared and its pool is
sized once. That is the setup this is written for.

**What that pool has to have is a timeout.** A client that waits forever turns a network that went away
into a request that never returns, and it is the one failure this library cannot answer for on your
behalf — see [Resilience](resilience.md).

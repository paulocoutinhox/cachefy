# 🎸 Django

Django is synchronous at heart and asynchronous where you ask, so the only question is which side each
piece of your code is on.

## 🧱 The cache module

```python
# myapp/cache.py
from datetime import timedelta

from django.conf import settings
from redis.asyncio import Redis

from cachefy.app import Cachefy
from cachefy.store.redis import RedisStore

app = Cachefy(RedisStore(Redis.from_url(settings.CACHE_REDIS_URL, socket_timeout=1, socket_connect_timeout=1)))

accounts = app.space("accounts", ttl=timedelta(minutes=5))
```

Configuration stays in your settings module, because this library reads no environment variable of its
own.

## 🌉 One loop the process keeps

A synchronous view has no event loop of its own, and one built per call is a cache that never hits. A
connection pool belongs to the loop that opened it, so the call after it finds that loop closed, every
read fails into a miss, and the producer runs again on every single request while the log fills with
`Event loop is closed`. Measured against a real Redis: five requests for one name computed it five
times and four of them failed. That is why `async_to_sync` around a cache call is the one shape to
avoid — it builds a loop, runs the coroutine and closes it again, once per request.

Keep one loop for the process and hand the work to it:

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

Measured the same way, five requests for one name computed it once, and twelve web threads arriving at
one cold name together computed it once between them.

## 📨 Reading from a synchronous view

```python
from django.http import JsonResponse

from myapp.bridge import waited
from myapp.cache import accounts


def read_account(request, account_id):
    return JsonResponse(waited(accounts.fetch(str(account_id), lambda: loaded(account_id))))
```

The producer runs where the caller is, so a synchronous view means a synchronous producer:

```python
from myapp.models import Account


def loaded(account_id):
    account = Account.objects.get(pk=account_id)

    return {"id": account.pk, "name": account.name}
```

## 📬 Reading from an asynchronous view

```python
from django.http import JsonResponse

from myapp.cache import accounts
from myapp.models import Account


async def read_account(request, account_id):
    async def loaded():
        account = await Account.objects.aget(pk=account_id)

        return {"id": account.pk, "name": account.name}

    return JsonResponse(await accounts.fetch(str(account_id), loaded))
```

**The ORM from an asynchronous producer needs its `a`-prefixed calls** — `aget`, `acreate`, `asave`.
The synchronous ones raise `SynchronousOnlyOperation` there, and the wrapper is a coroutine either way.

## 🗑️ Invalidating on a save

```python
from django.db import transaction

from myapp.bridge import waited
from myapp.cache import accounts


def rename(account, name):
    account.name = name
    account.save(update_fields=["name"])

    transaction.on_commit(lambda: waited(accounts.drop(str(account.pk))))
```

**Drop on commit and never before it.** A drop that happens inside the transaction leaves a window
where another request reads the old row, computes the old value and caches it — and the commit that
follows never touches the cache again.

## 🧹 The janitor as a management command

```python
# myapp/management/commands/cache_janitor.py
import asyncio

from django.core.management.base import BaseCommand

from cachefy.janitor import Janitor

from myapp.cache import app


class Command(BaseCommand):
    help = "sweeps what the cache no longer holds"

    def handle(self, *arguments, **options):
        asyncio.run(self.sweep())

    async def sweep(self):
        await app.setup()
        await Janitor(app).run()
```

```bash
python manage.py cache_janitor
```

Run one of them per fleet, beside the web processes. Ask it to stop on whatever signal your process
manager sends and it returns from its wait at once.

## ⚙️ Building the store on deploy

```bash
python manage.py shell -c "import asyncio; from myapp.cache import app; asyncio.run(app.setup())"
```

Only `SqlAlchemyStore` builds anything, and only once. Do it as a deploy step rather than from every
worker at boot — several processes running the same DDL in the same instant is the one race a fresh
database has.

## 🔀 The two databases stay two databases

If you point `SqlAlchemyStore` at the database Django already uses, the cache still keeps its entries
in a table of its own, under metadata of its own, and never looks at your models. It is one connection
pool beside Django's and not inside it, which is what keeps a polling sweep from starving request
traffic.

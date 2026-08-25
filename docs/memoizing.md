# 🧠 Memoizing

Caching what a function answered, keyed by the arguments each call was made with.

```python
from datetime import timedelta

from myapp.cache import app
from myapp.database import load_account


@app.cached("profile", ttl=timedelta(minutes=5))
async def profile(user_id: int) -> dict:
    return await load_account(user_id)


await profile(7)
```

The decorator declares a space of its own, so `profile` owns every name it writes and nothing else
shares them.

## 🔑 What names a call

The arguments are bound to the signature and written down sorted, so the same call spelled either way
is the same name:

```python
await profile(7)
await profile(user_id=7)
```

Both are `{"user_id":7}`. A call too long to write out is named by its digest instead, drawn the same
way in every process — never by `hash`, which Python salts per process and which would name an entry
only the process that wrote it could ever read.

**A call nothing could write down is refused**, with a sentence saying so. Pass what identifies the
call, or name it yourself:

```python
from myapp.cache import app


@app.cached("profile", key=lambda account: str(account.id))
async def profile(account) -> dict:
    return {"id": account.id}
```

A method is the same thing, and the one worth showing: the lambda is handed `self` exactly as the
method is, so what names the call has to say which instance it was made on — or leave it out, when
every instance would answer the same.

```python
from myapp.cache import app


class Accounts:
    def __init__(self, tenant: str) -> None:
        self.tenant = tenant

    @app.cached("accounts", key=lambda self, account_id: f"{self.tenant}:{account_id}")
    async def profile(self, account_id: int) -> dict:
        return {"id": account_id}
```

Left to name itself, that call is refused: `self` is not something a store can write down, and naming
it by the object would name one entry per instance the process happens to build.

A classmethod is written with `@classmethod` on the **outside**, because what this decorator memoizes
has to be the function itself:

```python
from myapp.cache import app


class Reports:
    @classmethod
    @app.cached("totals", key=lambda cls, month: f"{cls.__name__}:{month}")
    async def totals(cls, month: int) -> str:
        return f"{cls.__name__}-{month}"
```

The other order is refused where the class is written, with a sentence saying which way round it goes.
A staticmethod works either way.

## 🧹 Forgetting

```python
await profile.invalidate(7)
await profile.clear()
```

Invalidating names one call and leaves the others. Clearing forgets every call of the function.

## ♻️ Computing again

```python
await profile.refresh(7)
```

Computes the call again and keeps what it answered, whatever was there. That is what a webhook that
knows a record changed should call.

## 🧭 Reading the name yourself

```python
await profile.key_for(7)
await profile.space.get(profile.key_for(7))
```

## ⚖️ Asynchronous or plain

Both are memoized. A coroutine is awaited and a plain function is called where the caller is:

```python
from myapp.cache import app


@app.cached("squared")
def squared(number: int) -> int:
    return number * number


await squared(4)
```

The wrapper is always a coroutine, because reaching the store is.

> **A plain function runs on the event loop.** Nothing here moves it to a thread, because moving
> somebody's code somewhere they did not ask for it to run is not a decision a cache should make. If
> what it does blocks, make it `async def` and reach for `asyncio.to_thread` yourself.

**A generator is refused where it is declared.** Calling one runs none of its body, so what would be
kept under that name is the generator itself. Handed straight to `fetch` it is refused there too, for
the same reason and with the same sentence.

## 🧊 What a memoized call is worth

Everything on [Stampede](stampede.md) applies: twenty callers of one cold call compute it once between
them, and a `stale` on the space lets one caller recompute while everybody else is served what is
already there.

```python
from datetime import timedelta

from myapp.cache import app
from myapp.database import summarize


@app.cached("report", ttl=timedelta(hours=1), stale=timedelta(minutes=5))
async def report(day: str) -> dict:
    return await summarize(day)
```

That report is never recomputed by more than one caller, and no caller ever waits for it after the
first hour.

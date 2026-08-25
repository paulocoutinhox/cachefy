# 🪝 Hooks

Being told about every hit, every miss and everything a store could not answer. A cache with no hit
rate is a cache nobody can tell has stopped working.

```python
from myapp.cache import app


@app.on_hit
def hit(space, key):
    metrics.increment("cache.hit", tags={"space": space})


@app.on_miss
def miss(space, key):
    metrics.increment("cache.miss", tags={"space": space})


@app.on_error
def broke(what, failure):
    metrics.increment("cache.error", tags={"doing": what})
```

| Hook | Called with | When |
| --- | --- | --- |
| `on_hit` | the space and the key | a name held something |
| `on_miss` | the space and the key | a name held nothing |
| `on_error` | what was being done, and what broke | a call the store could not answer |

## 📊 What each one is worth

**The miss rate is the number to watch.** A store that went away looks exactly like a cache that
suddenly stopped hitting, which is why it is the first place an outage shows.

**A name holding `None` is a hit.** That is the whole reason a value and a name holding nothing are
told apart here.

**Reading many names tells the listeners about each of them**, so a page of twenty that found twelve
is twelve hits and eight misses and never one of either.

**What `on_error` is told** is a short sentence naming the call and the name it was for, such as
`read 'users:42'` or `sweep what has died`, and the exception itself. Everything the guard answers for
goes through it — see [Resilience](resilience.md).

**It is also where a traceback comes from.** A store failure is logged as one line without one, because
this runs on every request. The hook is handed the exception, so a listener that wants the whole thing
can have it:

```python
import logging

from myapp.cache import app


@app.on_error
def broke(what, failure):
    logging.getLogger("myapp").warning("the cache could not %s", what, exc_info=failure)
```

## ⚙️ What a listener may be

Either a plain function or a coroutine. A coroutine is awaited.

```python
from myapp.cache import app


@app.on_miss
async def remember(space, key):
    await audit.write(space, key)
```

## 🛡️ A listener that breaks breaks alone

A metric that fails must never take the value a caller asked for with it. Anything a listener raises is
logged and the call carries on, including a library calling `sys.exit` deep inside one.

The single exception is a cancellation, which is passed on — swallowing that would leave a loop nobody
can stop.

**Every listener is told even when one before it broke**, so an audit trail is not lost because a
metric was down.

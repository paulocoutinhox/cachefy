# 🛟 Resilience

A cache is what makes an application faster and never what makes it work. Everything here follows from
that one sentence.

## 🚦 The line

There are two kinds of failure and they are answered in opposite ways.

| What went wrong | What happens |
| --- | --- |
| the store is unreachable, refused the call, or went away halfway through | logged, told to the listeners, and answered as a miss |
| the caller asked for something no store could ever hold | refused where it was written, with a sentence saying why |

The first is a bad minute and the second is a bug. A cache that raised on the first would take an
application down for something that was only ever an optimisation, and one that stayed quiet about the
second would hide a mistake nothing else would ever surface.

## 🕳️ What a missing store answers

Every call answers, and none of them raises:

| Call | With no store at all |
| --- | --- |
| `get` | the default, which is `MISS` unless you gave another |
| `entry` | nothing at all |
| `get_many` | an empty answer |
| `set`, `add`, `swap` | nothing at all, meaning the value was not kept |
| `drop`, `touch` | false |
| `incr` | nothing at all |
| `clear`, `count` | zero |
| `fetch` | the value, computed here and now |

The last row is the one that matters. A store nobody can reach never makes a caller wait on a name
nothing can tell it about — it computes the value and hands it back, exactly as it would on a miss.

```python
from myapp.cache import users
from myapp.database import load_account

# With redis down, this is still a working function. It is only a slower one.
profile = await users.fetch("42", lambda: load_account(42))
```

## 📣 Knowing it is happening

A cache that silently stopped caching is a cache nobody notices has stopped. Every failure is logged
under the `cachefy` logger and told to whoever is listening:

> **What a store failure writes down is one short line, and never a traceback.** This is on the path of
> every request, so an outage would otherwise write one per call — measured at a thousand reads against
> a store that was gone: nine thousand lines and four hundred kilobytes, which drowns a log pipeline at
> the one moment anybody needs it. The line names the call, the entry, the failure and what it said.
> Whoever wants the traceback registers `on_error`, which is handed the failure itself.

> **A key is written down and a value never is.** The name is what makes a line worth reading, so it
> goes into the log and into the hook — which means a key is not the place for a secret. What a caller
> stored is never written down anywhere.

> **A listener that raises keeps its traceback**, because a store failing is a state this expects and a
> listener raising is a bug. Only one of the two needs pointing at.

```python
from myapp.cache import app


@app.on_error
def broke(what, failure):
    metrics.increment("cache.error", tags={"doing": what})
```

Read [Hooks](hooks.md) for the rest, and watch the miss rate: a store that went away looks exactly
like a cache that suddenly stopped hitting.

## 🧯 What is still raised

**Building the store.** Coming up is not serving a request, and a process that could not build what it
needs must never come up pretending it did:

```python
from myapp.cache import app

await app.setup()
```

**Anything the calling code got wrong**, refused where it happens:

- a key or a space no store could tell one entry from another by
- a value no store could write down, or one past what a store keeps one as
- a lifetime, freshness or lease that could never work
- an amount a counter could not be moved by, or a version no entry could carry
- a producer that could never be called, or one asking for the very name it is computing
- a sweep batch no statement could name

**Whatever a producer raises, when there was nothing to serve instead.** That is your own code and your
request has to see it. A refresh that broke over a value that was only stale is answered with that
value — see [Stampede](stampede.md).

## 🔌 Timeouts are yours to set

**A client with no timeout is the one thing this cannot answer for.** The library awaits the store; if
the store never answers, the await never returns. Both clients wait forever by default, so a network
that went away leaves a request hanging on a cache rather than degrading past it.

```python
from redis.asyncio import Redis

Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1, health_check_interval=30)
```

```python
from sqlalchemy.ext.asyncio import create_async_engine

create_async_engine(url, pool_pre_ping=True, pool_size=20, pool_timeout=1, connect_args={"command_timeout": 1})
```

Pick a timeout shorter than what the value costs to compute. A cache read that takes longer than the
query it was saving is worse than no cache at all.

**Size the pool, and give it a timeout too.** A cache is read on every request, so its pool running out
is likelier than its network going away — and SQLAlchemy waits **thirty seconds** by default before it
says so. Measured: with the pool held, a read waits out `pool_timeout` exactly and then degrades to a
miss with the failure told to the hooks. A second is plenty: a cache that has to queue for a connection
is one that has stopped being faster than the thing behind it.

## 🧪 How this is kept true

The suite has a store that refuses every call, one pointed at a port nothing is listening on, and one
that goes away halfway through a call. Every public call is asked against all three, and none of them
is allowed to raise. There is also a sweep that cuts one round trip of one call at a time and reads
what that call left behind — see [Contribution](contribution.md).

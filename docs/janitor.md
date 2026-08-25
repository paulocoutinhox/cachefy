# 🧹 Janitor

A cache nobody sweeps is a table that only ever grows. The janitor drops what has died.

```python
import asyncio

from cachefy.janitor import Janitor

from myapp.cache import app

janitor = Janitor(app)
sweeping = asyncio.create_task(janitor.run())
```

| Argument | Default | What it means |
| --- | --- | --- |
| `every` | 5 minutes | how long it waits when the last sweep found nothing left to drop |
| `batch` | 500 | how many dead entries one sweep drops, at most the 1000 one statement may name |

## 🔁 What a pass does

One pass drops a batch of what was already dead, oldest death first. **A full batch is followed at
once** rather than after the interval, so a week that was never swept is caught up over passes instead
of over a week.

Nothing a store does ends the loop. A store that is unreachable is logged, told to the
[hooks](hooks.md), and the janitor waits and asks again.

## 🛑 Stopping it

```python
janitor.stop()
await sweeping
```

A janitor is asked to stop once and is finished: `run` answers at once from then on, because what stops
it is what the lifespan sets on the way out. Build a new one to sweep again.

Asking it to stop returns from the wait at once rather than after the interval, so a deploy is not held
for five minutes by a process that had nothing to do.

Under an asgi framework there is a lifespan that does all of this — see [Frameworks](frameworks.md).

## 📐 Sizing it

The batch is what one sweep holds the store for, and the interval is how often that happens. The
default pair drops six thousand entries an hour without ever holding anything for long, and a full
batch is followed at once — so a burst is caught up at the speed of the store rather than of the clock.

A cache that dies faster than that wants a shorter interval:

```python
from datetime import timedelta

from cachefy.janitor import Janitor

from myapp.cache import app

Janitor(app, every=timedelta(seconds=30), batch=1000)
```

> **A batch is at most a thousand names**, because that is what one statement may name. Measured:
> PostgreSQL refuses a statement naming about twenty thousand while SQLite and MySQL take it, so a
> bigger batch is one that works on two stores and raises on the third. Ask for a shorter interval
> instead — nothing is lost, because a sweep that filled its batch never waits one out.

**One janitor per fleet is enough**, and more than one is harmless — a sweep is a conditional delete
like everything else here, so two of them running together drop different rows and neither answers for
what the other took.

**Run it where the clock is right.** A sweep drops what died before the instant it was given, and that
instant comes from the machine it runs on. A janitor an hour fast decides an hour of living entries are
already dead and takes them, and one an hour slow sweeps an hour late. What that costs is the entry and
the recompute behind it, never a wrong value: every reader still compares the field to its own instant,
so nothing is ever answered with something it should not have been.

## 🔒 What a released name leaves behind

A caller computing a value holds a name, and letting it go writes one that has already died rather
than taking it away — because taking it away would let a holder whose lease had run out remove the
name the holder after it is on.

So a cache that misses often leaves a dead row per miss. **The sweep drains those as fast as the store
answers**, because a full batch is followed at once rather than after the interval. That is also why a
cache whose entries never expire still wants a janitor.

## 🟥 Redis still wants one

Redis gives the memory of a dead entry back on its own, because every entry is written with the expiry
that matches the instant it dies at. What it does not give back is the listing the entry was named in
and the index a sweep reads, which are what make clearing a space and counting one exact.

So run a janitor there too. It has less to do, and what it does is cheap.

# 🐘 Stampede

A cold name that twenty requests want at the same instant is twenty copies of the same expensive
query. That is what `fetch` exists to stop.

```python
from myapp.cache import users
from myapp.database import load_account

value = await users.fetch("42", lambda: load_account(42))
```

## 🎯 What happens

1. The name is read. Something there and still fresh is the answer, and nothing else happens.
2. Otherwise exactly one caller **takes the name** by writing one nothing else can write over while it is held.
3. That caller runs the producer, writes what it answered, and lets the name go.
4. Every other caller is served what is already there, or waits for the value the holder is about to write.

Taking the name is the same conditional write everything else here is built on: it lands only while
the name is free or what held it has already died, and the store is what decides the winner.

## 🍞 A value that is stale but still there

```python
from datetime import timedelta

from myapp.cache import app

users = app.space("users", ttl=timedelta(minutes=5), stale=timedelta(minutes=1))
```

A value past its freshness is still a value. One caller takes the name and computes a new one, and
**everybody else is served the old one while it does** — nobody waits, and the expensive call happens
once a minute rather than once a request.

That is the shape most read-heavy caches want. Give a value a long lifetime and a short freshness and
no caller ever waits for it after the first one.

## ⏱️ A caller that waits

A caller that lost the name and has nothing to be served asks again every twenty milliseconds until
the value lands. It waits no longer than the space's `lease`, and then computes the value itself.

That bound is what makes a process killed mid computation cost one lease and never a request that
hangs. Set the lease past how long the producer really takes:

```python
from datetime import timedelta

from myapp.cache import app

reports = app.space("reports", ttl=timedelta(hours=1), lease=timedelta(minutes=2))
```

## 🔥 What a producer may do

**Whatever it likes, and what it raises reaches the caller.** A producer is your own code, so a
database that refused a query is a failure your request has to see. The name is let go of on the way
out, so the next caller is not left waiting on work that already failed.

**It may ask the cache for any other name**, and that nests as deep as you like. What it may not do is
ask for **the name it is computing** — it holds that one itself, so waiting for it would be waiting out
a whole lease for a value nobody else is ever going to write. That is refused at once, with a sentence
saying so, rather than left to look like a slow request.

**Unless there was a value to serve.** A refresh that broke while a stale value was already there is
logged, told to the [hooks](hooks.md), and answered with what is there. The value is old, the request
worked, and the failure was recorded — which is what you want from a cache when the thing behind it is
having a bad minute.

**A refusal is never answered around that way.** Anything this library refuses is a mistake in the
calling code, not a bad minute, so it is raised even when a stale value was sitting right there —
otherwise a bug would be served a value and never reported at all.

The one thing that is not raised is an answer no store could write down. The value was computed and
the caller asked for the value, so it is handed back uncached and the failure goes to the
[hooks](hooks.md). A cache must never be the reason a request fails.

## 🧮 What it does not promise

**Exactly once is not what a lease can give you.** A holder whose process is killed leaves the name
held until the lease runs out, and the caller that takes it then computes the value a second time.
That is the same at-least-once boundary every distributed lock has, and it is why a producer should be
something you can run twice: a read, a query, a render — and never a charge or a send.

If the work behind the name must happen once and only once, it is not a cache producer. It belongs
wherever your application already writes down that it happened.

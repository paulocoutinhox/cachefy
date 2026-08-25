# 📁 Spaces

A space is a family of names and the policy every entry of it is written under. It is the one gate a
policy passes through, so anything that could never work is refused where you declare it.

```python
from datetime import timedelta

from myapp.cache import app

users = app.space("users", ttl=timedelta(minutes=5), stale=timedelta(minutes=1))
limits = app.space("limits", ttl=timedelta(minutes=1))
```

| Argument | What it means |
| --- | --- |
| `ttl` | how long a value written into this space is kept, and nothing at all for one kept until somebody drops it |
| `stale` | how long a value stays worth serving without a refresh, which has to be shorter than `ttl` |
| `lease` | how long one caller may hold a name while it computes, which is also the longest anybody waits on it |

Every span is a `timedelta` and never a number of seconds. A plain `60` written where a minute was
meant is refused with a sentence saying so, because nothing further down could tell.

## 🔤 What a name may be

A name is the space and the key together, and it is the only thing that tells one entry from another.

- A key is text, at most **255 characters**, and never empty.
- A space is text, at most **64 characters**, and may not hold a colon — that is what joins the two halves wherever the entries live.
- Neither may hold a nul byte or a lone surrogate, because no store agrees on what to do with one.
- Case and accents are compared code point by code point, so `user:Bob` and `user:bob` are two entries in every store.
- **A name is judged and held by the text it holds, never by the object it is.** The rules above are read off that text, so a subclass answering its own length or hiding what it carries is refused by what a store would really receive. A `str` subclass is welcome — an `Enum` whose value is text is how a lot of code names things — and it is settled to plain text before a store ever sees it, so a name that renders as one thing and holds another is still found by what it holds.

```python
from enum import Enum


class Space(str, Enum):
    USERS = "users"

users = app.space(Space.USERS, ttl=timedelta(minutes=5))

await users.set("42", {"name": "Paulo"})
await app.space("users", ttl=timedelta(minutes=5)).get("42")  # {'name': 'Paulo'} — one name, two spellings
```

## 📖 Reading

```python
from cachefy.entry import MISS

value = await users.get("42")
value = await users.get("42", default=None)
```

A name that holds nothing answers `MISS`. That is not the same thing as a name holding `None`, and
telling the two apart is what lets a function that legitimately answers nothing be cached at all.

**Many names in one round trip:**

```python
found = await users.get_many(["42", "43", "44"])
```

The answer holds only the names that held something. Ask for as many as you like — they are read in
batches no statement would refuse.

**The whole entry**, for a caller that means to write a value back:

```python
held = await users.entry("42")
held.value, held.version, held.expires_at
```

## ✍️ Writing

```python
await users.set("42", {"name": "Paulo"})
await users.set("42", {"name": "Paulo"}, ttl=timedelta(hours=1))
await users.set("42", {"name": "Paulo"}, ttl=None)
```

A call that says nothing about a lifetime takes the one the space was declared with. One that says
`ttl=None` keeps the value until somebody drops it.

**Only while the name holds nothing:**

```python
if await users.add("42", "mine") is not None:
    ...
```

**Only while nobody wrote in between**, which is what a read, a change and a write back need:

```python
held = await users.entry("42")
changed = dict(held.value, visits=held.value["visits"] + 1)

if await users.swap("42", changed, held.version) is None:
    ...
```

A version travels with every entry, and no two writes of one name ever carry the same one. A slow writer
that read a value, worked something out and came back to write it is refused rather than allowed to drop
what landed while it was thinking — and it is refused even when the value it read died and a completely
different one took its place, which is the case a version counted per name would have let through.

## 🗑️ Forgetting

```python
await users.drop("42")
await users.clear()
```

Dropping one name answers whether it held anything. Clearing a space drops everything it holds in one
step and answers how much that was.

> **Clearing walks every name the space holds.** That is what makes the answer exact, and it is also
> why a space with a million names in it is one to invalidate by shortening its lifetime instead.

## ⏳ Moving when a value dies

```python
await users.touch("42", ttl=timedelta(hours=1))
```

Moving the instant a living value dies at, without touching what it holds. A name that holds nothing
answers `False`.

## 🔢 Counting

```python
seen = await limits.incr("203.0.113.7")
```

One atomic step in every store, so a rate limit two callers each read as one is not something that can
happen here. It answers the count after adding, and **nothing at all** when the name holds something
that is not a count.

**The window starts on the first call.** The lifetime is taken where the count is created and left
alone on every call after it, because that is what a rate limit means.

A count runs from `-(2**53 - 1)` to `2**53 - 1`, which is what every store adds up exactly.

## 📏 Watching it

```python
depth = await users.count()
```

How many living entries the space holds. It is a number for an operator and never for a hot path — on
Redis it walks what the space listed.

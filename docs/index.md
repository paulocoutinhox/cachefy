# ⚡ Cachefy

An asynchronous cache for Python, built around one row.

## 💡 The one idea

Everything this library does reduces to **a name that holds a value until an instant**.

A read is that row before that instant. A write replaces it. A miss is that row absent or past its
instant. Memoizing a function, holding a name so exactly one caller computes, and counting a rate
limit are all the same row under different values.

| Question | Answer |
| --- | --- |
| what tells one entry from another? | the space it belongs to and the key it was written under, and nothing else |
| what stops twenty callers all computing the same cold value? | a name exactly one of them takes, and a value the rest are handed |
| what happens when the store is unreachable? | a read is a miss, a write is dropped, and neither is ever raised at the caller |
| what happens when the value cannot be written? | it is refused where it is written, because that is a bug in the calling code |

## 🚧 What it does not do

- **It does not make your application work.** A cache makes it faster. Everything here is written so that losing the cache costs speed and never correctness.
- **It is not a database.** A value is bounded, entries have no relations and nothing is queried by anything but its name.
- **It does not guess your invalidation.** Nothing watches your tables. You drop what you changed, or you give it a lifetime.

## 🧭 Where to go next

Start at [Getting started](getting-started.md), then read [Spaces](spaces.md) for lifetimes and every
call a space answers.

After that, in whatever order the question comes up:

| Page | When you want it |
| --- | --- |
| [Memoizing](memoizing.md) | caching what a function answered, keyed by its arguments |
| [Stampede](stampede.md) | one caller computes, and what everybody else is served |
| [Resilience](resilience.md) | what happens when the store is gone |
| [Stores](stores.md) | Redis, a database, memory, and writing your own |
| [Janitor](janitor.md) | sweeping what has died |
| [Hooks](hooks.md) | hit rate, miss rate and every call a store could not answer |
| [Frameworks](frameworks.md) | running it under FastAPI, Django, Flask or nothing at all |
| [Contribution](contribution.md) | the suite, the servers it wants, and what a change is asked for |

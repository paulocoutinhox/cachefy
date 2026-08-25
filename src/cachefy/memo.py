import inspect
from functools import update_wrapper
from typing import Callable

from cachefy.keys import built, named
from cachefy.space import DECLARED, Space


class Memo:
    """A function whose answers live in a space of their own, remembered under the arguments each call was made with."""

    def __init__(self, space: Space, handler: Callable, key: Callable | None) -> None:
        # What is wrapped is copied first, because `update_wrapper` carries the wrapped function's own attributes over and would write across the ones below.
        update_wrapper(self, handler)

        self.space = space
        self.handler = handler
        self.naming = key
        self.signature = inspect.signature(handler)

    def key_for(self, *arguments, **keywords) -> str:
        """Answers the name this call is remembered under."""
        if self.naming is not None:
            return named(self.naming(*arguments, **keywords))

        # The arguments are bound to the signature first, so the same call spelled positionally and by keyword is the same name.
        bound = self.signature.bind(*arguments, **keywords)
        bound.apply_defaults()

        return built(dict(bound.arguments))

    async def __call__(self, *arguments, **keywords):
        """Answers what this call is remembered as, computing it once however many callers ask at the same instant."""
        return await self.space.fetch(self.key_for(*arguments, **keywords), lambda: self.handler(*arguments, **keywords))

    async def refresh(self, *arguments, **keywords):
        """Computes this call again and keeps what it answered, whatever was there."""
        living, fresh = self.space.spans(DECLARED, DECLARED)

        return await self.space.produced(self.key_for(*arguments, **keywords), lambda: self.handler(*arguments, **keywords), living, fresh)

    async def invalidate(self, *arguments, **keywords) -> bool:
        """Forgets what this call answered, so the next one computes it again."""
        return await self.space.drop(self.key_for(*arguments, **keywords))

    async def clear(self) -> int:
        """Forgets every call of this function, and answers how much that was."""
        return await self.space.clear()

    def __get__(self, instance, owner):
        """Answers this memo as it was reached, which is what carries the instance into a call on a method."""
        # Without this a memoized method is an attribute rather than a method, so the first argument of every call is bound to `self` and the last one is missing.
        if instance is None:
            return self

        return Bound(self, instance)


class Bound:
    """One memoized method seen through the instance it was reached on, which every call is made with."""

    def __init__(self, memo: Memo, instance) -> None:
        update_wrapper(self, memo.handler)

        self.memo = memo
        self.instance = instance

    def key_for(self, *arguments, **keywords) -> str:
        """Answers the name this call is remembered under."""
        return self.memo.key_for(self.instance, *arguments, **keywords)

    async def __call__(self, *arguments, **keywords):
        """Answers what this call is remembered as, computing it once however many callers ask at the same instant."""
        return await self.memo(self.instance, *arguments, **keywords)

    async def refresh(self, *arguments, **keywords):
        """Computes this call again and keeps what it answered, whatever was there."""
        return await self.memo.refresh(self.instance, *arguments, **keywords)

    async def invalidate(self, *arguments, **keywords) -> bool:
        """Forgets what this call answered, so the next one computes it again."""
        return await self.memo.invalidate(self.instance, *arguments, **keywords)

    async def clear(self) -> int:
        """Forgets every call of this function, and answers how much that was."""
        return await self.memo.clear()

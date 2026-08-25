class CacheError(Exception):
    """Raised for anything this library refuses, so a caller can tell its failures from the ones of the code it caches."""


class UnknownSpace(CacheError):
    """Raised when a space nobody declared is asked for."""


class UnwritableValue(CacheError):
    """Raised when a value no store could write down is handed to the cache."""

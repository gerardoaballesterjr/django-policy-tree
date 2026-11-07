"""
Caching that never outlives a rolled-back change.

A policy or role change clears the shared cache at once, and again when its
transaction commits. Until then, data read inside that transaction is kept
only for that transaction (it may still roll back), never in the shared cache.
Under manual transaction management (autocommit off) nothing is cached.
Data is always read from the database policies are written to, never a replica.
"""
import django
from django.core.cache import caches
from django.db import connections, router, transaction

from .conf import PolicyTreeSettings


class _Pending:
    """Registered with on_commit while a change is uncommitted; holds that transaction's reads."""

    def __init__(self):
        self.keys = set()
        self.memo = {}

    def __call__(self):
        SafeCache.delete(*self.keys)


class SafeCache:
    @staticmethod
    def _alias():
        from .models import Policy

        return router.db_for_write(Policy)

    @classmethod
    def database(cls):
        """The database to read policies and roles from (the one they're written to)."""
        return cls._alias()

    @staticmethod
    def _cache():
        return caches[PolicyTreeSettings.cache_alias()]

    @classmethod
    def _pending(cls, connection):
        if not connection.in_atomic_block:
            return None
        for entry in connection.run_on_commit:
            if isinstance(entry[1], _Pending):
                return entry[1]
        return None

    @classmethod
    def get(cls, key, build):
        connection = connections[cls._alias()]
        if not connection.in_atomic_block and not connection.get_autocommit():
            return build()  # manual transaction: may still roll back, so don't share it
        pending = cls._pending(connection)
        if pending is not None:
            # Uncommitted changes: remember reads for this transaction state only.
            memo_key = (key, tuple(connection.savepoint_ids))
            if memo_key not in pending.memo:
                pending.memo[memo_key] = build()
            return pending.memo[memo_key]
        cache = cls._cache()
        data = cache.get(key)
        if data is None:
            data = build()
            cache.set(key, data, PolicyTreeSettings.cache_timeout())
        return data

    @classmethod
    def delete(cls, *keys):
        cache = cls._cache()
        for key in keys:
            cache.delete(key)

    @classmethod
    def changed(cls, *keys):
        """Call after any change that affects cached data."""
        cls.delete(*keys)
        alias = cls._alias()
        connection = connections[alias]
        if not connection.in_atomic_block:
            return
        pending = cls._pending(connection)
        if pending is None:
            pending = _Pending()
            if django.VERSION >= (4, 2):
                # robust: an earlier failing callback must not skip this clear
                transaction.on_commit(pending, using=alias, robust=True)
            else:
                transaction.on_commit(pending, using=alias)
        pending.keys.update(keys)
        pending.memo.clear()

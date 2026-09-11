"""A session lock prevents multiple workers from operating the same account."""
import contextlib
import hashlib

from django.conf import settings
from django.core.management.base import CommandError


@contextlib.contextmanager
def single_worker(name):
    database = settings.DATABASES["default"]
    if "postgresql" in database["ENGINE"]:
        import psycopg
        key = int.from_bytes(hashlib.sha256(("striangle:"+name).encode()).digest()[:7], "big")
        with psycopg.connect(dbname=database["NAME"], user=database.get("USER"), password=database.get("PASSWORD"),
                            host=database.get("HOST"), port=database.get("PORT") or 5432, autocommit=True,
                            **{k: v for k, v in database.get("OPTIONS", {}).items() if k in ("sslmode", "sslrootcert")}) as conn:
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
                raise CommandError(f"Another {name} worker owns the lock")
            yield lambda: conn.execute("SELECT 1").fetchone()
    else:
        import fcntl
        with open(settings.BASE_DIR / f"{name}.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError(f"Another {name} worker owns the local lock") from None
            yield lambda: True

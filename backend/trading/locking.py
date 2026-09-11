"""A session lock prevents multiple workers from operating the same account."""
import contextlib
import hashlib
import logging
import time

from django.conf import settings
from django.core.management.base import CommandError

log = logging.getLogger(__name__)


@contextlib.contextmanager
def single_worker(name, stop=None):
    database = settings.DATABASES["default"]
    if "postgresql" in database["ENGINE"]:
        import psycopg
        key = int.from_bytes(hashlib.sha256(("striangle:"+name).encode()).digest()[:7], "big")
        with psycopg.connect(dbname=database["NAME"], user=database.get("USER"), password=database.get("PASSWORD"),
                            host=database.get("HOST"), port=database.get("PORT") or 5432, autocommit=True,
                            **{k: v for k, v in database.get("OPTIONS", {}).items() if k in ("sslmode", "sslrootcert")}) as conn:
            # Render starts the replacement before terminating the old instance.
            # Keep the replacement alive but idle until ownership is released.
            deadline, waiting = time.monotonic()+420, False
            while True:
                if stop is not None and stop.is_set():
                    raise SystemExit(0)
                if conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]:
                    break
                if time.monotonic() >= deadline:
                    raise CommandError(f"Another {name} worker still owns the lock after 420 seconds")
                if not waiting:
                    log.info("worker=%s status=waiting_for_ownership", name)
                    waiting = True
                if stop is not None:
                    stop.wait(1)
                else:
                    time.sleep(1)
            log.info("worker=%s status=ownership_acquired", name)
            yield lambda: conn.execute("SELECT 1").fetchone()
    else:
        import fcntl
        with open(settings.BASE_DIR / f"{name}.lock", "a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError(f"Another {name} worker owns the local lock") from None
            yield lambda: True

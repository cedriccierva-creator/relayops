"""Run from backend/: python worker.py. Stop with Ctrl+C or SIGTERM."""
from app.file_permissions import initialize_file_permissions

# Process startup: before imports that create database directories or files.
initialize_file_permissions()

import asyncio
import fcntl
import logging
import os
import signal
import time
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.delivery import run_once


async def serve(settings, heartbeat=None):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    print("RelayOps worker ready; Ctrl+C or SIGTERM stops after the current attempt.", flush=True)
    while not stop.is_set():
        try:
            worked = await run_once(SessionLocal, settings)
            if heartbeat is not None:
                heartbeat()
            if worked:
                continue
        except Exception:
            # Never print arbitrary transport exceptions, URLs, payloads or credentials.
            logging.error("Worker cycle failed; persisted jobs will be recovered")
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass


def cycle_heartbeat(lock):
    # Invalidate the previous process's signal before initialization/first cycle.
    os.utime(lock.fileno(), (0, 0))
    last_write = None

    def update():
        nonlocal last_write
        now = time.monotonic()
        if last_write is None or now - last_write >= 10:
            os.utime(lock.fileno(), None)
            last_write = now

    return update


def main():
    settings = get_settings()
    parsed = make_url(settings.DATABASE_URL)
    if parsed.get_backend_name() != "sqlite" or not parsed.database or parsed.database == ":memory:":
        raise SystemExit("Phase 1 worker requires a file-backed SQLite database")
    db_path = Path(parsed.database).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # One sending process per SQLite file on this host, including during a long pause.
    # OS releases the lock on crashes. Do not place the database on NFS.
    with Path(str(db_path) + ".worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("A RelayOps worker already owns this database") from None
        heartbeat = cycle_heartbeat(lock)
        init_db()
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        asyncio.run(serve(settings, heartbeat))


if __name__ == "__main__":
    main()

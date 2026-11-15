"""Worker entrypoint process-lock and graceful shutdown tests; no network destinations."""
import fcntl
import os
import select
import subprocess
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


def worker_env(tmp_path):
    return {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'worker.db'}",
            "SECRET_KEY": "worker-test-secret-not-for-production-0123456789",
            "ENVIRONMENT": "production", "DEV_ALLOWED_ORIGINS": "[]"}


def test_second_worker_is_excluded(tmp_path):
    with (tmp_path / "worker.db.worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run([sys.executable, "worker.py"], cwd=BACKEND,
                                env=worker_env(tmp_path), capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "already owns this database" in result.stderr
        assert not (tmp_path / "worker.db").exists()


def test_worker_starts_stops_and_releases_lock(tmp_path):
    process = subprocess.Popen([sys.executable, "worker.py"], cwd=BACKEND,
                               env=worker_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        readable, _, _ = select.select([process.stdout], [], [], 10)
        assert readable, "Worker did not signal readiness"
        assert "worker ready" in process.stdout.readline()
        process.terminate()
        _, errors = process.communicate(timeout=10)
        assert process.returncode == 0, errors
        with (tmp_path / "worker.db.worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_worker_continues_after_unexpected_exception(monkeypatch, caplog):
    import asyncio
    import signal
    import worker

    async def exercise():
        handlers = {}
        calls = 0
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.setdefault(sig, callback))

        async def cycle(factory, settings):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("private payload must not be logged")
            handlers[signal.SIGTERM]()
            return True

        monkeypatch.setattr(worker, "run_once", cycle)
        await asyncio.wait_for(worker.serve(object()), timeout=3)
        assert calls == 2

    asyncio.run(exercise())
    assert "Worker cycle failed" in caplog.text
    assert "private payload" not in caplog.text

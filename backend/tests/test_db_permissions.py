"""Real startup and SQLite permission checks, isolated from the pytest process."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

BACKEND = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux startup policy")


def run_case(tmp_path, code):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path / 'data' / 'app.db'}",
           "SECRET_KEY": "permission-test-secret-not-for-production-0123456789",
           "ENVIRONMENT": "production", "DEV_ALLOWED_ORIGINS": "[]"}
    result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("entrypoint", ["main", "worker"])
@pytest.mark.parametrize("initial", [0o000, 0o022, 0o277])
def test_startup_and_process_scope(tmp_path, entrypoint, initial):
    run_case(tmp_path, f"""
import os, importlib, stat
from pathlib import Path
os.umask({initial})
real_umask = os.umask
calls = []
def guarded(mask):
    # Every policy call must be at least as restrictive as the initial mask.
    assert mask & {initial} == {initial}
    calls.append(mask)
    return real_umask(mask)
os.umask = guarded
app = importlib.import_module({entrypoint!r})
assert calls == [{initial} | 0o077]
from app.db import engine, init_db, make_engine
path = Path(engine.url.database)
assert stat.S_IMODE(path.parent.stat().st_mode) == (0o777 & ~({initial} | 0o077))
if {initial} & 0o700:
    # Preserved owner restrictions can prevent DB initialization; never relax them.
    other = path.parent.parent / 'strict.log'
    other.write_text('test')
    assert stat.S_IMODE(other.stat().st_mode) == (0o666 & ~({initial} | 0o077))
    extra = make_engine('sqlite:///:memory:')
    extra.dispose()
    assert calls == [{initial} | 0o077]
    raise SystemExit(0)
init_db()
assert stat.S_IMODE(path.stat().st_mode) == (0o666 & ~({initial} | 0o077))
# Unrelated files and a SQLite backup inherit the same process-wide policy.
other = path.parent / 'unrelated.log'
other.write_text('test')
assert stat.S_IMODE(other.stat().st_mode) == (0o666 & ~({initial} | 0o077))
import sqlite3
with sqlite3.connect(path) as source, sqlite3.connect(path.parent / 'backup.db') as target:
    source.backup(target)
assert stat.S_IMODE((path.parent / 'backup.db').stat().st_mode) == (0o666 & ~({initial} | 0o077))
extra = make_engine('sqlite:///:memory:')
with extra.connect() as conn:
    conn.exec_driver_sql('SELECT 1')
extra.dispose()
assert calls == [{initial} | 0o077], 'Database operations must not change umask'
""")


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
@pytest.mark.parametrize("existing", [False, True])
def test_real_sidecars_and_existing_data(tmp_path, journal_mode, existing):
    run_case(tmp_path, f"""
import os, sqlite3, stat
from pathlib import Path
path = Path(os.environ['DATABASE_URL'].removeprefix('sqlite:///'))
existing = {existing!r}
os.umask(0)
if existing:
    path.parent.mkdir(mode=0o775)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE proof (value TEXT)')
        conn.execute("INSERT INTO proof VALUES ('preserved')")
    path.chmod(0o644)
import main
from app.db import engine
with engine.connect() as conn:
    conn.exec_driver_sql('PRAGMA journal_mode={journal_mode}')
    if not existing:
        conn.exec_driver_sql('CREATE TABLE proof (value TEXT)')
        conn.commit()
    else:
        assert conn.exec_driver_sql('SELECT value FROM proof').scalar_one() == 'preserved'
        conn.commit()
    conn.exec_driver_sql("INSERT INTO proof VALUES ('new')")
    suffixes = ['-journal'] if {journal_mode!r} == 'DELETE' else ['-wal', '-shm']
    for suffix in suffixes:
        # Linux SQLite copies the DB mode, even under the restrictive startup mask.
        assert stat.S_IMODE(Path(str(path) + suffix).stat().st_mode) == (0o644 if existing else 0o600)
    conn.commit()
assert stat.S_IMODE(path.stat().st_mode) == (0o644 if existing else 0o600)
assert stat.S_IMODE(path.parent.stat().st_mode) == (0o775 if existing else 0o700)
engine.dispose()
""")


@pytest.mark.parametrize("target", ["helper", "main", "worker"])
@pytest.mark.parametrize("status", [
    pytest.param(None, id="unavailable"),
    pytest.param("Name:\tpython\n", id="missing-umask"),
    pytest.param("Umask:\t\n", id="empty-umask"),
    pytest.param("Umask:\t0088\n", id="invalid-octal"),
    pytest.param("Umask:\t1000\n", id="out-of-range"),
])
def test_invalid_status_refuses_startup_before_files_or_umask(tmp_path, target, status):
    run_case(tmp_path, f"""
import importlib, os, sys
from pathlib import Path
from unittest.mock import patch
from app.file_permissions import initialize_file_permissions
directory = Path(os.environ['DATABASE_URL'].removeprefix('sqlite:///')).parent
assert not directory.exists()
assert 'app.db' not in sys.modules
status = {status!r}
read_options = dict(side_effect=OSError('unavailable')) if status is None else dict(return_value=status)
with patch('app.file_permissions.Path.read_text', **read_options) as read, patch('os.umask') as change:
    try:
        if {target!r} == 'helper':
            initialize_file_permissions()
        else:
            importlib.import_module({target!r})
    except RuntimeError as error:
        assert str(error) == 'Cannot read Linux process umask; refusing unsafe startup'
    else:
        raise AssertionError('Must refuse startup')
    read.assert_called_once_with()
    change.assert_not_called()
assert 'app.db' not in sys.modules
assert not directory.exists(), 'Failure must precede database-directory creation'
""")

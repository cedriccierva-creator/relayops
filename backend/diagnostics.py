"""Local operator command; OS file permissions control access. No HTTP endpoint."""
import argparse
import json
import sqlite3

from app.config import get_settings
from app.diagnostics import database_path, delivery_diagnostics, worker_health


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    settings = get_settings()
    try:
        path = database_path(settings)
        health = worker_health(path, settings.WORKER_HEALTH_MAX_AGE_SECONDS)
        deliveries = delivery_diagnostics(path)
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({"error": "Diagnostics unavailable; check database configuration, schema and file permissions"}))
        return 2
    print(json.dumps({"worker": health, "deliveries": deliveries}))
    return 0 if health["status"] == "healthy" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Run `python retention.py --dry-run`; this command never deletes data."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser(description="Read-only payload retention plan; cleanup is not supported.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit read-only mode (also the default)")
    parser.parse_args()
    from app.config import get_settings
    from app.db import SessionLocal
    from app.retention import plan_retention

    print(json.dumps(plan_retention(SessionLocal, get_settings())))


if __name__ == "__main__":
    main()

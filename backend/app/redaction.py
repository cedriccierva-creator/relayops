"""Defense-in-depth for dashboard payloads; original bytes remain private in SQLite."""
import re

SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|api.?key|credential|signature", re.I)


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SENSITIVE.search(key) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value

"""Linux process-startup file policy; never changes existing file modes."""
import os
from pathlib import Path


def initialize_file_permissions():
    """Tighten the process-wide mask without a temporary relaxation.

    Call before application initialization/threads. Linux exposes the mask without
    changing it; portable POSIX umask probing would temporarily replace the mask.
    """
    try:
        status = Path("/proc/self/status").read_text()
        mask = next(line.split()[1] for line in status.splitlines() if line.startswith("Umask:"))
        current = int(mask, 8)
        if not 0 <= current <= 0o777:
            raise ValueError
    except (OSError, StopIteration, IndexError, ValueError):
        raise RuntimeError("Cannot read Linux process umask; refusing unsafe startup") from None
    os.umask(current | 0o077)

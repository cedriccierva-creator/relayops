"""Process-local fixed windows, keyed only by authenticated source UUIDs."""
import math
import time
from collections import OrderedDict
from functools import lru_cache
from threading import Lock


class SourceRateLimiter:
    def __init__(self, limit, window_seconds, max_sources, clock=time.monotonic):
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_sources = max_sources
        self.clock = clock
        self.windows = OrderedDict()
        self.lock = Lock()

    def retry_after(self, source_id):
        """Consume one request; return zero if admitted, otherwise seconds to wait."""
        with self.lock:
            now = self.clock()
            # Entries stay in creation/expiration order, not access order.
            while self.windows and next(iter(self.windows.values()))[0] <= now:
                self.windows.popitem(last=False)
            if source_id not in self.windows:
                if len(self.windows) >= self.max_sources:
                    # Fail closed: evicting active entries would reset their quota.
                    return max(1, math.ceil(next(iter(self.windows.values()))[0] - now))
                self.windows[source_id] = (now + self.window_seconds, 0)
            expires, count = self.windows[source_id]
            if count >= self.limit:
                return max(1, math.ceil(expires - now))
            self.windows[source_id] = (expires, count + 1)
            return 0


@lru_cache(maxsize=1)
def get_ingestion_limiter(limit, window_seconds, max_sources):
    return SourceRateLimiter(limit, window_seconds, max_sources)

"""Small in-process sliding-window limiter for a single Render web instance."""

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    def __init__(self, max_keys: int = 50000):
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._checks = 0

    def check(self, key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
        if limit < 1 or window_seconds < 1:
            raise ValueError("limit and window_seconds must be positive")
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            self._checks += 1
            if self._checks % 1000 == 0:
                expired_keys = []
                for stored_key, stored_events in self._events.items():
                    while stored_events and stored_events[0] <= cutoff:
                        stored_events.popleft()
                    if not stored_events:
                        expired_keys.append(stored_key)
                for expired_key in expired_keys:
                    self._events.pop(expired_key, None)
            if key not in self._events and len(self._events) >= self._max_keys:
                return False, window_seconds
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                retry_after = max(1, int(window_seconds - (now - events[0])) + 1)
                return False, retry_after
            events.append(now)
            return True, 0

    def clear(self):
        with self._lock:
            self._events.clear()
            self._checks = 0

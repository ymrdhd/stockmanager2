"""Browser request policy: host allow-listing, origin checks, CSRF and login throttling."""

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
# Only these prefixes carry business facts; the shell itself is public.
BUSINESS_PREFIXES = ('/api/v1',)


def is_business_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + '/') for prefix in BUSINESS_PREFIXES)


def is_write(method: str) -> bool:
    return method.upper() not in SAFE_METHODS


@dataclass(eq=False)
class Failure:
    count: int = 0
    blocked_until: datetime | None = None


@dataclass
class Throttle:
    """Bound repeated failed logins per client address without persisting anything."""

    max_failed_attempts: int = 10
    lockout_seconds: int = 300
    _failures: dict[str, Failure] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def blocked(self, client: str, *, now: datetime | None = None) -> bool:
        moment = now or datetime.now(UTC)
        with self._lock:
            entry = self._failures.get(client)
            if entry is None or entry.blocked_until is None:
                return False
            if moment >= entry.blocked_until:
                del self._failures[client]
                return False
            return True

    def fail(self, client: str, *, now: datetime | None = None) -> None:
        moment = now or datetime.now(UTC)
        with self._lock:
            entry = self._failures.setdefault(client, Failure())
            entry.count += 1
            if entry.count >= self.max_failed_attempts:
                entry.blocked_until = moment + timedelta(seconds=self.lockout_seconds)

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


def host_allowed(host: str | None, allowed: tuple[str, ...]) -> bool:
    if not host or not allowed:
        return False
    return host.lower() in {item.lower() for item in allowed}


def origin_allowed(origin: str | None, origins: tuple[str, ...]) -> bool:
    """Requests without an Origin header are non-browser clients, which CSRF cannot forge."""
    if not origin:
        return True
    return origin.rstrip('/') in {item.rstrip('/') for item in origins}

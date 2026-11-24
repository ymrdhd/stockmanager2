"""In-memory single-user sessions with explicit expiry and revocation.

Session identifiers and CSRF tokens live only in process memory and in the
browser cookie jar: nothing durable is written, and no secret is logged.
"""

import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SESSION_BYTES = 32
CSRF_BYTES = 32


@dataclass(frozen=True)
class Session:
    identifier: str
    username: str
    csrf: str
    issued_at: datetime
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


@dataclass
class SessionStore:
    """Process-local session registry; a restart deliberately logs everyone out."""

    ttl_seconds: int = 43200
    _sessions: dict[str, Session] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(self, username: str, *, now: datetime | None = None) -> Session:
        moment = now or datetime.now(UTC)
        session = Session(
            identifier=secrets.token_urlsafe(SESSION_BYTES),
            username=username,
            csrf=secrets.token_urlsafe(CSRF_BYTES),
            issued_at=moment,
            expires_at=moment + timedelta(seconds=self.ttl_seconds),
        )
        with self._lock:
            self._sessions[session.identifier] = session
        return session

    def lookup(self, identifier: str | None, *, now: datetime | None = None) -> Session | None:
        if not identifier:
            return None
        moment = now or datetime.now(UTC)
        with self._lock:
            session = self._sessions.get(identifier)
            if session is None:
                return None
            if session.expired(moment):
                del self._sessions[identifier]
                return None
            return session

    def revoke(self, identifier: str | None) -> bool:
        if not identifier:
            return False
        with self._lock:
            return self._sessions.pop(identifier, None) is not None

    def revoke_all(self) -> int:
        with self._lock:
            count = len(self._sessions)
            self._sessions.clear()
            return count

    def purge_expired(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        with self._lock:
            stale = [key for key, value in self._sessions.items() if value.expired(moment)]
            for key in stale:
                del self._sessions[key]
            return len(stale)

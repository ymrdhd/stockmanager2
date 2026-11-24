"""Request-level browser security: host, origin, CSRF, session and declared identity."""

import re
from dataclasses import dataclass
from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from backend.config import AuthConfig
from backend.infrastructure.database import clear_declared_identity, declare_identity
from backend.security.policy import (
    Throttle,
    host_allowed,
    is_business_path,
    is_write,
    origin_allowed,
)
from backend.security.sessions import SessionStore

# Public endpoints stay reachable without a session: liveness and the login flow itself.
PUBLIC_PATHS = frozenset({'/api/v1/health'})
PUBLIC_PREFIXES = ('/api/v1/auth/',)
UUID_PATTERN = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
IDENTITY_HEADER = 'x-database-uuid'
GENERATION_HEADER = 'x-database-generation'


def _refused(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, headers=headers,
                        content={'detail': {'code': code, 'message': message}})


@dataclass
class Security:
    """Everything needed to authenticate requests, kept out of durable storage."""

    auth: AuthConfig
    sessions: SessionStore
    throttle: Throttle
    require_identity: bool = False

    @classmethod
    def build(cls, auth: AuthConfig, *, require_identity: bool) -> 'Security':
        return cls(auth=auth, sessions=SessionStore(ttl_seconds=auth.session_ttl_seconds),
                   throttle=Throttle(max_failed_attempts=auth.max_failed_attempts,
                                     lockout_seconds=auth.lockout_seconds),
                   require_identity=require_identity)


def _public(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


class BrowserSecurity(BaseHTTPMiddleware):
    """Reject forged hosts and origins before any business handler runs."""

    def __init__(self, app: ASGIApp, security: Security) -> None:
        super().__init__(app)
        self.security = security
        self.origins = security.auth.origins()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        clear_declared_identity()
        auth = self.security.auth
        path = request.url.path
        method = request.method
        # Host and Origin are checked on every request, including static assets.
        if not host_allowed(request.headers.get('host'), auth.allowed_hosts):
            return _refused(400, 'host_not_allowed', 'request host is not allowed')
        origin = request.headers.get('origin')
        if not origin_allowed(origin, self.origins):
            return _refused(403, 'origin_not_allowed', 'request origin is not allowed')

        session = self.security.sessions.lookup(request.cookies.get(auth.cookie_name))
        request.state.session = session
        if not is_business_path(path) or _public(path):
            return await call_next(request)
        if session is None:
            return _refused(401, 'unauthenticated', 'authentication required')
        if is_write(method):
            # A same-site cookie alone is not enough: require the per-session token.
            token = request.headers.get(auth.csrf_header)
            if not token or token != session.csrf:
                return _refused(403, 'csrf_failed', 'csrf token missing or invalid')
            identity = self._identity(request)
            if identity is None:
                if self.security.require_identity:
                    return _refused(428, 'identity_required',
                                    'write requests must declare the expected database identity')
            elif isinstance(identity, tuple):
                declared_uuid, declared_generation = identity
                declare_identity(declared_uuid, declared_generation)
            else:
                return _refused(400, 'identity_malformed',
                                'declared database identity is malformed')
        try:
            return await call_next(request)
        finally:
            clear_declared_identity()

    def _identity(self, request: Request) -> tuple[str, int] | None | Literal[False]:
        """Return the declared identity, None when absent, False when malformed."""
        uuid = (request.headers.get(IDENTITY_HEADER) or '').strip()
        generation = (request.headers.get(GENERATION_HEADER) or '').strip()
        if not uuid and not generation:
            return None
        if not UUID_PATTERN.match(uuid) or not generation.isdigit() or int(generation) < 1:
            return False
        return uuid, int(generation)


__all__ = ['Security', 'BrowserSecurity', 'PUBLIC_PATHS', 'PUBLIC_PREFIXES',
           'IDENTITY_HEADER', 'GENERATION_HEADER']

"""Minimal single-user browser authentication: login, session, logout, CSRF."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.config import AuthConfig
from backend.security.passwords import verify_password

router = APIRouter(prefix='/api/v1/auth', tags=['auth'])
MAX_USERNAME = 64


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=MAX_USERNAME)
    password: str = Field(min_length=1, max_length=1024)


def _client(request: Request) -> str:
    return request.client.host if request.client is not None else 'unknown'


def refused(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={'detail': {'code': code, 'message': message}})


@router.post('/login')
def login(request: Request, data: LoginInput):
    """Authenticate one operator; failures are throttled per client address."""
    security = getattr(request.app.state, 'security', None)
    if security is None:
        return refused(503, 'auth_disabled', 'authentication is not configured')
    auth: AuthConfig = security.auth
    client = _client(request)
    if security.throttle.blocked(client):
        return refused(429, 'too_many_attempts', 'too many failed attempts; retry later')
    if data.username != auth.username or not verify_password(auth.password_hash, data.password):
        security.throttle.fail(client)
        return refused(401, 'invalid_credentials', 'invalid credentials')
    security.throttle.reset(client)
    session = security.sessions.issue(auth.username, now=datetime.now(UTC))
    response = JSONResponse(content={'username': session.username, 'csrf': session.csrf,
                                     'expires_at': session.expires_at.isoformat()})
    response.set_cookie(auth.cookie_name, session.identifier, max_age=auth.session_ttl_seconds,
                        httponly=True, secure=auth.cookie_secure, samesite='strict', path='/')
    return response


@router.get('/session')
def current_session(request: Request):
    """Report the active session and hand back the CSRF token for write requests."""
    session = getattr(request.state, 'session', None)
    if session is None:
        return refused(401, 'unauthenticated', 'authentication required')
    return {'username': session.username, 'csrf': session.csrf,
            'expires_at': session.expires_at.isoformat()}


@router.post('/logout')
def logout(request: Request):
    """Revoke the presented session immediately; nothing durable is left behind."""
    security = getattr(request.app.state, 'security', None)
    session = getattr(request.state, 'session', None)
    if security is None or session is None:
        return refused(401, 'unauthenticated', 'authentication required')
    security.sessions.revoke(session.identifier)
    auth: AuthConfig = security.auth
    response = JSONResponse(content={'revoked': True})
    response.delete_cookie(auth.cookie_name, path='/')
    return response

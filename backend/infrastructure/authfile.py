"""Load operator credentials and browser origin policy from a local, untracked file."""

import json
import os
from pathlib import Path

from backend.config import ROOT, AuthConfig

DEFAULT_AUTH_FILE = ROOT / 'data' / 'auth.json'


class AuthFileError(ValueError):
    """The local credential file is missing or malformed; never guess a default."""


def load_auth(file: Path) -> AuthConfig:
    """Read the single-user credential file; secrets stay on disk, mode 0600."""
    file = Path(file)
    if file.is_symlink() or not file.is_file():
        raise AuthFileError(f'auth file missing: {file}')
    try:
        data = json.loads(file.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise AuthFileError(f'auth file unreadable: {exc}') from exc
    if not isinstance(data, dict):
        raise AuthFileError('auth file must contain a JSON object')
    try:
        return AuthConfig(
            username=data['username'],
            password_hash=data['password_hash'],
            allowed_hosts=tuple(data.get('allowed_hosts') or ()),
            allowed_origins=tuple(data.get('allowed_origins') or ()),
            session_ttl_seconds=int(data.get('session_ttl_seconds', 43200)),
            cookie_secure=bool(data.get('cookie_secure', True)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthFileError(f'auth file invalid: {exc}') from exc


def save_auth(file: Path, auth: AuthConfig) -> None:
    """Write the credential file atomically with an owner-only mode."""
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'username': auth.username,
        'password_hash': auth.password_hash,
        'allowed_hosts': list(auth.allowed_hosts),
        'allowed_origins': list(auth.allowed_origins),
        'session_ttl_seconds': auth.session_ttl_seconds,
        'cookie_secure': auth.cookie_secure,
    }
    temp = file.with_name(file.name + '.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, file)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise

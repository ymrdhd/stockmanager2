"""Single-user password hashing with scrypt; no plaintext is ever stored or logged."""

import base64
import hashlib
import hmac
import secrets

ALGORITHM = 'scrypt'
N = 2 ** 14
R = 8
P = 1
SALT_BYTES = 16
KEY_BYTES = 32
# scrypt needs roughly 128*N*R bytes; keep a bounded allowance for the KDF itself.
MAX_MEM = 128 * N * R * 2


class PasswordError(ValueError):
    """Raised when a stored hash cannot be parsed."""


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode('ascii'), validate=True)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=n, r=r, p=p,
                          dklen=KEY_BYTES, maxmem=MAX_MEM)


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Encode a password as scrypt$N$r$p$salt$hash for storage in a local secret file."""
    if not password:
        raise PasswordError('password must not be empty')
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, N, R, P)
    return f'{ALGORITHM}${N}${R}${P}${_b64(salt)}${_b64(key)}'


def verify_password(stored: str, password: str) -> bool:
    """Constant-time verification; returns False instead of leaking hash details."""
    try:
        algorithm, n, r, p, salt, key = stored.split('$')
        if algorithm != ALGORITHM:
            raise PasswordError('unsupported password hash')
        expected = _unb64(key)
        if len(expected) != KEY_BYTES:
            raise PasswordError('invalid password hash length')
        actual = _derive(password, _unb64(salt), int(n), int(r), int(p))
    except (ValueError, PasswordError):
        return False
    return hmac.compare_digest(actual, expected)

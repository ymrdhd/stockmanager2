"""Immutable application settings; credentials never live in code or defaults."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / 'frontend' / 'dist'


class AuthConfig(BaseModel):
    """Single-user authentication and browser request-origin policy."""

    model_config = ConfigDict(frozen=True)

    username: str = Field(min_length=1, max_length=64)
    # scrypt$n$r$p$<salt-b64>$<hash-b64>; produced by backend.security.passwords.
    password_hash: str = Field(min_length=1)
    allowed_hosts: tuple[str, ...] = ('127.0.0.1:9091', 'localhost:9091')
    allowed_origins: tuple[str, ...] = ()
    session_ttl_seconds: int = Field(default=43200, ge=300, le=604800)
    cookie_secure: bool = True
    cookie_name: str = 'sm2_session'
    csrf_cookie_name: str = 'sm2_csrf'
    csrf_header: str = 'x-csrf-token'
    max_failed_attempts: int = Field(default=10, ge=1, le=100)
    lockout_seconds: int = Field(default=300, ge=0, le=86400)

    def origins(self) -> tuple[str, ...]:
        """Explicit origins plus the HTTPS/HTTP forms of every allowed host."""
        hosts = tuple(f'{scheme}://{host}' for host in self.allowed_hosts
                      for scheme in ('https', 'http'))
        return tuple(dict.fromkeys((*hosts, *self.allowed_origins)))


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = "Stock Manager 2.0"
    version: str = "2.0.0-dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    near_stop_bps: int = Field(default=200, ge=0, le=1000)
    market_auto_refresh: bool = True
    market_refresh_seconds: int = Field(default=20, ge=10, le=300)
    # Production entry points pass the built browser application; tests opt out.
    static_dir: Path | None = None
    # Formal launches require every write to declare the expected database identity.
    require_write_identity: bool = False
    auth: AuthConfig | None = None
    # Behaviour without a database is liveness-only; the web shell is advertised anyway.
    web_root_requires_auth: bool = True

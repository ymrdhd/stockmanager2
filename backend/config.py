"""Immutable application settings; no business services are configured yet."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AppConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = "Stock Manager 2.0"
    version: str = "2.0.0-dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    market_auto_refresh: bool = True
    market_refresh_seconds: int = Field(default=20, ge=10, le=300)

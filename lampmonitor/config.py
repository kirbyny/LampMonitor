"""Process-level configuration read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path = Path("./data")
    host: str = "0.0.0.0"
    port: int = 8080
    username: str = "admin"
    password: str | None = None
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lampmonitor.db"

    @classmethod
    def from_env(cls) -> AppConfig:
        env = os.environ
        return cls(
            data_dir=Path(env.get("LAMPMONITOR_DATA_DIR", "./data")),
            host=env.get("LAMPMONITOR_HOST", "0.0.0.0"),
            port=int(env.get("LAMPMONITOR_PORT", "8080")),
            username=env.get("LAMPMONITOR_USERNAME", "admin"),
            password=env.get("LAMPMONITOR_PASSWORD") or None,
            log_level=env.get("LAMPMONITOR_LOG_LEVEL", "INFO").upper(),
        )

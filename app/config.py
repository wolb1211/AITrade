from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8800
    database_type: str = "sqlite"
    database_path: Path = _project_root() / "runtime" / "gainlab_ai.db"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = ""
    mysql_user: str = ""
    mysql_password: str = ""
    demo_deployment_key: str = "gl_demo_pa_key"

    @classmethod
    def from_env(cls) -> "Settings":
        raw_db = os.getenv("GAINLAB_DATABASE_PATH", "runtime/gainlab_ai.db")
        database_path = Path(raw_db)
        if not database_path.is_absolute():
            database_path = _project_root() / database_path
        return cls(
            environment=os.getenv("GAINLAB_ENV", "development"),
            host=os.getenv("GAINLAB_HOST", "127.0.0.1"),
            port=int(os.getenv("GAINLAB_PORT", "8800")),
            database_type=os.getenv("GAINLAB_DATABASE_TYPE", "sqlite").strip().lower(),
            database_path=database_path,
            mysql_host=os.getenv("GAINLAB_MYSQL_HOST", "127.0.0.1"),
            mysql_port=int(os.getenv("GAINLAB_MYSQL_PORT", "3306")),
            mysql_database=os.getenv("GAINLAB_MYSQL_DATABASE", ""),
            mysql_user=os.getenv("GAINLAB_MYSQL_USER", ""),
            mysql_password=os.getenv("GAINLAB_MYSQL_PASSWORD", ""),
            demo_deployment_key=os.getenv(
                "GAINLAB_DEMO_DEPLOYMENT_KEY",
                "gl_demo_pa_key",
            ),
        )

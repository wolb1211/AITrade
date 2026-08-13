from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env_path = _project_root() / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
    ai_timeout: float = 30.0
    demo_deployment_key: str = "gl_demo_pa_key"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
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
            ai_timeout=float(os.getenv("GAINLAB_AI_TIMEOUT", "30")),
            demo_deployment_key=os.getenv(
                "GAINLAB_DEMO_DEPLOYMENT_KEY",
                "gl_demo_pa_key",
            ),
        )

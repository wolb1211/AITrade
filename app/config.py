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

    mail_env_file = os.getenv("GAINLAB_MAIL_ENV_FILE", "").strip()
    if not mail_env_file:
        return
    external_path = Path(mail_env_file)
    if not external_path.exists():
        return
    for raw_line in external_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("MAIL_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


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
    auth_secret: str = ""
    admin_jwt_secret: str = ""
    session_days: int = 30
    verification_minutes: int = 10
    mail_host: str = ""
    mail_port: int = 465
    mail_user: str = ""
    mail_password: str = ""
    mail_from: str = ""
    mail_secure: bool = True

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
            auth_secret=os.getenv("GAINLAB_AUTH_SECRET", ""),
            admin_jwt_secret=os.getenv("GAINLAB_ADMIN_JWT_SECRET", ""),
            session_days=int(os.getenv("GAINLAB_SESSION_DAYS", "30")),
            verification_minutes=int(os.getenv("GAINLAB_VERIFICATION_MINUTES", "10")),
            mail_host=os.getenv("GAINLAB_MAIL_HOST", os.getenv("MAIL_HOST", "")),
            mail_port=int(os.getenv("GAINLAB_MAIL_PORT", os.getenv("MAIL_PORT", "465"))),
            mail_user=os.getenv("GAINLAB_MAIL_USER", os.getenv("MAIL_USER", "")),
            mail_password=os.getenv("GAINLAB_MAIL_PASSWORD", os.getenv("MAIL_PASS", "")),
            mail_from=os.getenv("GAINLAB_MAIL_FROM", os.getenv("MAIL_FROM", os.getenv("MAIL_USER", ""))),
            mail_secure=os.getenv("GAINLAB_MAIL_SECURE", "true").strip().lower() not in {"0", "false", "no"},
            demo_deployment_key=os.getenv(
                "GAINLAB_DEMO_DEPLOYMENT_KEY",
                "gl_demo_pa_key",
            ),
        )

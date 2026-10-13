from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    app_secret_key: str = ""
    app_secret_key_file: str | None = None
    database_url: str | None = None
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "cloud_transfer"
    postgres_user: str = "cloud_transfer"
    postgres_password_file: str | None = None
    redis_url: str = "redis://redis:6379/0"
    rclone_binary: str = "/usr/bin/rclone"
    smb_allowed_targets: str = ""
    smb_interactive_timeout_seconds: int = Field(default=30, ge=1, le=300)
    nextcloud_enabled: bool = False
    nextcloud_allowed_origins: str = ""
    nextcloud_max_concurrent: int = Field(default=1, ge=1, le=8)
    nextcloud_bandwidth_limit: str = Field(default="8M", pattern=r"^[1-9][0-9]{0,3}(?:K|M|G)?$")
    nextcloud_chunk_size: str = Field(
        default="10Mi",
        pattern=r"^(?:(?:10|32|64|128|256|512)Mi|1Gi)$",
    )
    nextcloud_job_timeout_seconds: int = Field(default=43200, ge=60, le=86400)
    megacmd_binary_dir: str = "/usr/bin"
    transfer_temp_path: str = "/var/lib/cloud-transfer/tmp"
    transfer_staging_path: str = "/var/lib/cloud-transfer/staging"
    transfer_max_retries: int = Field(default=5, ge=0)
    transfer_max_concurrent: int = Field(default=1, ge=1)
    transfer_max_files: int = Field(default=5000, ge=1)
    smb_transfer_max_files: int = Field(default=100000, ge=1)
    transfer_max_file_bytes: int = Field(default=0, ge=0)
    transfer_staging_reserve_bytes: int = Field(default=1073741824, ge=0)
    transfer_staging_margin_percent: int = Field(default=20, ge=0, le=100)
    app_base_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password_file: str | None = None
    smtp_use_tls: bool = True
    smtp_from_address: str = ""
    session_cookie_secure: bool = True
    session_max_age_seconds: int = 28800
    master_encryption_key_file: str = "/run/ctm-secrets/master_key"

    def session_secret(self) -> str:
        if self.app_secret_key:
            return self.app_secret_key
        if self.app_secret_key_file:
            return Path(self.app_secret_key_file).read_text(encoding="utf-8").strip()
        return ""

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        password = None
        if self.postgres_password_file:
            password = Path(self.postgres_password_file).read_text(encoding="utf-8").strip()
        return URL.create(
            "postgresql+asyncpg",
            username=self.postgres_user,
            password=password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings()

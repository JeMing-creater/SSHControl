from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONTROL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Control Platform"
    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"
    static_dir: Path = BASE_DIR / "static"
    templates_dir: Path = BASE_DIR / "templates"
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'platform.db').as_posix()}"
    default_port_start: int = 50000
    default_port_end: int = 50050
    default_snapshot_root: str = "/mnt/docker_platform_snapshots"
    default_workspace_root: str = "/workspace/tenants"
    default_base_image: str = "pytorch:2.7.1-cuda12.8-cudnn9-devel"
    default_user_share: int = 10
    scheduler_interval_seconds: int = 3600
    dashboard_refresh_seconds: int = 30
    auth_secret_key: str = "change-this-control-platform-secret"
    root_admin_account: str = "root"
    root_admin_password: str = "scutbiolab"
    registration_notify_email: str = "jaming.work@gmail.com"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_use_tls: bool = True


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.static_dir.mkdir(parents=True, exist_ok=True)
    settings.templates_dir.mkdir(parents=True, exist_ok=True)
    return settings

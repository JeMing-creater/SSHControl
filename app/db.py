from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


settings = get_settings()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        inspector = inspect(connection)
        allocation_columns = {column["name"] for column in inspector.get_columns("allocations")}
        if "snapshot_policy_override" not in allocation_columns:
            connection.execute(
                text("ALTER TABLE allocations ADD COLUMN snapshot_policy_override BOOLEAN NOT NULL DEFAULT 0")
            )
        if "snapshot_keep_count" not in allocation_columns:
            connection.execute(
                text("ALTER TABLE allocations ADD COLUMN snapshot_keep_count INTEGER")
            )
        if "snapshot_interval_days" not in allocation_columns:
            connection.execute(
                text("ALTER TABLE allocations ADD COLUMN snapshot_interval_days INTEGER")
            )
        host_columns = {column["name"] for column in inspector.get_columns("managed_hosts")}
        if "cached_images" not in host_columns:
            connection.execute(
                text("ALTER TABLE managed_hosts ADD COLUMN cached_images TEXT")
            )
        cache_columns = {column["name"] for column in inspector.get_columns("host_status_cache")}
        cache_migrations = {
            "stable_reachable": "ALTER TABLE host_status_cache ADD COLUMN stable_reachable BOOLEAN NOT NULL DEFAULT 0",
            "stable_docker_info_json": "ALTER TABLE host_status_cache ADD COLUMN stable_docker_info_json TEXT",
            "stable_gpus_json": "ALTER TABLE host_status_cache ADD COLUMN stable_gpus_json TEXT",
            "stable_container_rows_json": "ALTER TABLE host_status_cache ADD COLUMN stable_container_rows_json TEXT",
            "stable_stats_json": "ALTER TABLE host_status_cache ADD COLUMN stable_stats_json TEXT",
            "stable_gpu_detail_json": "ALTER TABLE host_status_cache ADD COLUMN stable_gpu_detail_json TEXT",
            "stable_disk_usage_json": "ALTER TABLE host_status_cache ADD COLUMN stable_disk_usage_json TEXT",
            "stable_error_log": "ALTER TABLE host_status_cache ADD COLUMN stable_error_log TEXT",
            "stable_refreshed_at": "ALTER TABLE host_status_cache ADD COLUMN stable_refreshed_at DATETIME",
            "refresh_in_progress": "ALTER TABLE host_status_cache ADD COLUMN refresh_in_progress BOOLEAN NOT NULL DEFAULT 0",
            "refresh_started_at": "ALTER TABLE host_status_cache ADD COLUMN refresh_started_at DATETIME",
            "refresh_completed_at": "ALTER TABLE host_status_cache ADD COLUMN refresh_completed_at DATETIME",
        }
        for column_name, sql in cache_migrations.items():
            if column_name not in cache_columns:
                connection.execute(text(sql))
        connection.execute(
            text(
                """
                UPDATE host_status_cache
                SET stable_reachable = COALESCE(stable_reachable, reachable, 0),
                    stable_docker_info_json = COALESCE(stable_docker_info_json, docker_info_json),
                    stable_gpus_json = COALESCE(stable_gpus_json, gpus_json),
                    stable_container_rows_json = COALESCE(stable_container_rows_json, container_rows_json),
                    stable_stats_json = COALESCE(stable_stats_json, stats_json),
                    stable_gpu_detail_json = COALESCE(stable_gpu_detail_json, gpu_detail_json),
                    stable_disk_usage_json = COALESCE(stable_disk_usage_json, disk_usage_json),
                    stable_error_log = COALESCE(stable_error_log, error_log),
                    stable_refreshed_at = COALESCE(stable_refreshed_at, refreshed_at),
                    refresh_in_progress = COALESCE(refresh_in_progress, 0)
                WHERE stable_refreshed_at IS NULL
                  AND refreshed_at IS NOT NULL
                """
            )
        )
        admin_columns = {column["name"] for column in inspector.get_columns("admin_users")}
        expected_admin_columns = {
            "id",
            "account",
            "password_hash",
            "email",
            "student_id",
            "status",
            "approved_by",
            "approved_at",
            "revoked_at",
            "created_at",
            "updated_at",
        }
        if admin_columns and not admin_columns.issubset(expected_admin_columns):
            connection.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS admin_users_clean (
                        id INTEGER NOT NULL,
                        account VARCHAR(100) NOT NULL,
                        password_hash VARCHAR(255) NOT NULL,
                        email VARCHAR(255) NOT NULL,
                        student_id VARCHAR(100) NOT NULL,
                        status VARCHAR(20) NOT NULL,
                        approved_by VARCHAR(100),
                        approved_at DATETIME,
                        revoked_at DATETIME,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        PRIMARY KEY (id),
                        UNIQUE (account)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT OR IGNORE INTO admin_users_clean (
                        id,
                        account,
                        password_hash,
                        email,
                        student_id,
                        status,
                        approved_by,
                        approved_at,
                        revoked_at,
                        created_at,
                        updated_at
                    )
                    SELECT
                        id,
                        account,
                        password_hash,
                        email,
                        student_id,
                        status,
                        approved_by,
                        approved_at,
                        revoked_at,
                        created_at,
                        updated_at
                    FROM admin_users
                    """
                )
            )
            connection.execute(text("DROP TABLE admin_users"))
            connection.execute(text("ALTER TABLE admin_users_clean RENAME TO admin_users"))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_admin_users_status ON admin_users (status)")
            )
            connection.execute(
                text("CREATE UNIQUE INDEX IF NOT EXISTS ix_admin_users_account ON admin_users (account)")
            )
        Base.metadata.create_all(bind=connection)
        connection.execute(
            text(
                """
                INSERT INTO platform_users (
                    account,
                    password_hash,
                    enabled,
                    notes,
                    created_at,
                    updated_at
                )
                VALUES (
                    'user',
                    '8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92',
                    1,
                    'Public read-only platform user without any legal host allocation.',
                    CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT(account) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    enabled = 1,
                    notes = excluded.notes,
                    updated_at = CURRENT_TIMESTAMP
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE managed_hosts
                SET is_local = 0,
                    enabled = 0,
                    notes = COALESCE(notes || char(10), '') || 'SSH-only migration: this host must be reconfigured with password or key authentication.'
                WHERE auth_type = 'local'
                  AND (
                    notes IS NULL
                    OR notes NOT LIKE '%SSH-only migration: this host must be reconfigured with password or key authentication.%'
                  )
                """
            )
        )

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

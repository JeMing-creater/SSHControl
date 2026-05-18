from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AuthType(str, Enum):
    LOCAL = "local"
    PASSWORD = "password"
    KEY = "key"


class AllocationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    DELETED = "deleted"


class AdminStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REVOKED = "revoked"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class ManagedHost(TimestampMixin, Base):
    __tablename__ = "managed_hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    address: Mapped[str] = mapped_column(String(255), nullable=False, default="127.0.0.1")
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(100), default="root")
    auth_type: Mapped[str] = mapped_column(String(20), default=AuthType.LOCAL.value)
    ssh_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ssh_key_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    port_start: Mapped[int] = mapped_column(Integer, default=50000)
    port_end: Mapped[int] = mapped_column(Integer, default=50050)
    workspace_root: Mapped[str] = mapped_column(String(255), default="/workspace/tenants")
    shared_mnt_path: Mapped[str] = mapped_column(String(255), default="/mnt")
    snapshot_root: Mapped[str] = mapped_column(
        String(255),
        default="/mnt/docker_platform_snapshots",
    )
    reserve_cpu_cores: Mapped[float] = mapped_column(Float, default=8.0)
    reserve_memory_gb: Mapped[float] = mapped_column(Float, default=64.0)
    reserve_disk_gb: Mapped[float] = mapped_column(Float, default=200.0)
    default_user_share: Mapped[int] = mapped_column(Integer, default=10)
    snapshot_keep_count: Mapped[int] = mapped_column(Integer, default=2)
    snapshot_interval_days: Mapped[int] = mapped_column(Integer, default=14)
    workspace_limit_mode: Mapped[str] = mapped_column(String(40), default="metadata_only")
    cached_images: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    allocations: Mapped[list["Allocation"]] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
    )

    status_cache: Mapped["HostStatusCache | None"] = relationship(
        back_populates="host",
        cascade="all, delete-orphan",
        uselist=False,
    )


class HostStatusCache(TimestampMixin, Base):
    __tablename__ = "host_status_cache"

    host_id: Mapped[int] = mapped_column(ForeignKey("managed_hosts.id"), primary_key=True)
    reachable: Mapped[bool] = mapped_column(Boolean, default=False)
    docker_info_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    gpus_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    container_rows_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    gpu_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    disk_usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stable_reachable: Mapped[bool] = mapped_column(Boolean, default=False)
    stable_docker_info_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stable_gpus_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stable_container_rows_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stable_stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stable_gpu_detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stable_disk_usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    stable_error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    stable_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_in_progress: Mapped[bool] = mapped_column(Boolean, default=False)
    refresh_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    host: Mapped["ManagedHost"] = relationship(back_populates="status_cache")


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    account: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    student_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=AdminStatus.PENDING.value, index=True)
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlatformUser(TimestampMixin, Base):
    __tablename__ = "platform_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    account: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Allocation(TimestampMixin, Base):
    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("managed_hosts.id"), index=True)
    host_port: Mapped[int] = mapped_column(Integer, index=True)
    container_name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    image_name: Mapped[str] = mapped_column(String(255), nullable=False)
    assignee: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    root_password: Mapped[str] = mapped_column(String(255), nullable=False)
    cpu_limit_cores: Mapped[float] = mapped_column(Float, default=4.0)
    memory_limit_gb: Mapped[float] = mapped_column(Float, default=32.0)
    workspace_limit_gb: Mapped[float] = mapped_column(Float, default=200.0)
    status: Mapped[str] = mapped_column(String(20), default=AllocationStatus.PENDING.value)
    shared_mnt_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    all_gpus_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    x11_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    extra_mounts: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_policy_override: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_keep_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_rebuild: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_rebuild_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    host: Mapped["ManagedHost"] = relationship(back_populates="allocations")
    snapshots: Mapped[list["SnapshotRecord"]] = relationship(
        back_populates="allocation",
        cascade="all, delete-orphan",
    )


class SnapshotRecord(TimestampMixin, Base):
    __tablename__ = "snapshot_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("managed_hosts.id"), index=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), index=True)
    snapshot_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    image_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ready")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    allocation: Mapped["Allocation"] = relationship(back_populates="snapshots")

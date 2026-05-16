from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Allocation, SnapshotRecord
from app.services.docker_engine import DockerService


def effective_snapshot_policy(allocation: Allocation) -> tuple[int, int]:
    if allocation.snapshot_policy_override:
        keep_count = allocation.snapshot_keep_count if allocation.snapshot_keep_count is not None else 0
        interval_days = allocation.snapshot_interval_days if allocation.snapshot_interval_days is not None else 14
        return max(keep_count, 0), max(interval_days, 1)
    return max(allocation.host.snapshot_keep_count, 0), max(allocation.host.snapshot_interval_days, 1)


def create_snapshot_record(db: Session, allocation: Allocation, logs: list[str] | None = None) -> SnapshotRecord:
    docker = DockerService(allocation.host)
    image_ref, archive_path = docker.create_snapshot(allocation, logs=logs)
    record = SnapshotRecord(
        host_id=allocation.host_id,
        allocation_id=allocation.id,
        snapshot_name=f"{allocation.container_name}-{datetime.utcnow():%Y%m%d-%H%M%S}",
        storage_path=archive_path,
        image_ref=image_ref,
        status="ready",
    )
    db.add(record)
    try:
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        try:
            docker.remove_image(image_ref)
        except Exception:
            pass
        try:
            docker.runner.run(f"rm -f {archive_path}", timeout=600)
        except Exception:
            pass
        raise
    db.refresh(record)
    try:
        rotate_snapshots(db, allocation)
    except Exception:
        db.rollback()
        try:
            docker.remove_image(image_ref)
        except Exception:
            pass
        try:
            docker.runner.run(f"rm -f {archive_path}", timeout=600)
        except Exception:
            pass
        raise
    return record


def rotate_snapshots(db: Session, allocation: Allocation) -> None:
    keep_count, _ = effective_snapshot_policy(allocation)
    records = (
        db.query(SnapshotRecord)
        .filter(SnapshotRecord.allocation_id == allocation.id)
        .order_by(SnapshotRecord.created_at.desc())
        .all()
    )
    removable = records[keep_count:]
    docker = DockerService(allocation.host)
    for record in removable:
        try:
            docker.runner.run(f"rm -f {record.storage_path}", timeout=600)
        finally:
            try:
                docker.remove_image(record.image_ref)
            except Exception:
                pass
            db.delete(record)
    if removable:
        db.commit()


def snapshot_due(allocation: Allocation) -> bool:
    keep_count, interval_days = effective_snapshot_policy(allocation)
    if keep_count <= 0:
        return False
    if allocation.status != "running":
        return False
    snapshots = sorted(allocation.snapshots, key=lambda item: item.created_at, reverse=True)
    if not snapshots:
        return True
    last_created_at = snapshots[0].created_at
    return datetime.utcnow() >= last_created_at + timedelta(days=interval_days)

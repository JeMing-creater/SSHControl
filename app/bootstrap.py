import socket

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AuthType, ManagedHost


def ensure_local_host(db: Session) -> None:
    existing = db.query(ManagedHost).filter(ManagedHost.is_local.is_(True)).first()
    if existing:
        return

    settings = get_settings()
    host = ManagedHost(
        name=socket.gethostname(),
        address="127.0.0.1",
        ssh_port=22,
        ssh_user="root",
        auth_type=AuthType.LOCAL.value,
        is_local=True,
        enabled=True,
        port_start=settings.default_port_start,
        port_end=settings.default_port_end,
        workspace_root=settings.default_workspace_root,
        shared_mnt_path="/mnt",
        snapshot_root=settings.default_snapshot_root,
        reserve_cpu_cores=8.0,
        reserve_memory_gb=64.0,
        reserve_disk_gb=200.0,
        default_user_share=settings.default_user_share,
        snapshot_keep_count=2,
        snapshot_interval_days=14,
        notes="Auto-registered local management host.",
    )
    db.add(host)
    db.commit()

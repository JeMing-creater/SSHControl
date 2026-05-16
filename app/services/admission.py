from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Allocation, AllocationStatus, ManagedHost
from app.services.docker_engine import DockerService


ACTIVE_STATUSES = (
    AllocationStatus.PENDING.value,
    AllocationStatus.RUNNING.value,
    AllocationStatus.STOPPED.value,
)


@dataclass
class AdmissionDecision:
    allowed: bool
    reasons: list[str]


def recommended_defaults(host: ManagedHost, docker_info: dict | None = None) -> dict[str, int]:
    docker_info = docker_info or {}
    total_cpu = float(docker_info.get("NCPU") or 0)
    total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)

    usable_cpu = max(total_cpu - host.reserve_cpu_cores, 1.0)
    usable_memory = max(total_memory_gb - host.reserve_memory_gb, 4.0)
    share = max(host.default_user_share, 1)

    return {
        "cpu_limit_cores": max(math.floor(usable_cpu / share), 1),
        "memory_limit_gb": max(math.floor(usable_memory / share), 4),
        "workspace_limit_gb": max(math.floor(host.reserve_disk_gb / share), 50),
    }


def check_allocation(
    db: Session,
    host: ManagedHost,
    cpu_limit_cores: float,
    memory_limit_gb: float,
    exclude_allocation_id: int | None = None,
) -> AdmissionDecision:
    docker_info = {}
    try:
        docker_info = DockerService(host).docker_info()
    except Exception:
        pass

    total_cpu = float(docker_info.get("NCPU") or 0)
    total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)

    query = db.query(
        func.coalesce(func.sum(Allocation.cpu_limit_cores), 0.0),
        func.coalesce(func.sum(Allocation.memory_limit_gb), 0.0),
    ).filter(
        Allocation.host_id == host.id,
        Allocation.status.in_(ACTIVE_STATUSES),
    )
    if exclude_allocation_id is not None:
        query = query.filter(Allocation.id != exclude_allocation_id)

    allocated_cpu, allocated_memory = query.one()

    reasons: list[str] = []
    if total_cpu and (allocated_cpu + cpu_limit_cores + host.reserve_cpu_cores) > total_cpu:
        reasons.append(
            f"CPU超出安全水位：当前已分配 {allocated_cpu:.1f} 核，保底预留 {host.reserve_cpu_cores:.1f} 核。"
        )
    if total_memory_gb and (allocated_memory + memory_limit_gb + host.reserve_memory_gb) > total_memory_gb:
        reasons.append(
            f"内存超出安全水位：当前已分配 {allocated_memory:.1f} GB，保底预留 {host.reserve_memory_gb:.1f} GB。"
        )
    return AdmissionDecision(allowed=not reasons, reasons=reasons)

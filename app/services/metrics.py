from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Allocation, AllocationStatus, ManagedHost
from app.services.docker_engine import DockerService


ACTIVE_STATUSES = (
    AllocationStatus.PENDING.value,
    AllocationStatus.RUNNING.value,
    AllocationStatus.STOPPED.value,
)


def parse_percent(value: str) -> float:
    value = (value or "").replace("%", "").strip()
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def parse_memory_usage(value: str) -> float:
    chunk = (value or "").split("/")[0].strip().upper()
    if not chunk:
        return 0.0
    units = {"GIB": 1.0, "MIB": 1 / 1024, "KIB": 1 / (1024**2), "B": 1 / (1024**3)}
    for suffix, factor in units.items():
        if chunk.endswith(suffix):
            try:
                return float(chunk[: -len(suffix)].strip()) * factor
            except ValueError:
                return 0.0
    try:
        return float(chunk)
    except ValueError:
        return 0.0


def host_summary(
    db: Session,
    host: ManagedHost,
    timeout: int = 120,
    include_heavy: bool = True,
    include_gpus: bool = True,
) -> dict:
    try:
        docker = DockerService(host)
        reachable = docker.ping(timeout=timeout)
        docker_info = docker.docker_info(timeout=timeout) if reachable else {}
        stats = docker.list_container_stats(timeout=timeout) if reachable and include_heavy else []
        gpus = docker.gpu_stats(timeout=timeout) if reachable and include_gpus else []
        actual_container_names = (
            {
                item["container_name"]
                for item in docker.managed_container_rows(host.port_start, host.port_end, timeout=timeout)
            }
            if reachable
            else set()
        )
    except Exception:
        reachable = False
        docker_info = {}
        stats = []
        gpus = []
        actual_container_names = set()

    active_query = db.query(Allocation).filter(
        Allocation.host_id == host.id,
        Allocation.status.in_(ACTIVE_STATUSES),
    )
    if actual_container_names:
        active_allocations = [
            allocation for allocation in active_query.all()
            if allocation.container_name in actual_container_names
        ]
    else:
        active_allocations = []

    allocated_cpu = sum(float(item.cpu_limit_cores or 0.0) for item in active_allocations)
    allocated_memory_gb = sum(float(item.memory_limit_gb or 0.0) for item in active_allocations)

    return {
        "reachable": reachable,
        "docker_info": docker_info,
        "stats": stats,
        "gpus": gpus,
        "allocated_cpu": allocated_cpu,
        "allocated_memory_gb": allocated_memory_gb,
        "active_allocations": len(active_allocations),
        "container_count": len(stats),
    }

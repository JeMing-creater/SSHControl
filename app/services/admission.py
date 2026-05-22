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
HOST_SURVIVAL_RESERVE_RATIO = 0.15
PLATFORM_ALLOCATABLE_RATIO = 1.0 - HOST_SURVIVAL_RESERVE_RATIO


@dataclass
class AdmissionDecision:
    allowed: bool
    reasons: list[str]
    warnings: list[str] | None = None


def platform_allocatable_limit(total: float | int | None) -> float:
    return max(float(total or 0.0) * PLATFORM_ALLOCATABLE_RATIO, 0.0)


def _disk_total_from_docker_info(docker_info: dict) -> float:
    workspace_usage = docker_info.get("_workspace_disk_usage") if isinstance(docker_info, dict) else {}
    root_usage = docker_info.get("_root_disk_usage") if isinstance(docker_info, dict) else {}
    if not isinstance(workspace_usage, dict):
        workspace_usage = {}
    if not isinstance(root_usage, dict):
        root_usage = {}
    return float(workspace_usage.get("total_gb") or root_usage.get("total_gb") or 0.0)


def recommended_defaults(host: ManagedHost, docker_info: dict | None = None) -> dict[str, int]:
    docker_info = docker_info or {}
    total_cpu = float(docker_info.get("NCPU") or 0)
    total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)
    total_disk_gb = _disk_total_from_docker_info(docker_info)

    usable_cpu = max(platform_allocatable_limit(total_cpu), 1.0)
    usable_memory = max(platform_allocatable_limit(total_memory_gb), 4.0)
    usable_disk = max(platform_allocatable_limit(total_disk_gb), 50.0)
    share = max(host.default_user_share, 1)

    return {
        "cpu_limit_cores": max(math.floor(usable_cpu / share), 1),
        "memory_limit_gb": max(math.floor(usable_memory / share), 4),
        "workspace_limit_gb": max(math.floor(usable_disk / share), 50),
    }


def check_allocation(
    db: Session,
    host: ManagedHost,
    cpu_limit_cores: float,
    memory_limit_gb: float,
    workspace_limit_gb: float,
    exclude_allocation_id: int | None = None,
) -> AdmissionDecision:
    docker_info = {}
    try:
        docker_service = DockerService(host)
        docker_info = docker_service.docker_info()
        disk_info = docker_service.filesystem_usage_gb(host.workspace_root)
    except Exception:
        disk_info = {}
        pass

    total_cpu = float(docker_info.get("NCPU") or 0)
    total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)
    total_disk_gb = float(disk_info.get("total_gb") or 0)
    cpu_platform_limit = platform_allocatable_limit(total_cpu)
    memory_platform_limit = platform_allocatable_limit(total_memory_gb)
    disk_platform_limit = platform_allocatable_limit(total_disk_gb)

    query = db.query(
        func.coalesce(func.sum(Allocation.cpu_limit_cores), 0.0),
        func.coalesce(func.sum(Allocation.memory_limit_gb), 0.0),
        func.coalesce(func.sum(Allocation.workspace_limit_gb), 0.0),
    ).filter(
        Allocation.host_id == host.id,
        Allocation.status.in_(ACTIVE_STATUSES),
    )
    if exclude_allocation_id is not None:
        query = query.filter(Allocation.id != exclude_allocation_id)

    allocated_cpu, allocated_memory, allocated_disk = query.one()

    allocated_cpu = float(allocated_cpu or 0.0)
    allocated_memory = float(allocated_memory or 0.0)
    allocated_disk = float(allocated_disk or 0.0)
    requested_cpu = float(cpu_limit_cores or 0.0)
    requested_memory = float(memory_limit_gb or 0.0)
    requested_disk = float(workspace_limit_gb or 0.0)
    requested_cpu_total = allocated_cpu + requested_cpu
    requested_memory_total = allocated_memory + requested_memory
    requested_disk_total = allocated_disk + requested_disk

    reasons: list[str] = []
    warnings: list[str] = []
    if total_cpu and requested_cpu_total > cpu_platform_limit:
        reasons.append(
            "CPU超出安全水位："
            f"当前已登记分配 {allocated_cpu:.1f} 核，本次申请 {requested_cpu:.1f} 核，"
            f"合计 {requested_cpu_total:.1f} 核，平台可分配上限 {cpu_platform_limit:.1f} 核（宿主机预留15%）。"
        )
    if total_memory_gb and requested_memory_total > memory_platform_limit:
        reasons.append(
            "内存超出安全水位："
            f"当前已登记分配 {allocated_memory:.1f} GB，本次申请 {requested_memory:.1f} GB，"
            f"合计 {requested_memory_total:.1f} GB，平台可分配上限 {memory_platform_limit:.1f} GB（宿主机预留15%）。"
        )
    if not total_disk_gb:
        if (host.workspace_limit_mode or "metadata_only").strip().lower() == "strict_storage_opt":
            reasons.append("无法读取宿主机工作目录磁盘容量，严格磁盘限额模式下暂不允许分配。")
        else:
            warnings.append("无法读取宿主机工作目录磁盘容量，已降级为仅记录 workspace 上限，不阻断容器创建。")
    elif requested_disk_total > disk_platform_limit:
        reasons.append(
            "磁盘超出安全水位："
            f"当前已登记分配 {allocated_disk:.1f} GB，本次申请 {requested_disk:.1f} GB，"
            f"合计 {requested_disk_total:.1f} GB，平台可分配上限 {disk_platform_limit:.1f} GB（宿主机预留15%）。"
        )
    return AdmissionDecision(allowed=not reasons, reasons=reasons, warnings=warnings)

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Allocation, AllocationStatus, ManagedHost
from app.services.docker_engine import DockerService
from app.services.metrics import parse_memory_usage, parse_percent


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


@dataclass
class AdmissionUsageSnapshot:
    docker_info: dict
    disk_info: dict
    stats_rows: list[dict]
    disk_usage_by_name: dict[str, dict]
    port_container_rows: list[dict]
    warnings: list[str]


@dataclass
class ResourceReservationSummary:
    elastic_cpu: float
    elastic_memory_gb: float
    elastic_disk_gb: float
    guarantee_cpu: float
    guarantee_memory_gb: float
    guarantee_disk_gb: float
    actual_cpu: float
    actual_memory_gb: float
    actual_disk_gb: float
    reserved_cpu: float
    reserved_memory_gb: float
    reserved_disk_gb: float

    def elastic_payload(self, gpu: float = 0.0) -> dict[str, float]:
        return {
            "gpu": round(float(gpu or 0.0), 2),
            "cpu": round(self.elastic_cpu, 2),
            "memory": round(self.elastic_memory_gb, 2),
            "disk": round(self.elastic_disk_gb, 2),
        }

    def guarantee_payload(self) -> dict[str, float]:
        return {
            "cpu": round(self.guarantee_cpu, 2),
            "memory": round(self.guarantee_memory_gb, 2),
            "disk": round(self.guarantee_disk_gb, 2),
        }

    def reserved_payload(self) -> dict[str, float]:
        return {
            "cpu": round(self.reserved_cpu, 2),
            "memory": round(self.reserved_memory_gb, 2),
            "disk": round(self.reserved_disk_gb, 2),
        }


def platform_allocatable_limit(total: float | int | None) -> float:
    return max(float(total or 0.0) * PLATFORM_ALLOCATABLE_RATIO, 0.0)


def guarantee_from_elastic(value: float | int | None) -> int:
    return max(math.floor(float(value or 0.0) / 4), 1)


def _disk_total_from_docker_info(docker_info: dict) -> float:
    workspace_usage = docker_info.get("_workspace_disk_usage") if isinstance(docker_info, dict) else {}
    root_usage = docker_info.get("_root_disk_usage") if isinstance(docker_info, dict) else {}
    if not isinstance(workspace_usage, dict):
        workspace_usage = {}
    if not isinstance(root_usage, dict):
        root_usage = {}
    return float(workspace_usage.get("total_gb") or root_usage.get("total_gb") or 0.0)


def _parse_cache_json(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _normalize_container_name(value: object) -> str:
    return str(value or "").strip().lstrip("/")


def _cached_usage_snapshot(host: ManagedHost, realtime_error: str) -> AdmissionUsageSnapshot | None:
    cache = host.status_cache
    if not cache:
        return None
    use_stable = bool(cache.stable_refreshed_at)
    docker_info = _parse_cache_json(cache.stable_docker_info_json if use_stable else cache.docker_info_json, {})
    container_rows = _parse_cache_json(cache.stable_container_rows_json if use_stable else cache.container_rows_json, [])
    stats_rows = _parse_cache_json(cache.stable_stats_json if use_stable else cache.stats_json, [])
    disk_usage_by_name = _parse_cache_json(cache.stable_disk_usage_json if use_stable else cache.disk_usage_json, {})
    if not isinstance(docker_info, dict) or not docker_info:
        return None
    if not isinstance(container_rows, list):
        container_rows = []
    if not isinstance(stats_rows, list):
        stats_rows = []
    if not isinstance(disk_usage_by_name, dict):
        disk_usage_by_name = {}

    running_rows = [
        row for row in container_rows
        if isinstance(row, dict) and str(row.get("status") or "").strip().lower() == "running"
    ]
    if running_rows and not stats_rows:
        return None

    disk_info = {}
    workspace_usage = docker_info.get("_workspace_disk_usage") if isinstance(docker_info, dict) else {}
    root_usage = docker_info.get("_root_disk_usage") if isinstance(docker_info, dict) else {}
    if isinstance(workspace_usage, dict) and workspace_usage:
        disk_info = workspace_usage
    elif isinstance(root_usage, dict) and root_usage:
        disk_info = root_usage

    cache_time = cache.stable_refreshed_at if use_stable else cache.refreshed_at
    cache_label = cache_time.strftime("%Y-%m-%d %H:%M:%S") if cache_time else "未知时间"
    return AdmissionUsageSnapshot(
        docker_info=docker_info,
        disk_info=disk_info if isinstance(disk_info, dict) else {},
        stats_rows=stats_rows,
        disk_usage_by_name={_normalize_container_name(key): value for key, value in disk_usage_by_name.items()},
        port_container_rows=container_rows,
        warnings=[
            "实时资源读取失败，已使用平台最后稳定缓存进行弹性准入判断。"
            f"缓存时间：{cache_label}；实时读取错误：{realtime_error}"
        ],
    )


def _load_usage_snapshot(host: ManagedHost) -> AdmissionUsageSnapshot | None:
    docker_service = DockerService(host)
    warnings: list[str] = []
    try:
        docker_info = docker_service.docker_info(timeout=20)
    except Exception as exc:
        return _cached_usage_snapshot(host, f"Docker 基础信息读取失败：{exc}")

    try:
        stats_rows = docker_service.list_container_stats(timeout=20)
    except Exception as exc:
        return _cached_usage_snapshot(host, f"docker stats 读取失败：{exc}")

    try:
        disk_info = docker_service.filesystem_usage_gb(host.workspace_root, timeout=20)
    except Exception as exc:
        disk_info = {}
        warnings.append(f"工作目录磁盘容量读取失败，已降级使用 Docker 缓存或元数据：{exc}")

    try:
        disk_usage_by_name = docker_service.container_disk_usage_gb_map(timeout=20)
    except Exception as exc:
        disk_usage_by_name = {}
        warnings.append(f"容器 Docker 磁盘占用读取失败，本次仅按保障额度参与准入：{exc}")

    try:
        port_container_rows = docker_service.managed_container_rows(host.port_start, host.port_end, timeout=20)
    except Exception as exc:
        port_container_rows = []
        warnings.append(f"端口池容器扫描失败，已按 docker stats 可见容器继续准入：{exc}")

    return AdmissionUsageSnapshot(
        docker_info=docker_info,
        disk_info=disk_info if isinstance(disk_info, dict) else {},
        stats_rows=stats_rows,
        disk_usage_by_name={_normalize_container_name(key): value for key, value in disk_usage_by_name.items()},
        port_container_rows=port_container_rows,
        warnings=warnings,
    )


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


def reservation_summary_from_rows(resource_rows: list[dict]) -> ResourceReservationSummary:
    elastic_cpu = 0.0
    elastic_memory = 0.0
    elastic_disk = 0.0
    guarantee_cpu = 0.0
    guarantee_memory = 0.0
    guarantee_disk = 0.0
    actual_cpu = 0.0
    actual_memory = 0.0
    actual_disk = 0.0
    reserved_cpu = 0.0
    reserved_memory = 0.0
    reserved_disk = 0.0

    for row in resource_rows:
        row_actual_cpu = parse_percent(str(row.get("cpu_percent") or "")) / 100
        row_actual_memory = float(row.get("memory_used_gb") or 0.0)
        row_actual_disk = float(row.get("disk_used_gb") or 0.0)
        row_elastic_cpu = float(row.get("cpu_limit_cores") or 0.0)
        row_elastic_memory = float(row.get("memory_limit_gb") or 0.0)
        row_elastic_disk = float(row.get("workspace_limit_gb") or 0.0)

        actual_cpu += row_actual_cpu
        actual_memory += row_actual_memory
        actual_disk += row_actual_disk
        if row_elastic_cpu > 0 or row_elastic_memory > 0 or row_elastic_disk > 0:
            row_guarantee_cpu = guarantee_from_elastic(row_elastic_cpu)
            row_guarantee_memory = guarantee_from_elastic(row_elastic_memory)
            row_guarantee_disk = guarantee_from_elastic(row_elastic_disk)
            elastic_cpu += row_elastic_cpu
            elastic_memory += row_elastic_memory
            elastic_disk += row_elastic_disk
            guarantee_cpu += row_guarantee_cpu
            guarantee_memory += row_guarantee_memory
            guarantee_disk += row_guarantee_disk
            reserved_cpu += max(row_actual_cpu, row_guarantee_cpu)
            reserved_memory += max(row_actual_memory, row_guarantee_memory)
            reserved_disk += max(row_actual_disk, row_guarantee_disk)
        else:
            reserved_cpu += row_actual_cpu
            reserved_memory += row_actual_memory
            reserved_disk += row_actual_disk

    return ResourceReservationSummary(
        elastic_cpu=elastic_cpu,
        elastic_memory_gb=elastic_memory,
        elastic_disk_gb=elastic_disk,
        guarantee_cpu=guarantee_cpu,
        guarantee_memory_gb=guarantee_memory,
        guarantee_disk_gb=guarantee_disk,
        actual_cpu=actual_cpu,
        actual_memory_gb=actual_memory,
        actual_disk_gb=actual_disk,
        reserved_cpu=reserved_cpu,
        reserved_memory_gb=reserved_memory,
        reserved_disk_gb=reserved_disk,
    )


def check_allocation(
    db: Session,
    host: ManagedHost,
    cpu_limit_cores: float,
    memory_limit_gb: float,
    workspace_limit_gb: float,
    exclude_allocation_id: int | None = None,
) -> AdmissionDecision:
    snapshot = _load_usage_snapshot(host)
    if snapshot is None:
        return AdmissionDecision(
            allowed=False,
            reasons=[
                "无法读取宿主机实时资源状态，且平台没有可用稳定缓存，暂不能进行弹性资源准入判断。"
                "请先确认 SSH 与 Docker 可访问，或等待后台监控完成一次成功刷新。"
            ],
            warnings=[],
        )

    docker_info = snapshot.docker_info
    stats_rows = snapshot.stats_rows
    disk_usage_by_name = snapshot.disk_usage_by_name
    port_container_rows = snapshot.port_container_rows
    disk_info = snapshot.disk_info
    total_cpu = float(docker_info.get("NCPU") or 0)
    total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)
    total_disk_gb = float(disk_info.get("total_gb") or _disk_total_from_docker_info(docker_info) or 0)
    cpu_platform_limit = platform_allocatable_limit(total_cpu)
    memory_platform_limit = platform_allocatable_limit(total_memory_gb)
    disk_platform_limit = platform_allocatable_limit(total_disk_gb)

    query = db.query(Allocation).filter(
        Allocation.host_id == host.id,
        Allocation.status.in_(ACTIVE_STATUSES),
    )
    target_allocation = None
    if exclude_allocation_id is not None:
        target_allocation = (
            db.query(Allocation)
            .filter(Allocation.id == exclude_allocation_id)
            .first()
        )
        query = query.filter(Allocation.id != exclude_allocation_id)
    other_allocations = query.all()

    stats_by_name = {
        _normalize_container_name(item.get("Name") or item.get("container_name")): item
        for item in stats_rows
        if _normalize_container_name(item.get("Name") or item.get("container_name"))
    }

    def usage_for_name(name: str) -> tuple[float, float, float]:
        normalized_name = _normalize_container_name(name)
        stats = stats_by_name.get(normalized_name, {})
        cpu_cores = parse_percent(str(stats.get("CPUPerc") or "")) / 100
        memory_gb = parse_memory_usage(str(stats.get("MemUsage") or ""))
        disk_gb = float((disk_usage_by_name.get(normalized_name) or {}).get("disk_used_gb") or 0.0)
        return cpu_cores, memory_gb, disk_gb

    other_registered_names = {_normalize_container_name(allocation.container_name) for allocation in other_allocations}
    target_name = _normalize_container_name(target_allocation.container_name if target_allocation else "")
    target_actual_cpu, target_actual_memory, target_actual_disk = usage_for_name(target_name)

    reserved_cpu = 0.0
    reserved_memory = 0.0
    reserved_disk = 0.0
    guaranteed_cpu = 0.0
    guaranteed_memory = 0.0
    guaranteed_disk = 0.0
    actual_cpu = 0.0
    actual_memory = 0.0
    actual_disk = 0.0

    for allocation in other_allocations:
        row_actual_cpu, row_actual_memory, row_actual_disk = usage_for_name(allocation.container_name)
        row_guarantee_cpu = guarantee_from_elastic(allocation.cpu_limit_cores)
        row_guarantee_memory = guarantee_from_elastic(allocation.memory_limit_gb)
        row_guarantee_disk = guarantee_from_elastic(allocation.workspace_limit_gb)
        actual_cpu += row_actual_cpu
        actual_memory += row_actual_memory
        actual_disk += row_actual_disk
        guaranteed_cpu += row_guarantee_cpu
        guaranteed_memory += row_guarantee_memory
        guaranteed_disk += row_guarantee_disk
        reserved_cpu += max(row_actual_cpu, row_guarantee_cpu)
        reserved_memory += max(row_actual_memory, row_guarantee_memory)
        reserved_disk += max(row_actual_disk, row_guarantee_disk)

    known_platform_names = set(other_registered_names)
    if target_name:
        known_platform_names.add(target_name)
    for name in set(stats_by_name) | set(disk_usage_by_name):
        if not name or name in known_platform_names:
            continue
        row_actual_cpu, row_actual_memory, row_actual_disk = usage_for_name(name)
        actual_cpu += row_actual_cpu
        actual_memory += row_actual_memory
        actual_disk += row_actual_disk
        reserved_cpu += row_actual_cpu
        reserved_memory += row_actual_memory
        reserved_disk += row_actual_disk

    for row in port_container_rows:
        name = _normalize_container_name(row.get("container_name"))
        if not name or name in known_platform_names or name in stats_by_name or name in disk_usage_by_name:
            continue
        row_actual_cpu, row_actual_memory, row_actual_disk = usage_for_name(name)
        actual_cpu += row_actual_cpu
        actual_memory += row_actual_memory
        actual_disk += row_actual_disk
        reserved_cpu += row_actual_cpu
        reserved_memory += row_actual_memory
        reserved_disk += row_actual_disk

    requested_cpu = float(cpu_limit_cores or 0.0)
    requested_memory = float(memory_limit_gb or 0.0)
    requested_disk = float(workspace_limit_gb or 0.0)
    requested_cpu_total = reserved_cpu + requested_cpu
    requested_memory_total = reserved_memory + requested_memory
    requested_disk_total = reserved_disk + requested_disk
    requested_cpu_guarantee = guarantee_from_elastic(requested_cpu)
    requested_memory_guarantee = guarantee_from_elastic(requested_memory)
    requested_disk_guarantee = guarantee_from_elastic(requested_disk)

    reasons: list[str] = []
    warnings: list[str] = list(snapshot.warnings)
    if requested_cpu < 1:
        reasons.append("CPU 弹性上限至少为 1 核。")
    if requested_memory < 1:
        reasons.append("内存弹性上限至少为 1 GB。")
    if requested_disk < 1:
        reasons.append("workspace 弹性上限至少为 1 GB。")

    if exclude_allocation_id is not None:
        if requested_memory < target_actual_memory:
            reasons.append(
                "内存弹性上限低于当前容器实际占用："
                f"当前容器正在使用 {target_actual_memory:.1f} GB，本次设置 {requested_memory:.1f} GB。"
            )
        if requested_disk < target_actual_disk:
            reasons.append(
                "workspace 弹性上限低于当前 Docker 磁盘占用："
                f"当前容器已占用 {target_actual_disk:.1f} GB，本次设置 {requested_disk:.1f} GB。"
            )
        if requested_cpu < target_actual_cpu:
            warnings.append(
                "本次 CPU 弹性上限低于当前瞬时 CPU 占用，更新后 Docker 会对该容器进行限速。"
            )

    if total_cpu and requested_cpu_total > cpu_platform_limit:
        reasons.append(
            "CPU 弹性准入失败："
            f"其他容器需保留 {reserved_cpu:.1f} 核（实时占用与保障额度取较大值），"
            f"本次弹性上限 {requested_cpu:.1f} 核，合计 {requested_cpu_total:.1f} 核，"
            f"平台额定可分配上限 {cpu_platform_limit:.1f} 核（宿主机预留15%）。"
        )
    if total_memory_gb and requested_memory_total > memory_platform_limit:
        reasons.append(
            "内存弹性准入失败："
            f"其他容器需保留 {reserved_memory:.1f} GB（实时占用与保障额度取较大值），"
            f"本次弹性上限 {requested_memory:.1f} GB，合计 {requested_memory_total:.1f} GB，"
            f"平台额定可分配上限 {memory_platform_limit:.1f} GB（宿主机预留15%）。"
        )
    if not total_disk_gb:
        if (host.workspace_limit_mode or "metadata_only").strip().lower() == "strict_storage_opt":
            reasons.append("无法读取宿主机工作目录磁盘容量，严格磁盘限额模式下暂不允许分配。")
        else:
            warnings.append("无法读取宿主机工作目录磁盘容量，已降级为仅记录 workspace 弹性上限，不阻断容器创建。")
    elif requested_disk_total > disk_platform_limit:
        reasons.append(
            "workspace 弹性准入失败："
            f"其他容器需保留 {reserved_disk:.1f} GB（实时占用与保障额度取较大值），"
            f"本次弹性上限 {requested_disk:.1f} GB，合计 {requested_disk_total:.1f} GB，"
            f"平台额定可分配上限 {disk_platform_limit:.1f} GB（宿主机预留15%）。"
        )
    warnings.append(
        "准入规则：本次输入作为弹性上限；保障额度按弹性上限的 1/4 向下取整且最低为 1。"
        f"本次保障额度为 CPU {requested_cpu_guarantee} 核 / 内存 {requested_memory_guarantee} GB / workspace {requested_disk_guarantee} GB。"
    )
    warnings.append(
        "其他容器保留量按 max(实时占用, 保障额度) 计算；"
        f"当前其他容器实时占用约 CPU {actual_cpu:.1f} 核 / 内存 {actual_memory:.1f} GB / workspace {actual_disk:.1f} GB，"
        f"保障额度合计 CPU {guaranteed_cpu:.1f} 核 / 内存 {guaranteed_memory:.1f} GB / workspace {guaranteed_disk:.1f} GB。"
    )
    return AdmissionDecision(allowed=not reasons, reasons=reasons, warnings=warnings)

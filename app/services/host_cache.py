from __future__ import annotations

import json
from datetime import datetime
from threading import Lock, Thread

from app.db import SessionLocal
from app.models import HostStatusCache, ManagedHost
from app.services.docker_engine import DockerService
from app.services.ssh_client import RunnerError


_refresh_lock = Lock()
_refreshing_hosts: set[int] = set()


def _heavy_cache_ready(cache: HostStatusCache | None) -> bool:
    return bool(
        cache
        and (
            (
                cache.stable_stats_json is not None
                and cache.stable_gpu_detail_json is not None
                and cache.stable_disk_usage_json is not None
            )
            or (
                cache.stats_json is not None
                and cache.gpu_detail_json is not None
                and cache.disk_usage_json is not None
            )
        )
    )


def _ensure_cache(db, host_id: int) -> HostStatusCache:
    cache = db.query(HostStatusCache).filter(HostStatusCache.host_id == host_id).first()
    if cache is None:
        cache = HostStatusCache(host_id=host_id)
        db.add(cache)
    return cache


def _mark_refresh_started(cache: HostStatusCache) -> None:
    cache.refresh_in_progress = True
    cache.refresh_started_at = datetime.utcnow()


def _mark_refresh_failed(cache: HostStatusCache, error_log: str) -> None:
    cache.reachable = False
    cache.error_log = error_log
    cache.refreshed_at = datetime.utcnow()
    cache.refresh_in_progress = False
    cache.refresh_completed_at = cache.refreshed_at


def _promote_dynamic_to_stable(cache: HostStatusCache) -> None:
    cache.stable_reachable = cache.reachable
    cache.stable_docker_info_json = cache.docker_info_json
    cache.stable_container_rows_json = cache.container_rows_json
    cache.stable_stats_json = cache.stats_json
    cache.stable_gpus_json = cache.gpus_json
    cache.stable_gpu_detail_json = cache.gpu_detail_json
    cache.stable_disk_usage_json = cache.disk_usage_json
    cache.stable_error_log = cache.error_log
    cache.stable_refreshed_at = cache.refreshed_at
    cache.refresh_in_progress = False
    cache.refresh_completed_at = cache.refreshed_at


def _docker_info_with_root_disk(docker: DockerService, timeout: int) -> dict:
    docker_info = docker.docker_info(timeout=timeout)
    try:
        docker_info["_root_disk_usage"] = docker.filesystem_usage_gb("/", timeout=timeout)
    except Exception:
        docker_info["_root_disk_usage"] = {}
    try:
        docker_info["_workspace_disk_usage"] = docker.filesystem_usage_gb(docker.host.workspace_root, timeout=timeout)
    except Exception:
        docker_info["_workspace_disk_usage"] = {}
    return docker_info


def schedule_host_refresh(host_id: int, full: bool = False) -> bool:
    with _refresh_lock:
        if host_id in _refreshing_hosts:
            return False
        _refreshing_hosts.add(host_id)

    def runner() -> None:
        try:
            if full:
                refresh_host_status_cache_once(host_id)
            else:
                refresh_host_status_cache_fast(host_id)
        finally:
            with _refresh_lock:
                _refreshing_hosts.discard(host_id)

    Thread(target=runner, daemon=True).start()
    return True


def refresh_host_status_cache_once(host_id: int) -> None:
    db = SessionLocal()
    try:
        host = db.query(ManagedHost).filter(ManagedHost.id == host_id).first()
        if not host or not host.enabled:
            return
        cache = _ensure_cache(db, host.id)
        _mark_refresh_started(cache)
        db.commit()
        try:
            docker = DockerService(host)
            reachable = docker.ping(timeout=4)
            cache.reachable = reachable
            if reachable:
                docker_info = _docker_info_with_root_disk(docker, timeout=6)
                container_rows = docker.managed_container_rows(host.port_start, host.port_end, timeout=8)
                stats = docker.list_container_stats(timeout=8)
                gpus = docker.gpu_stats(timeout=6)
                gpu_detail = docker.gpu_memory_detail_by_container(timeout=8)
                disk_usage = docker.container_disk_usage_gb_map(timeout=8)
                cache.docker_info_json = json.dumps(docker_info, ensure_ascii=False)
                cache.container_rows_json = json.dumps(container_rows, ensure_ascii=False)
                cache.stats_json = json.dumps(stats, ensure_ascii=False)
                cache.gpus_json = json.dumps(gpus, ensure_ascii=False)
                cache.gpu_detail_json = json.dumps(gpu_detail, ensure_ascii=False)
                cache.disk_usage_json = json.dumps(disk_usage, ensure_ascii=False)
                cache.error_log = ""
                cache.refreshed_at = datetime.utcnow()
                _promote_dynamic_to_stable(cache)
            else:
                _mark_refresh_failed(cache, "宿主机不可达。")
        except RunnerError as exc:
            _mark_refresh_failed(cache, exc.log_text() or str(exc))
        except Exception as exc:
            _mark_refresh_failed(cache, f"{type(exc).__name__}: {exc}")
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

def refresh_host_status_cache_fast(host_id: int) -> None:
    db = SessionLocal()
    try:
        host = db.query(ManagedHost).filter(ManagedHost.id == host_id).first()
        if not host or not host.enabled:
            return
        cache = _ensure_cache(db, host.id)
        if not _heavy_cache_ready(cache):
            cache.refresh_in_progress = False
            db.commit()
            db.close()
            refresh_host_status_cache_once(host_id)
            return
        _mark_refresh_started(cache)
        db.commit()
        try:
            docker = DockerService(host)
            reachable = docker.ping(timeout=3)
            cache.reachable = reachable
            if reachable:
                docker_info = _docker_info_with_root_disk(docker, timeout=4)
                stats = docker.list_container_stats(timeout=5)
                gpus = docker.gpu_stats(timeout=4)
                container_rows = docker.managed_container_rows(host.port_start, host.port_end, timeout=5)
                gpu_detail = docker.gpu_memory_detail_by_container(timeout=5)
                disk_usage = docker.container_disk_usage_gb_map(timeout=5)
                cache.docker_info_json = json.dumps(docker_info, ensure_ascii=False)
                cache.container_rows_json = json.dumps(container_rows, ensure_ascii=False)
                cache.stats_json = json.dumps(stats, ensure_ascii=False)
                cache.gpus_json = json.dumps(gpus, ensure_ascii=False)
                cache.gpu_detail_json = json.dumps(gpu_detail, ensure_ascii=False)
                cache.disk_usage_json = json.dumps(disk_usage, ensure_ascii=False)
                cache.error_log = ""
                cache.refreshed_at = datetime.utcnow()
                _promote_dynamic_to_stable(cache)
            else:
                _mark_refresh_failed(cache, "宿主机不可达。")
        except RunnerError as exc:
            _mark_refresh_failed(cache, exc.log_text() or str(exc))
        except Exception as exc:
            _mark_refresh_failed(cache, f"{type(exc).__name__}: {exc}")
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
from zoneinfo import ZoneInfo
from datetime import datetime

from fastapi import APIRouter, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import AdminStatus, AdminUser, Allocation, AllocationStatus, AuthType, HostStatusCache, ManagedHost, SnapshotRecord
from app.services.admission import (
    check_allocation,
    guarantee_from_elastic,
    recommended_defaults,
    reservation_summary_from_rows,
)
from app.services.docker_engine import DockerService, slugify
from app.services.host_cache import schedule_host_refresh
from app.services.image_filters import filter_supported_base_images, is_supported_base_image
from app.services.metrics import parse_memory_usage, parse_percent
from app.services.snapshots import create_snapshot_record, effective_snapshot_policy
from app.services.ssh_client import RunnerError, get_runner
from app.services.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    USER_SESSION_COOKIE,
    authenticate_platform_user,
    client_ip,
    password_hash,
    read_session,
    read_terminal_token,
    send_registration_email,
    sign_session,
    sign_terminal_token,
    verify_password,
)


router = APIRouter()
settings = get_settings()
beijing_tz = ZoneInfo("Asia/Shanghai")
HOST_REFRESH_INTERVAL_SECONDS = 15 * 60
ARCHIVED_HOST_NAME_PREFIX = "已移除-"


def render(request: Request, template_name: str, context: dict):
    context.setdefault("admin", getattr(request.state, "admin", None))
    context.setdefault("platform_user", getattr(request.state, "platform_user", None))
    context.setdefault("is_user_view", bool(getattr(request.state, "platform_user", None)))
    return request.app.state.templates.TemplateResponse(
        request,
        template_name,
        context,
    )


def split_error_payload(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return "操作失败。", ""
    parts = [part.strip() for part in text.splitlines() if part.strip()]
    if not parts:
        return "操作失败。", text
    return parts[0], text


def chinese_error_summary(text: str, fallback_message: str = "操作失败。") -> str:
    raw = (text or "").strip()
    lower = raw.lower()
    if not raw:
        return fallback_message
    if "address already in use" in lower or "port is already allocated" in lower or "bind" in lower and "already in use" in lower:
        return "端口已被占用，请更换端口或先清理占用该端口的旧容器。"
    if "no such image" in lower or "pull access denied" in lower or "not found" in lower and "image" in lower:
        return "宿主机本地没有找到指定基础镜像，请重新选择本机已有的 PyTorch/CUDA 镜像。"
    if "no space left on device" in lower:
        return "宿主机磁盘空间不足，Docker 无法继续写入数据。"
    if "minimum memory limit" in lower or "memoryswap" in lower or "memory" in lower and "invalid" in lower:
        return "Docker 内存上限参数不合法，请调高内存弹性上限后重试。"
    if "cannot update memory limit" in lower or "memory limit should be smaller" in lower:
        return "Docker 拒绝更新内存上限，可能低于当前容器占用或低于 Docker 允许范围。"
    if "invalid argument" in lower and "--cpus" in lower:
        return "CPU 弹性上限参数不合法，请填写大于 0 的整数核数。"
    if "name is already in use" in lower or "conflict" in lower and "container name" in lower:
        return "容器名已被占用，平台已尝试清理残留；如仍失败，请检查同名容器。"
    if "driver/library version mismatch" in lower or "failed to initialize nvml" in lower:
        return "NVIDIA 驱动与 NVML 库版本不匹配，宿主机 GPU 运行环境需要管理员修复。"
    if "nvidia-container" in lower or "nvidia runtime" in lower:
        return "NVIDIA 容器运行时异常，平台已尝试自动修复；仍失败时需要检查宿主机 GPU runtime。"
    if "permission denied" in lower:
        return "权限不足，当前 SSH 用户无法完成该 Docker 操作。"
    if "connection timed out" in lower or "timed out" in lower or "timeout" in lower:
        return "后端等待宿主机命令超时，请检查 SSH、Docker 负载或稍后重试。"
    if "ssh" in lower and ("authentication" in lower or "auth" in lower):
        return "SSH 认证失败，请检查账号、密码或私钥配置。"
    if "ssh" in lower:
        return "SSH 连接或远端命令执行失败，请检查宿主机网络和 SSH 服务。"
    if raw and not any("\u4e00" <= ch <= "\u9fff" for ch in raw):
        return f"{fallback_message} 后端返回英文错误，已保留原始日志供排查。"
    return fallback_message


def localize_error_log(text: str, fallback_message: str = "操作失败。") -> str:
    raw = (text or "").strip()
    summary = chinese_error_summary(raw, fallback_message)
    if not raw:
        return summary
    if raw.startswith("中文解释："):
        return raw
    if summary and summary not in raw:
        return f"中文解释：{summary}\n\n原始后端日志：\n{raw}"
    return raw


def runner_error_payload(exc: RunnerError, fallback_message: str) -> tuple[str, str]:
    error_log = exc.log_text() or str(exc).strip()
    summary = chinese_error_summary(error_log, fallback_message.strip() or "操作失败。")
    if summary == (fallback_message.strip() or "操作失败。") and str(exc).strip():
        summary = split_error_payload(str(exc).strip())[0]
    return summary, localize_error_log(error_log, summary)


def ajax_response(request: Request, ok: bool, message: str, error_log: str = "", status_code: int = 200):
    if wants_json(request):
        payload = {"ok": ok, "message": message}
        if error_log:
            payload["error_log"] = error_log
        return JSONResponse(payload, status_code=status_code)
    return None


def operation_success(request: Request, message: str, redirect_url: str):
    if wants_json(request):
        return JSONResponse({"ok": True, "message": message})
    return RedirectResponse(redirect_url, status_code=303)


def operation_error(request: Request, message: str, redirect_url: str, status_code: int = 200):
    if wants_json(request):
        summary = chinese_error_summary(message, split_error_payload(message)[0])
        error_log = localize_error_log(message, summary)
        return JSONResponse(
            {"ok": False, "message": summary, "error_log": error_log},
            status_code=status_code,
        )
    return RedirectResponse(f"{redirect_url}?error={message}", status_code=303)


def wants_json(request: Request) -> bool:
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )


def active_platform_user(request: Request) -> dict | None:
    return getattr(request.state, "platform_user", None)


def require_admin_response(request: Request, redirect_url: str = "/"):
    if getattr(request.state, "admin", None):
        return None
    message = "当前账号无管理权限。"
    if wants_json(request):
        return JSONResponse(
            {"ok": False, "message": message, "error_log": "普通使用者禁止执行维护和管理操作。"},
            status_code=403,
        )
    return RedirectResponse(f"{redirect_url}?error={message}", status_code=303)


def user_allocation_ids(db: Session, account: str) -> set[int]:
    rows = (
        db.query(Allocation.id)
        .filter(
            Allocation.assignee == account,
            Allocation.status != AllocationStatus.DELETED.value,
        )
        .all()
    )
    return {int(row[0]) for row in rows}


def allocation_owner_response(request: Request, allocation: Allocation):
    platform_user = active_platform_user(request)
    if not platform_user:
        return None
    if allocation.assignee == platform_user.get("account"):
        return None
    message = "当前账号不能访问该容器。"
    if wants_json(request):
        return JSONResponse({"ok": False, "message": message, "error_log": message}, status_code=403)
    return RedirectResponse("/?error=当前账号不能访问该容器。", status_code=303)


def websocket_client_ip(websocket: WebSocket) -> str:
    forwarded = websocket.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return websocket.client.host if websocket.client else ""


def websocket_cookies(websocket: WebSocket) -> dict[str, str]:
    cookies: dict[str, str] = {}
    cookie_header = websocket.headers.get("cookie") or ""
    for chunk in cookie_header.split(";"):
        if "=" in chunk:
            key, value = chunk.split("=", 1)
            cookies[key.strip()] = value.strip()
    return cookies


def read_websocket_session(cookies: dict[str, str], cookie_name: str, websocket: WebSocket) -> str | None:
    value = cookies.get(cookie_name)
    account = read_session(value, websocket_client_ip(websocket))
    if account:
        return account
    return read_session(value, None, verify_ip=False)


def websocket_admin(cookies: dict[str, str], websocket: WebSocket, db: Session) -> dict | None:
    account = read_websocket_session(cookies, SESSION_COOKIE, websocket)
    if not account:
        return None
    return admin_from_account(db, account)


def admin_from_account(db: Session, account: str | None) -> dict | None:
    if not account:
        return None
    if account == settings.root_admin_account:
        return {"account": account, "is_root": True}
    admin_user = db.query(AdminUser).filter(AdminUser.account == account).first()
    if admin_user and admin_user.status == AdminStatus.APPROVED.value:
        return {"account": account, "is_root": False}
    return None


def terminal_token_account(websocket: WebSocket, scope: str, resource_id: int) -> str | None:
    token = websocket.query_params.get("token")
    return read_terminal_token(token, scope, resource_id)


def log_response(lines: list[str], ok: bool = True, message: str = "", error_log: str = "", status_code: int = 200):
    payload = {"ok": ok, "message": message, "logs": lines}
    if error_log:
        payload["error_log"] = error_log
    return JSONResponse(payload, status_code=status_code)


def stream_event(event: str, **payload) -> str:
    data = {"event": event, **payload}
    return json.dumps(data, ensure_ascii=False) + "\n"


def visible_hosts_query(db: Session):
    return (
        db.query(ManagedHost)
        .filter(ManagedHost.enabled == True)  # noqa: E712
        .filter(~ManagedHost.name.startswith(ARCHIVED_HOST_NAME_PREFIX))
    )


def visible_host_by_id(db: Session, host_id: int) -> ManagedHost | None:
    return visible_hosts_query(db).filter(ManagedHost.id == host_id).first()


def ssh_host_count(db: Session) -> int:
    return (
        visible_hosts_query(db)
        .filter(ManagedHost.auth_type.in_([AuthType.PASSWORD.value, AuthType.KEY.value]))
        .count()
    )


def parse_cache_json(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def host_cache_payload(host: ManagedHost, cache: HostStatusCache | None = None, stable: bool = True) -> dict:
    cache = cache or host.status_cache
    use_stable = bool(stable and cache and cache.stable_refreshed_at)
    return {
        "reachable": bool(cache.stable_reachable if use_stable else cache.reachable) if cache else False,
        "docker_info": parse_cache_json(cache.stable_docker_info_json if use_stable else cache.docker_info_json, {}) if cache else {},
        "container_rows": parse_cache_json(cache.stable_container_rows_json if use_stable else cache.container_rows_json, []) if cache else [],
        "stats": parse_cache_json(cache.stable_stats_json if use_stable else cache.stats_json, []) if cache else [],
        "gpus": parse_cache_json(cache.stable_gpus_json if use_stable else cache.gpus_json, []) if cache else [],
        "gpu_detail_by_name": parse_cache_json(cache.stable_gpu_detail_json if use_stable else cache.gpu_detail_json, {}) if cache else {},
        "disk_usage_by_name": parse_cache_json(cache.stable_disk_usage_json if use_stable else cache.disk_usage_json, {}) if cache else {},
        "error_log": (cache.stable_error_log if use_stable else cache.error_log) if cache else "",
        "refreshed_at": (cache.stable_refreshed_at if use_stable else cache.refreshed_at) if cache else None,
        "dynamic_refreshed_at": cache.refreshed_at if cache else None,
        "refresh_in_progress": bool(cache.refresh_in_progress) if cache else False,
        "refresh_started_at": cache.refresh_started_at if cache else None,
        "refresh_completed_at": cache.refresh_completed_at if cache else None,
        "using_stable": use_stable,
    }


def cache_needs_full_refresh(host: ManagedHost, cached: dict, db: Session) -> bool:
    if not cached["refreshed_at"]:
        return True
    if cached.get("refresh_in_progress"):
        return False
    allocations_exist = db.query(Allocation.id).filter(Allocation.host_id == host.id).first() is not None
    if not allocations_exist:
        return False
    if not cached["container_rows"]:
        return True
    if not cached["gpus"]:
        return True
    if not cached["stats"] and allocations_exist:
        return True
    if not cached["gpu_detail_by_name"] or not cached["disk_usage_by_name"]:
        return True
    return False


def format_cache_time(value: datetime | None) -> str:
    if not value:
        return "暂无缓存"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")


def active_host_allocations(db_allocations: list[Allocation]) -> list[Allocation]:
    return [
        allocation
        for allocation in db_allocations
        if allocation.status != AllocationStatus.DELETED.value
    ]


def host_port_pool_state(host: ManagedHost, db_allocations: list[Allocation], cached_container_rows: list[dict]) -> dict:
    active_allocations = active_host_allocations(db_allocations)
    deleted_container_names = {
        allocation.container_name
        for allocation in db_allocations
        if allocation.status == AllocationStatus.DELETED.value and allocation.container_name
    }
    used_ports = {
        int(allocation.host_port)
        for allocation in active_allocations
        if host.port_start <= int(allocation.host_port) <= host.port_end
    }
    for row in cached_container_rows:
        container_name = str(row.get("container_name") or "")
        if container_name in deleted_container_names:
            continue
        try:
            host_port = int(row.get("host_port"))
        except (TypeError, ValueError):
            continue
        if host.port_start <= host_port <= host.port_end:
            used_ports.add(host_port)
    available_ports = [
        port
        for port in range(host.port_start, host.port_end + 1)
        if port not in used_ports
    ]
    return {
        "used_ports": sorted(used_ports),
        "available_ports": available_ports,
    }


def remove_cached_container_row(db: Session, host_id: int, container_name: str) -> bool:
    target = container_name.strip().lstrip("/")
    if not target:
        return False
    cache = db.query(HostStatusCache).filter(HostStatusCache.host_id == host_id).first()
    if not cache:
        return False

    def matches_name(value: object) -> bool:
        return str(value or "").strip().lstrip("/") == target

    def prune_row_list(raw: str | None, name_keys: tuple[str, ...]) -> tuple[str | None, bool]:
        rows = parse_cache_json(raw, [])
        if not isinstance(rows, list):
            return raw, False
        next_rows = [
            row
            for row in rows
            if not any(matches_name(row.get(key)) for key in name_keys if isinstance(row, dict))
        ]
        if len(next_rows) == len(rows):
            return raw, False
        return json.dumps(next_rows, ensure_ascii=False), True

    def prune_mapping(raw: str | None) -> tuple[str | None, bool]:
        mapping = parse_cache_json(raw, {})
        if not isinstance(mapping, dict):
            return raw, False
        next_mapping = {
            key: value
            for key, value in mapping.items()
            if not matches_name(key)
        }
        if len(next_mapping) == len(mapping):
            return raw, False
        return json.dumps(next_mapping, ensure_ascii=False), True

    changed = False
    for attr, keys in (
        ("container_rows_json", ("container_name", "Name")),
        ("stable_container_rows_json", ("container_name", "Name")),
        ("stats_json", ("Name", "container_name")),
        ("stable_stats_json", ("Name", "container_name")),
    ):
        next_value, attr_changed = prune_row_list(getattr(cache, attr), keys)
        if attr_changed:
            setattr(cache, attr, next_value)
            changed = True
    for attr in (
        "gpu_detail_json",
        "stable_gpu_detail_json",
        "disk_usage_json",
        "stable_disk_usage_json",
    ):
        next_value, attr_changed = prune_mapping(getattr(cache, attr))
        if attr_changed:
            setattr(cache, attr, next_value)
            changed = True
    if changed:
        cache.updated_at = datetime.utcnow()
    return changed


def ensure_host_status_cache(db: Session, host_id: int) -> HostStatusCache:
    for instance in list(db.identity_map.values()) + list(db.new):
        if isinstance(instance, HostStatusCache) and instance.host_id == host_id:
            return instance
    cache = db.query(HostStatusCache).filter(HostStatusCache.host_id == host_id).first()
    if cache is None:
        cache = HostStatusCache(host_id=host_id)
        db.add(cache)
    return cache


def update_cached_container_status(
    db: Session,
    host_id: int,
    container_name: str,
    status: str,
    host_port: int | None = None,
    image_name: str | None = None,
) -> bool:
    target = container_name.strip().lstrip("/")
    if not target:
        return False
    if status == AllocationStatus.DELETED.value:
        return remove_cached_container_row(db, host_id, target)

    cache = ensure_host_status_cache(db, host_id)
    if cache is None:
        if host_port is None:
            return False

    resolved_port = int(host_port) if host_port is not None else None

    def row_matches(row: dict) -> bool:
        row_name = str(row.get("container_name") or row.get("Name") or "").strip().lstrip("/")
        if row_name == target:
            return True
        if resolved_port is None:
            return False
        try:
            return int(row.get("host_port")) == resolved_port
        except (TypeError, ValueError):
            return False

    def update_row_list(raw: str | None) -> tuple[str, bool]:
        rows = parse_cache_json(raw, [])
        if not isinstance(rows, list):
            rows = []
        changed = False
        found = False
        next_rows = []
        for row in rows:
            if not isinstance(row, dict) or not row_matches(row):
                next_rows.append(row)
                continue
            current = dict(row)
            current["container_name"] = target
            current["status"] = status
            current["cache_pending"] = True
            if resolved_port is not None:
                current["host_port"] = resolved_port
            if image_name:
                current["image_name"] = image_name
            detail = current.get("detail")
            if isinstance(detail, dict):
                state = detail.setdefault("State", {})
                if isinstance(state, dict):
                    state["Running"] = status == AllocationStatus.RUNNING.value
                    state["Status"] = "running" if status == AllocationStatus.RUNNING.value else "exited"
            next_rows.append(current)
            found = True
            changed = True
        if not found and resolved_port is not None:
            next_rows.append(
                {
                    "container_name": target,
                    "host_port": resolved_port,
                    "status": status,
                    "image_name": image_name or "",
                    "cache_pending": True,
                }
            )
            changed = True
        return json.dumps(next_rows, ensure_ascii=False), changed

    def prune_row_list(raw: str | None) -> tuple[str | None, bool]:
        rows = parse_cache_json(raw, [])
        if not isinstance(rows, list):
            return raw, False
        next_rows = [
            row
            for row in rows
            if not (
                isinstance(row, dict)
                and str(row.get("Name") or row.get("container_name") or "").strip().lstrip("/") == target
            )
        ]
        if len(next_rows) == len(rows):
            return raw, False
        return json.dumps(next_rows, ensure_ascii=False), True

    def prune_mapping(raw: str | None) -> tuple[str | None, bool]:
        mapping = parse_cache_json(raw, {})
        if not isinstance(mapping, dict) or target not in mapping:
            return raw, False
        next_mapping = dict(mapping)
        next_mapping.pop(target, None)
        return json.dumps(next_mapping, ensure_ascii=False), True

    changed = False
    for attr in ("container_rows_json", "stable_container_rows_json"):
        next_value, attr_changed = update_row_list(getattr(cache, attr))
        if attr_changed:
            setattr(cache, attr, next_value)
            changed = True

    if status != AllocationStatus.RUNNING.value:
        for attr in ("stats_json", "stable_stats_json"):
            next_value, attr_changed = prune_row_list(getattr(cache, attr))
            if attr_changed:
                setattr(cache, attr, next_value)
                changed = True
        for attr in ("gpu_detail_json", "stable_gpu_detail_json"):
            next_value, attr_changed = prune_mapping(getattr(cache, attr))
            if attr_changed:
                setattr(cache, attr, next_value)
                changed = True

    if changed:
        now = datetime.utcnow()
        cache.reachable = True
        cache.stable_reachable = True
        cache.updated_at = now
        cache.refreshed_at = cache.refreshed_at or now
        cache.stable_refreshed_at = cache.stable_refreshed_at or now
    return changed


def upsert_cached_container_row(
    db: Session,
    host_id: int,
    container_name: str,
    host_port: int,
    image_name: str,
    status: str,
) -> bool:
    name = container_name.strip().lstrip("/")
    if not name:
        return False
    cache = ensure_host_status_cache(db, host_id)

    next_row = {
        "container_name": name,
        "host_port": int(host_port),
        "status": status,
        "image_name": image_name,
        "cache_pending": True,
    }

    def merge_rows(raw: str | None) -> tuple[str, bool]:
        rows = parse_cache_json(raw, [])
        if not isinstance(rows, list):
            rows = []
        changed = False
        merged = []
        found = False
        for row in rows:
            if not isinstance(row, dict):
                merged.append(row)
                continue
            row_name = str(row.get("container_name") or row.get("Name") or "").strip().lstrip("/")
            row_port = row.get("host_port")
            try:
                row_port = int(row_port)
            except (TypeError, ValueError):
                row_port = None
            if row_name == name or row_port == int(host_port):
                current = {**row, **next_row}
                merged.append(current)
                found = True
                changed = True
            else:
                merged.append(row)
        if not found:
            merged.append(next_row)
            changed = True
        return json.dumps(merged, ensure_ascii=False), changed

    changed = False
    for attr in ("container_rows_json", "stable_container_rows_json"):
        next_value, attr_changed = merge_rows(getattr(cache, attr))
        if attr_changed:
            setattr(cache, attr, next_value)
            changed = True
    if changed:
        now = datetime.utcnow()
        cache.reachable = True
        cache.stable_reachable = True
        cache.updated_at = now
        cache.refreshed_at = cache.refreshed_at or now
        cache.stable_refreshed_at = cache.stable_refreshed_at or now
    return changed


def build_host_rows_from_cached_data(host: ManagedHost, db_allocations: list[Allocation], cached: dict) -> dict:
    container_rows = cached["container_rows"]
    stats_by_name = {item.get("Name"): item for item in cached["stats"]}
    active_allocations = active_host_allocations(db_allocations)
    deleted_container_names = {
        allocation.container_name
        for allocation in db_allocations
        if allocation.status == AllocationStatus.DELETED.value and allocation.container_name
    }
    allocation_by_container_name = {
        allocation.container_name: allocation for allocation in active_allocations
    }
    allocation_by_host_port = {
        allocation.host_port: allocation for allocation in active_allocations
    }
    gpu_detail_by_name = cached["gpu_detail_by_name"]
    disk_usage_by_name = cached["disk_usage_by_name"]
    allocation_rows = []
    unified_container_rows = []
    resource_chart_rows = []
    rendered_allocation_ids: set[int] = set()

    def append_resource_row(row: dict, allocation: Allocation | None) -> None:
        resource_chart_rows.append(
            {
                "port": row["host_port"],
                "assignee": row["assignee"],
                "status": row["status"],
                "cpu_percent": round(float(row.get("cpu_percent") or 0.0), 2),
                "memory_used_gb": round(float(row.get("memory_used_gb") or 0.0), 2),
                "gpu_memory_used_mb": round(float(row.get("gpu_memory_used_mb") or 0.0), 2),
                "gpu_memory_detail": row.get("gpu_memory_detail") or [],
                "disk_used_gb": round(float(row.get("disk_used_gb") or 0.0), 2),
                "disk_virtual_gb": round(float(row.get("disk_virtual_gb") or 0.0), 2),
                "cpu_limit_cores": round(float(allocation.cpu_limit_cores or 0.0), 2) if allocation else 0.0,
                "memory_limit_gb": round(float(allocation.memory_limit_gb or 0.0), 2) if allocation else 0.0,
                "workspace_limit_gb": round(float(allocation.workspace_limit_gb or 0.0), 2) if allocation else 0.0,
                "cpu_guarantee_cores": guarantee_from_elastic(allocation.cpu_limit_cores) if allocation else 0.0,
                "memory_guarantee_gb": guarantee_from_elastic(allocation.memory_limit_gb) if allocation else 0.0,
                "workspace_guarantee_gb": guarantee_from_elastic(allocation.workspace_limit_gb) if allocation else 0.0,
            }
        )

    for container_row in container_rows:
        if str(container_row.get("container_name") or "") in deleted_container_names:
            continue
        allocation = allocation_by_container_name.get(
            container_row["container_name"]
        ) or allocation_by_host_port.get(container_row["host_port"])
        stats = stats_by_name.get(container_row["container_name"], {})
        cpu_percent = parse_percent(stats.get("CPUPerc", ""))
        memory_used_gb = parse_memory_usage(stats.get("MemUsage", ""))
        gpu_memory_detail = gpu_detail_by_name.get(container_row["container_name"], [])
        gpu_memory_used_mb = sum(float(item.get("used_memory_mb") or 0.0) for item in gpu_memory_detail)
        disk_usage = disk_usage_by_name.get(container_row["container_name"], {})
        disk_used_gb = float(disk_usage.get("disk_used_gb") or 0.0)
        disk_virtual_gb = float(disk_usage.get("disk_virtual_gb") or 0.0)
        disk_size_text = str(disk_usage.get("disk_size_text") or "")

        if allocation:
            snapshot_keep_count, snapshot_interval_days = effective_snapshot_policy(allocation)
            allocation.status = container_row["status"]
            allocation.image_name = container_row["image_name"] or allocation.image_name
            row = {
                "registered": True,
                "allocation": allocation,
                "container_name": allocation.container_name,
                "host_port": allocation.host_port,
                "assignee": allocation.assignee,
                "purpose": allocation.purpose,
                "image_name": allocation.image_name,
                "status": allocation.status,
                "stats": stats,
                "cpu_percent": cpu_percent,
                "memory_used_gb": memory_used_gb,
                "gpu_memory_used_mb": gpu_memory_used_mb,
                "gpu_memory_detail": gpu_memory_detail,
                "disk_used_gb": disk_used_gb,
                "disk_virtual_gb": disk_virtual_gb,
                "disk_size_text": disk_size_text,
                "effective_snapshot_keep_count": snapshot_keep_count,
                "effective_snapshot_interval_days": snapshot_interval_days,
            }
            allocation_rows.append(row)
            rendered_allocation_ids.add(allocation.id)
        else:
            row = {
                "registered": False,
                "container_name": container_row["container_name"],
                "host_port": container_row["host_port"],
                "assignee": container_row["container_name"],
                "purpose": "原始设定",
                "status": container_row["status"],
                "image_name": container_row["image_name"],
                "cpu_percent": cpu_percent,
                "memory_used_gb": memory_used_gb,
                "gpu_memory_used_mb": gpu_memory_used_mb,
                "gpu_memory_detail": gpu_memory_detail,
                "disk_used_gb": disk_used_gb,
                "disk_virtual_gb": disk_virtual_gb,
                "disk_size_text": disk_size_text,
                "allocation": None,
                "stats": stats,
                "effective_snapshot_keep_count": host.snapshot_keep_count,
                "effective_snapshot_interval_days": host.snapshot_interval_days,
            }
        unified_container_rows.append(row)
        append_resource_row(row, allocation)

    for allocation in active_allocations:
        if allocation.id in rendered_allocation_ids:
            continue
        snapshot_keep_count, snapshot_interval_days = effective_snapshot_policy(allocation)
        row = {
            "registered": True,
            "allocation": allocation,
            "container_name": allocation.container_name,
            "host_port": allocation.host_port,
            "assignee": allocation.assignee,
            "purpose": allocation.purpose,
            "image_name": allocation.image_name,
            "status": allocation.status,
            "stats": {},
            "cpu_percent": 0.0,
            "memory_used_gb": 0.0,
            "gpu_memory_used_mb": 0.0,
            "gpu_memory_detail": [],
            "disk_used_gb": 0.0,
            "disk_virtual_gb": 0.0,
            "disk_size_text": "",
            "effective_snapshot_keep_count": snapshot_keep_count,
            "effective_snapshot_interval_days": snapshot_interval_days,
            "cache_pending": True,
        }
        allocation_rows.append(row)
        unified_container_rows.append(row)
        append_resource_row(row, allocation)
    return {
        "allocation_rows": allocation_rows,
        "unified_container_rows": sorted(unified_container_rows, key=lambda row: row["host_port"]),
        "resource_chart_rows": resource_chart_rows,
    }


def summarize_resource_reservations(rows_data: dict) -> dict:
    reservation = reservation_summary_from_rows(rows_data.get("resource_chart_rows") or [])
    gpu_actual_gb = (
        sum(float(row.get("gpu_memory_used_mb") or 0.0) for row in rows_data.get("resource_chart_rows") or [])
        / 1024
    )
    return {
        "allocated_cpu": reservation.elastic_cpu,
        "allocated_memory_gb": reservation.elastic_memory_gb,
        "allocated_disk_gb": reservation.elastic_disk_gb,
        "guaranteed_cpu": reservation.guarantee_cpu,
        "guaranteed_memory_gb": reservation.guarantee_memory_gb,
        "guaranteed_disk_gb": reservation.guarantee_disk_gb,
        "reserved_cpu": reservation.reserved_cpu,
        "reserved_memory_gb": reservation.reserved_memory_gb,
        "reserved_disk_gb": reservation.reserved_disk_gb,
        "actual_cpu": reservation.actual_cpu,
        "actual_memory_gb": reservation.actual_memory_gb,
        "actual_disk_gb": reservation.actual_disk_gb,
        "allocated_payload": reservation.elastic_payload(gpu_actual_gb),
        "guaranteed_payload": reservation.guarantee_payload(),
        "reserved_payload": reservation.reserved_payload(),
    }


def compute_visual_payload(db: Session) -> dict:
    hosts = visible_hosts_query(db).order_by(ManagedHost.name.asc()).all()
    rows = []
    total_gpu_used_mb = 0.0
    total_gpu_memory_mb = 0.0
    total_containers = 0
    for host in hosts:
        active_allocations = (
            db.query(Allocation)
            .filter(
                Allocation.host_id == host.id,
                Allocation.status.in_(
                    [
                        AllocationStatus.PENDING.value,
                        AllocationStatus.RUNNING.value,
                        AllocationStatus.STOPPED.value,
                    ]
                ),
            )
            .all()
        )
        running_count = sum(1 for allocation in active_allocations if allocation.status == AllocationStatus.RUNNING.value)
        container_count = len(active_allocations)
        total_containers += container_count
        cached = host_cache_payload(host)
        gpus = cached["gpus"]
        gpu_used_mb = sum(float(gpu.get("memory_used_mb") or 0.0) for gpu in gpus)
        gpu_total_mb = sum(float(gpu.get("memory_total_mb") or 0.0) for gpu in gpus)
        gpu_percent = (gpu_used_mb / gpu_total_mb * 100) if gpu_total_mb else 0.0
        total_gpu_used_mb += gpu_used_mb
        total_gpu_memory_mb += gpu_total_mb
        rows.append(
            {
                "id": host.id,
                "name": host.name,
                "address": host.address,
                "allowed": True,
                "container_count": container_count,
                "running_count": running_count,
                "gpu_count": len(gpus),
                "last_status_at": format_cache_time(cached["refreshed_at"]),
                "gpu_used_mb": round(gpu_used_mb, 2),
                "gpu_total_mb": round(gpu_total_mb, 2),
                "gpu_percent": round(gpu_percent, 2),
                "load": gpu_percent,
            }
        )
    overall_gpu_percent = (total_gpu_used_mb / total_gpu_memory_mb * 100) if total_gpu_memory_mb else 0.0
    for row in rows:
        row["intensity"] = round(float(row["gpu_percent"] or 0.0) / 100, 3)
    return {
        "ok": True,
        "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "host_count": len(rows),
        "overall_gpu_percent": round(overall_gpu_percent, 2),
        "total_gpu_used_mb": round(total_gpu_used_mb, 2),
        "total_gpu_memory_mb": round(total_gpu_memory_mb, 2),
        "total_containers": total_containers,
        "hosts": rows,
    }


def safe_compute_visual_payload(db: Session) -> dict:
    try:
        return compute_visual_payload(db)
    except Exception:
        return {
            "ok": False,
            "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "host_count": 0,
            "overall_gpu_percent": 0.0,
            "total_gpu_used_mb": 0.0,
            "total_gpu_memory_mb": 0.0,
            "total_containers": 0,
            "hosts": [],
        }


def build_ssh_host(
    *,
    name: str,
    address: str,
    ssh_port: int,
    ssh_user: str,
    auth_type: str,
    ssh_password: str,
    ssh_key_path: str,
    port_start: int,
    port_end: int,
    workspace_root: str,
    shared_mnt_path: str,
    snapshot_root: str,
    default_user_share: int,
    snapshot_keep_count: int,
    snapshot_interval_days: int,
    notes: str,
) -> ManagedHost:
    resolved_auth_type = auth_type if auth_type in {AuthType.PASSWORD.value, AuthType.KEY.value} else AuthType.PASSWORD.value
    return ManagedHost(
        name=name.strip(),
        address=address.strip(),
        ssh_port=ssh_port,
        ssh_user=ssh_user.strip(),
        auth_type=resolved_auth_type,
        ssh_password=ssh_password or None,
        ssh_key_path=ssh_key_path or None,
        port_start=port_start,
        port_end=port_end,
        workspace_root=workspace_root,
        shared_mnt_path=shared_mnt_path,
        snapshot_root=snapshot_root,
        reserve_cpu_cores=0.0,
        reserve_memory_gb=0.0,
        reserve_disk_gb=0.0,
        default_user_share=default_user_share,
        snapshot_keep_count=snapshot_keep_count,
        snapshot_interval_days=snapshot_interval_days,
        notes=notes or None,
    )


def build_ssh_host_from_form(
    *,
    name: str,
    address: str,
    ssh_port: int,
    ssh_user: str,
    auth_type: str,
    ssh_password: str,
    ssh_key_path: str,
    port_start: int,
    port_end: int,
    workspace_root: str,
    shared_mnt_path: str,
    snapshot_root: str,
    default_user_share: int,
    snapshot_keep_count: int,
    snapshot_interval_days: int,
    notes: str,
) -> ManagedHost:
    return build_ssh_host(
        name=name,
        address=address,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        auth_type=auth_type,
        ssh_password=ssh_password,
        ssh_key_path=ssh_key_path,
        port_start=port_start,
        port_end=port_end,
        workspace_root=workspace_root,
        shared_mnt_path=shared_mnt_path,
        snapshot_root=snapshot_root,
        default_user_share=default_user_share,
        snapshot_keep_count=snapshot_keep_count,
        snapshot_interval_days=snapshot_interval_days,
        notes=notes,
    )


def validate_ssh_docker(host: ManagedHost) -> dict:
    docker = DockerService(host)
    docker_info = docker.docker_info()
    disk_info = docker.filesystem_usage_gb(host.workspace_root)
    used_ports = docker.used_host_ports()
    discovered_images = docker.discover_images()
    host.cached_images = json.dumps(discovered_images, ensure_ascii=False) if discovered_images else None
    available_ports = [
        port for port in range(host.port_start, host.port_end + 1) if port not in used_ports
    ]
    return {
        "docker_info": docker_info,
        "disk_info": disk_info,
        "used_ports": sorted(used_ports),
        "available_ports": available_ports,
        "images": discovered_images,
    }


def validate_ssh_docker_with_logs(host: ManagedHost, logs: list[str], *, timeout: int = 10) -> dict:
    docker = DockerService(host)
    logs.append(f"正在建立 SSH 连接：{host.ssh_user}@{host.address}:{host.ssh_port}")
    if not docker.ping(timeout=timeout):
        raise RunnerError("SSH 可连接性或 docker info 检测失败。", command="docker info >/dev/null 2>&1")
    logs.append("SSH 连接成功，Docker 服务可访问。")

    logs.append("正在读取 Docker 基础信息...")
    docker_info = docker.docker_info(timeout=timeout)
    total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)
    logs.append(
        "Docker 信息读取完成："
        f"{docker_info.get('NCPU') or '-'} 核，"
        f"{total_memory_gb:.1f} GB 内存，"
        f"Docker {docker_info.get('ServerVersion') or '-'}。"
    )

    logs.append(f"正在读取工作目录磁盘容量：{host.workspace_root}")
    disk_info = docker.filesystem_usage_gb(host.workspace_root, timeout=timeout)
    if disk_info:
        logs.append(
            "磁盘容量读取完成："
            f"{float(disk_info.get('used_gb') or 0):.1f} / "
            f"{float(disk_info.get('total_gb') or 0):.1f} GB。"
        )
    else:
        logs.append("未能读取工作目录磁盘容量，继续执行后续检测。")

    logs.append(f"正在扫描端口池占用：{host.port_start}-{host.port_end}")
    used_ports = docker.used_host_ports(timeout=timeout)
    available_ports = [
        port for port in range(host.port_start, host.port_end + 1) if port not in used_ports
    ]
    logs.append(f"端口扫描完成：已占用 {len(used_ports)} 个，可分配 {len(available_ports)} 个。")

    logs.append("正在检索本地可用基础镜像...")
    try:
        discovered_images = docker.discover_images(timeout=timeout)
    except RunnerError as exc:
        discovered_images = []
        logs.append(f"基础镜像检索失败，已跳过：{exc}")
    host.cached_images = json.dumps(discovered_images, ensure_ascii=False) if discovered_images else None
    if discovered_images:
        logs.append(f"发现 {len(discovered_images)} 个可选基础镜像。")
    else:
        logs.append("未发现符合 pytorch:x.x.x-cudax.x-cudnn 格式的基础镜像。")

    return {
        "docker_info": docker_info,
        "disk_info": disk_info,
        "used_ports": sorted(used_ports),
        "available_ports": available_ports,
        "images": discovered_images,
    }


def reclaim_existing_allocations_for_host(
    db: Session,
    host: ManagedHost,
    container_rows: list[dict],
    logs: list[str] | None = None,
) -> int:
    if not container_rows:
        return 0

    active_allocations = (
        db.query(Allocation)
        .filter(Allocation.status != AllocationStatus.DELETED.value)
        .all()
    )
    allocations_by_name = {
        allocation.container_name: allocation
        for allocation in active_allocations
        if allocation.container_name
    }
    allocations_by_port_image: dict[tuple[int, str], list[Allocation]] = {}
    for allocation in active_allocations:
        try:
            port = int(allocation.host_port)
        except (TypeError, ValueError):
            continue
        image_name = (allocation.image_name or "").strip()
        if image_name:
            allocations_by_port_image.setdefault((port, image_name), []).append(allocation)

    reclaimed = 0
    for row in container_rows:
        try:
            host_port = int(row.get("host_port"))
        except (TypeError, ValueError):
            continue
        container_name = str(row.get("container_name") or "").strip().lstrip("/")
        image_name = str(row.get("image_name") or "").strip()
        labels = (((row.get("detail") or {}).get("Config") or {}).get("Labels") or {})
        labeled_container_name = str(labels.get("control.container_name") or "").strip().lstrip("/")
        if not container_name:
            continue

        allocation = allocations_by_name.get(container_name)
        if allocation is None and labeled_container_name:
            allocation = allocations_by_name.get(labeled_container_name)
        if allocation is None and image_name:
            candidates = allocations_by_port_image.get((host_port, image_name), [])
            if len(candidates) == 1:
                allocation = candidates[0]
        if allocation is None or allocation.host_id == host.id:
            continue

        old_host_id = allocation.host_id
        allocation.host_id = host.id
        allocation.host_port = host_port
        allocation.container_name = container_name
        if image_name:
            allocation.image_name = image_name
        allocation.status = row.get("status") or allocation.status
        db.query(SnapshotRecord).filter(SnapshotRecord.allocation_id == allocation.id).update(
            {SnapshotRecord.host_id: host.id}
        )
        reclaimed += 1
        if logs is not None:
            logs.append(
                "自动认领旧平台分配："
                f"端口 {host_port} / 容器 {container_name} 从 host_id={old_host_id} 迁移到 host_id={host.id}。"
            )

    return reclaimed


def discover_and_reclaim_host_allocations(
    db: Session,
    host: ManagedHost,
    logs: list[str] | None = None,
    *,
    timeout: int = 30,
) -> int:
    try:
        rows = DockerService(host).managed_container_rows(host.port_start, host.port_end, timeout=timeout)
    except RunnerError as exc:
        if logs is not None:
            logs.append(f"自动认领扫描失败，已跳过：{exc}")
        return 0
    reclaimed = reclaim_existing_allocations_for_host(db, host, rows, logs=logs)
    for row in rows:
        try:
            upsert_cached_container_row(
                db,
                host.id,
                row["container_name"],
                int(row["host_port"]),
                row.get("image_name") or "",
                row.get("status") or AllocationStatus.STOPPED.value,
            )
        except Exception:
            continue
    if logs is not None:
        logs.append(f"宿主机已有容器扫描完成，自动认领 {reclaimed} 条旧平台分配。")
    return reclaimed


def archive_host_record(db: Session, host: ManagedHost, logs: list[str] | None = None) -> tuple[int, int]:
    active_allocations = (
        db.query(Allocation)
        .filter(
            Allocation.host_id == host.id,
            Allocation.status != AllocationStatus.DELETED.value,
        )
        .order_by(Allocation.host_port.asc())
        .all()
    )
    snapshot_count = db.query(SnapshotRecord).filter(SnapshotRecord.host_id == host.id).count()
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    original_name = host.name
    host.name = f"{ARCHIVED_HOST_NAME_PREFIX}{host.id}-{timestamp}-{original_name}"[:100]
    host.enabled = False
    archive_note = f"平台记录于 {timestamp} 归档移除；保留 {len(active_allocations)} 条合法分配用于重新接入自动认领。"
    host.notes = ((host.notes or "").rstrip() + "\n" if host.notes else "") + archive_note
    if logs is not None:
        logs.append(f"宿主机记录已归档隐藏：{original_name} -> {host.name}")
        logs.append(f"保留合法分配 {len(active_allocations)} 条，快照记录 {snapshot_count} 条。")
        if active_allocations:
            logs.append("后续如需恢复管理：重新接入同一 SSH 宿主机，平台会按容器名、标签或端口+镜像自动认领这些分配。")
    return len(active_allocations), snapshot_count


def parse_cached_images(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return filter_supported_base_images([str(item).strip() for item in value if str(item).strip()])


def resolve_root_disk_usage(host: ManagedHost, docker_info: dict) -> dict:
    usage = docker_info.get("_root_disk_usage") if isinstance(docker_info, dict) else {}
    if isinstance(usage, dict) and usage:
        return usage
    try:
        return DockerService(host).filesystem_usage_gb("/", timeout=8)
    except Exception:
        return {}


def resolve_workspace_disk_usage(docker_info: dict) -> dict:
    usage = docker_info.get("_workspace_disk_usage") if isinstance(docker_info, dict) else {}
    if isinstance(usage, dict) and usage:
        return usage
    return {}


def render_initial_setup(request: Request, message: str = "", error_log: str = ""):
    return render(
        request,
        "initial_setup.html",
        {
            "settings": settings,
            "auth_types": [AuthType.PASSWORD.value, AuthType.KEY.value],
            "message": message,
            "error_log": error_log,
        },
    )


@router.get("/login")
def login_page(request: Request):
    db: Session = SessionLocal()
    try:
        return render(
            request,
            "login.html",
            {
                "settings": settings,
                "message": "",
                "visual": safe_compute_visual_payload(db),
            },
        )
    finally:
        db.close()


@router.post("/login")
def login(request: Request, account: str = Form(...), password: str = Form(...)):
    is_ajax = wants_json(request)
    db: Session = SessionLocal()
    try:
        authenticated = False
        if account == settings.root_admin_account and password == settings.root_admin_password:
            authenticated = True
        else:
            user = db.query(AdminUser).filter(AdminUser.account == account).first()
            authenticated = bool(
                user
                and user.status == AdminStatus.APPROVED.value
                and verify_password(password, user.password_hash)
            )
        if not authenticated:
            if is_ajax:
                return JSONResponse(
                    {
                        "ok": False,
                        "message": "账号或密码错误，或账号尚未审批通过。",
                        "error_log": f"login failed account={account}",
                    },
                    status_code=200,
                )
            return RedirectResponse("/login?error=账号或密码错误，或账号尚未审批通过。", status_code=303)
        response = RedirectResponse("/", status_code=303)
        if is_ajax:
            response = JSONResponse({"ok": True, "message": "登录成功。", "redirect_url": "/"})
        response.set_cookie(
            SESSION_COOKIE,
            sign_session(account, client_ip(request)),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
        return response
    finally:
        db.close()


@router.get("/user-login")
def user_login_page(request: Request):
    db: Session = SessionLocal()
    try:
        return render(
            request,
            "user_login.html",
            {
                "settings": settings,
                "message": "",
                "visual": safe_compute_visual_payload(db),
            },
        )
    finally:
        db.close()


@router.post("/user-login")
def user_login(request: Request, account: str = Form(...), password: str = Form(...)):
    is_ajax = wants_json(request)
    db: Session = SessionLocal()
    try:
        platform_user = authenticate_platform_user(db, account, password)
        if not platform_user:
            if is_ajax:
                return JSONResponse(
                    {
                        "ok": False,
                        "message": "使用者账号或容器 SSH 密码错误，或该账号暂无有效分配。",
                        "error_log": f"user login failed account={account}",
                    },
                    status_code=200,
                )
            return RedirectResponse("/user-login?error=使用者账号或容器 SSH 密码错误，或该账号暂无有效分配。", status_code=303)
        response = RedirectResponse("/", status_code=303)
        if is_ajax:
            response = JSONResponse({"ok": True, "message": "使用者登录成功。", "redirect_url": "/"})
        response.set_cookie(
            USER_SESSION_COOKIE,
            sign_session(platform_user["account"], client_ip(request)),
            max_age=SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
        )
        response.delete_cookie(SESSION_COOKIE)
        return response
    finally:
        db.close()


@router.post("/logout")
def logout():
    response = RedirectResponse("/user-login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(USER_SESSION_COOKIE)
    return response


@router.get("/register")
def register_page(request: Request):
    return render(request, "register.html", {"settings": settings})


@router.post("/register")
def register(account: str = Form(...), password: str = Form(...), email: str = Form(...), student_id: str = Form(...)):
    db: Session = SessionLocal()
    try:
        existing = db.query(AdminUser).filter(AdminUser.account == account.strip()).first()
        if existing:
            return RedirectResponse("/register?error=该账号已存在或正在审批。", status_code=303)
        user = AdminUser(
            account=account.strip(),
            password_hash=password_hash(password),
            email=email.strip(),
            student_id=student_id.strip(),
            status=AdminStatus.PENDING.value,
        )
        db.add(user)
        db.commit()
        try:
            send_registration_email(user)
        except Exception:
            pass
        return RedirectResponse("/login?message=注册申请已提交，请等待 root 管理员审批。", status_code=303)
    except SQLAlchemyError as exc:
        db.rollback()
        return RedirectResponse(f"/register?error={str(exc).strip()}", status_code=303)
    finally:
        db.close()


@router.get("/admins")
def admins_page(request: Request):
    admin = getattr(request.state, "admin", None)
    if not admin or not admin.get("is_root"):
        return RedirectResponse("/", status_code=303)
    db: Session = SessionLocal()
    try:
        users = db.query(AdminUser).order_by(AdminUser.created_at.desc()).all()
        return render(request, "admins.html", {"settings": settings, "users": users})
    finally:
        db.close()


@router.post("/admins/{user_id}/approve")
def approve_admin(request: Request, user_id: int):
    admin = getattr(request.state, "admin", None)
    if not admin or not admin.get("is_root"):
        return RedirectResponse("/", status_code=303)
    db: Session = SessionLocal()
    try:
        user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
        if user:
            user.status = AdminStatus.APPROVED.value
            user.approved_by = settings.root_admin_account
            user.approved_at = datetime.utcnow()
            user.revoked_at = None
            db.commit()
        return RedirectResponse("/admins", status_code=303)
    finally:
        db.close()


@router.post("/admins/{user_id}/revoke")
def revoke_admin(request: Request, user_id: int):
    admin = getattr(request.state, "admin", None)
    if not admin or not admin.get("is_root"):
        return RedirectResponse("/", status_code=303)
    db: Session = SessionLocal()
    try:
        user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
        if user:
            user.status = AdminStatus.REVOKED.value
            user.revoked_at = datetime.utcnow()
            db.commit()
        return RedirectResponse("/admins", status_code=303)
    finally:
        db.close()


@router.get("/")
def dashboard(request: Request):
    db: Session = SessionLocal()
    try:
        platform_user = active_platform_user(request)
        if ssh_host_count(db) == 0 and not platform_user:
            return render_initial_setup(request)
        hosts = visible_hosts_query(db).order_by(ManagedHost.name.asc()).all()
        allocations_query = db.query(Allocation).order_by(Allocation.created_at.desc())
        if platform_user:
            allocations_query = allocations_query.filter(Allocation.assignee == platform_user["account"])
        allocations = allocations_query.limit(20).all()
        host_cards = []
        for host in hosts:
            cached = host_cache_payload(host)
            rows_data = build_host_rows_from_cached_data(host, list(host.allocations), cached)
            resource_summary = summarize_resource_reservations(rows_data)
            summary = {
                "reachable": cached["reachable"],
                "docker_info": cached["docker_info"],
                "stats": cached["stats"],
                "gpus": cached["gpus"],
                "active_allocations": sum(1 for row in rows_data["allocation_rows"] if row["status"] == AllocationStatus.RUNNING.value),
                "container_count": len(cached["container_rows"]),
                **resource_summary,
            }
            total_cpu = float(summary["docker_info"].get("NCPU") or 0)
            total_memory_gb = float(summary["docker_info"].get("MemTotal") or 0) / (1024**3)
            host_cards.append(
                {
                    "host": host,
                    "summary": summary,
                    "total_cpu": total_cpu,
                    "total_memory_gb": total_memory_gb,
                    "last_status_at": format_cache_time(cached["refreshed_at"]),
                }
            )
        return render(
            request,
            "dashboard.html",
            {
                "settings": settings,
                "host_cards": host_cards,
                "allocations": allocations,
                "visual": compute_visual_payload(db),
            },
        )
    finally:
        db.close()


@router.get("/compute-visual/data")
def compute_visual_data():
    db: Session = SessionLocal()
    try:
        return JSONResponse(compute_visual_payload(db))
    finally:
        db.close()


@router.get("/hosts")
def hosts_page(request: Request):
    admin_response = require_admin_response(request)
    if admin_response:
        return admin_response
    db: Session = SessionLocal()
    try:
        if ssh_host_count(db) == 0:
            return render_initial_setup(request)
        hosts = visible_hosts_query(db).order_by(ManagedHost.name.asc()).all()
        host_rows = []
        for host in hosts:
            cached = host_cache_payload(host)
            rows_data = build_host_rows_from_cached_data(host, list(host.allocations), cached)
            resource_summary = summarize_resource_reservations(rows_data)
            summary = {
                "reachable": cached["reachable"],
                "docker_info": cached["docker_info"],
                "stats": cached["stats"],
                "gpus": cached["gpus"],
                "active_allocations": sum(1 for row in rows_data["allocation_rows"] if row["status"] == AllocationStatus.RUNNING.value),
                "container_count": len(cached["container_rows"]),
                **resource_summary,
            }
            defaults = recommended_defaults(host, summary["docker_info"])
            host_rows.append({"host": host, "summary": summary, "defaults": defaults, "last_status_at": format_cache_time(cached["refreshed_at"])})
        return render(
            request,
            "hosts.html",
            {
                "settings": settings,
                "hosts": host_rows,
                "auth_types": [AuthType.PASSWORD.value, AuthType.KEY.value],
            },
        )
    finally:
        db.close()


@router.post("/hosts")
def create_host(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    ssh_port: int = Form(22),
    ssh_user: str = Form("root"),
    auth_type: str = Form(AuthType.PASSWORD.value),
    ssh_password: str = Form(""),
    ssh_key_path: str = Form(""),
    port_start: int = Form(50000),
    port_end: int = Form(50050),
    workspace_root: str = Form("/workspace/tenants"),
    shared_mnt_path: str = Form("/mnt"),
    snapshot_root: str = Form("/mnt/docker_platform_snapshots"),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
    admin_response = require_admin_response(request, "/hosts")
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    db: Session = SessionLocal()
    try:
        logs: list[str] = []
        logs.append(f"开始验证宿主机 {name} ({address}:{ssh_port})")
        host = build_ssh_host(
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            auth_type=auth_type,
            ssh_password=ssh_password,
            ssh_key_path=ssh_key_path,
            port_start=port_start,
            port_end=port_end,
            workspace_root=workspace_root,
            shared_mnt_path=shared_mnt_path,
            snapshot_root=snapshot_root,
            default_user_share=default_user_share,
            snapshot_keep_count=snapshot_keep_count,
            snapshot_interval_days=snapshot_interval_days,
            notes=notes,
        )
        logs.append("开始探测 SSH 与 Docker 环境")
        validate_ssh_docker(host)
        logs.append("探测通过，准备写入平台数据库")
        db.add(host)
        db.flush()
        logs.append("扫描宿主机已有容器，尝试自动认领旧平台分配")
        discover_and_reclaim_host_allocations(db, host, logs=logs, timeout=60)
        db.commit()
    except RunnerError as exc:
        db.rollback()
        summary, error_log = runner_error_payload(exc, "SSH 或 Docker 检测失败。")
        if is_ajax:
            logs.append("探测失败，已回滚数据库事务")
            return log_response(logs, ok=False, message=summary, error_log=error_log, status_code=200)
        return RedirectResponse(f"/hosts?error={summary}", status_code=303)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        if is_ajax:
            logs.append("数据库写入失败，已回滚事务")
            return log_response(logs, ok=False, message=reason, error_log=reason, status_code=200)
        return RedirectResponse(f"/hosts?error={reason}", status_code=303)
    finally:
        db.close()
    if is_ajax:
        logs.append("宿主机已成功纳入平台")
        return log_response(logs, ok=True, message=f"宿主机 {name} 已接入平台。")
    return RedirectResponse("/hosts", status_code=303)


@router.post("/hosts/test/stream")
def test_host_stream(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    ssh_port: int = Form(22),
    ssh_user: str = Form("root"),
    auth_type: str = Form(AuthType.PASSWORD.value),
    ssh_password: str = Form(""),
    ssh_key_path: str = Form(""),
    port_start: int = Form(50000),
    port_end: int = Form(50050),
    workspace_root: str = Form("/workspace/tenants"),
    shared_mnt_path: str = Form("/mnt"),
    snapshot_root: str = Form("/mnt/docker_platform_snapshots"),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
    admin_response = require_admin_response(request, "/hosts")
    if admin_response:
        return admin_response

    def generate():
        logs: list[str] = []
        emitted = 0

        def emit_logs(progress: int | None = None):
            nonlocal emitted
            while emitted < len(logs):
                emitted += 1
                yield stream_event("log", message=logs[emitted - 1], progress=progress)

        try:
            yield stream_event("log", message=f"开始检测宿主机 {name} ({address}:{ssh_port})", progress=5)
            host = build_ssh_host_from_form(
                name=name,
                address=address,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                auth_type=auth_type,
                ssh_password=ssh_password,
                ssh_key_path=ssh_key_path,
                port_start=port_start,
                port_end=port_end,
                workspace_root=workspace_root,
                shared_mnt_path=shared_mnt_path,
                snapshot_root=snapshot_root,
                default_user_share=default_user_share,
                snapshot_keep_count=snapshot_keep_count,
                snapshot_interval_days=snapshot_interval_days,
                notes=notes,
            )
            result = validate_ssh_docker_with_logs(host, logs, timeout=8)
            yield from emit_logs(progress=92)
            docker_info = result["docker_info"]
            total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)
            yield stream_event(
                "done",
                ok=True,
                message="SSH 与 Docker 检测通过。",
                progress=100,
                docker={
                    "cpus": docker_info.get("NCPU"),
                    "memory_gb": round(total_memory_gb, 1),
                    "containers": docker_info.get("Containers"),
                    "server_version": docker_info.get("ServerVersion"),
                },
                used_ports=result["used_ports"],
                available_ports=result["available_ports"],
            )
        except RunnerError as exc:
            yield from emit_logs(progress=100)
            summary, error_log = runner_error_payload(exc, "SSH 或 Docker 检测失败。")
            yield stream_event("error", ok=False, message=summary, error_log=error_log, progress=100)
        except Exception as exc:
            yield from emit_logs(progress=100)
            reason = str(exc).strip() or "宿主机检测失败。"
            yield stream_event("error", ok=False, message=reason, error_log=reason, progress=100)

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/hosts/create/stream")
def create_host_stream(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    ssh_port: int = Form(22),
    ssh_user: str = Form("root"),
    auth_type: str = Form(AuthType.PASSWORD.value),
    ssh_password: str = Form(""),
    ssh_key_path: str = Form(""),
    port_start: int = Form(50000),
    port_end: int = Form(50050),
    workspace_root: str = Form("/workspace/tenants"),
    shared_mnt_path: str = Form("/mnt"),
    snapshot_root: str = Form("/mnt/docker_platform_snapshots"),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
    admin_response = require_admin_response(request, "/hosts")
    if admin_response:
        return admin_response

    def generate():
        logs: list[str] = []
        emitted = 0
        db: Session | None = None

        def emit_logs(progress: int | None = None):
            nonlocal emitted
            while emitted < len(logs):
                emitted += 1
                yield stream_event("log", message=logs[emitted - 1], progress=progress)

        try:
            yield stream_event("log", message=f"开始保存宿主机 {name} ({address}:{ssh_port})", progress=3)
            host = build_ssh_host_from_form(
                name=name,
                address=address,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                auth_type=auth_type,
                ssh_password=ssh_password,
                ssh_key_path=ssh_key_path,
                port_start=port_start,
                port_end=port_end,
                workspace_root=workspace_root,
                shared_mnt_path=shared_mnt_path,
                snapshot_root=snapshot_root,
                default_user_share=default_user_share,
                snapshot_keep_count=snapshot_keep_count,
                snapshot_interval_days=snapshot_interval_days,
                notes=notes,
            )
            yield stream_event("log", message="保存前先执行 SSH 与 Docker 检测。", progress=8)
            validate_ssh_docker_with_logs(host, logs, timeout=8)
            yield from emit_logs(progress=78)

            db = SessionLocal()
            yield stream_event("log", message="检测通过，开始写入平台数据库。", progress=86)
            db.add(host)
            db.flush()
            yield stream_event("log", message="扫描宿主机已有容器，尝试自动认领旧平台分配。", progress=90)
            discover_and_reclaim_host_allocations(db, host, logs=logs, timeout=60)
            yield from emit_logs(progress=94)
            db.commit()
            yield stream_event("log", message="数据库写入完成，宿主机已纳入平台。", progress=96)
            yield stream_event(
                "done",
                ok=True,
                message=f"宿主机 {name} 已接入平台。",
                progress=100,
                redirect_url="/hosts",
            )
        except RunnerError as exc:
            if db is not None:
                db.rollback()
            yield from emit_logs(progress=100)
            summary, error_log = runner_error_payload(exc, "SSH 或 Docker 检测失败。")
            yield stream_event(
                "error",
                ok=False,
                message=summary,
                error_log=error_log,
                progress=100,
            )
        except SQLAlchemyError as exc:
            if db is not None:
                db.rollback()
            reason = str(exc).strip()
            yield stream_event(
                "error",
                ok=False,
                message=reason,
                error_log=f"{reason}\n数据库事务已回滚，未保留半写入宿主机记录。",
                progress=100,
            )
        except Exception as exc:
            if db is not None:
                db.rollback()
            reason = str(exc).strip() or "宿主机保存失败。"
            yield stream_event("error", ok=False, message=reason, error_log=reason, progress=100)
        finally:
            if db is not None:
                db.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/setup/test")
def test_initial_host(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    ssh_port: int = Form(22),
    ssh_user: str = Form("root"),
    auth_type: str = Form(AuthType.PASSWORD.value),
    ssh_password: str = Form(""),
    ssh_key_path: str = Form(""),
    port_start: int = Form(50000),
    port_end: int = Form(50050),
    workspace_root: str = Form("/workspace/tenants"),
    shared_mnt_path: str = Form("/mnt"),
    snapshot_root: str = Form("/mnt/docker_platform_snapshots"),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
    admin_response = require_admin_response(request)
    if admin_response:
        return admin_response
    try:
        host = build_ssh_host(
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            auth_type=auth_type,
            ssh_password=ssh_password,
            ssh_key_path=ssh_key_path,
            port_start=port_start,
            port_end=port_end,
            workspace_root=workspace_root,
            shared_mnt_path=shared_mnt_path,
            snapshot_root=snapshot_root,
            default_user_share=default_user_share,
            snapshot_keep_count=snapshot_keep_count,
            snapshot_interval_days=snapshot_interval_days,
            notes=notes,
        )
        result = validate_ssh_docker(host)
        docker_info = result["docker_info"]
        total_memory_gb = float(docker_info.get("MemTotal") or 0) / (1024**3)
        return JSONResponse(
            {
                "ok": True,
                "message": "SSH 与 Docker 检测通过。",
                "docker": {
                    "cpus": docker_info.get("NCPU"),
                    "memory_gb": round(total_memory_gb, 1),
                    "containers": docker_info.get("Containers"),
                    "server_version": docker_info.get("ServerVersion"),
                },
                "used_ports": result["used_ports"],
                "available_ports": result["available_ports"],
            }
        )
    except RunnerError as exc:
        summary, error_log = runner_error_payload(exc, "SSH 或 Docker 检测失败。")
        return JSONResponse({"ok": False, "message": summary, "error_log": error_log}, status_code=200)


@router.post("/setup")
def create_initial_host(
    request: Request,
    name: str = Form(...),
    address: str = Form(...),
    ssh_port: int = Form(22),
    ssh_user: str = Form("root"),
    auth_type: str = Form(AuthType.PASSWORD.value),
    ssh_password: str = Form(""),
    ssh_key_path: str = Form(""),
    port_start: int = Form(50000),
    port_end: int = Form(50050),
    workspace_root: str = Form("/workspace/tenants"),
    shared_mnt_path: str = Form("/mnt"),
    snapshot_root: str = Form("/mnt/docker_platform_snapshots"),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
    admin_response = require_admin_response(request)
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    db: Session = SessionLocal()
    try:
        logs: list[str] = []
        if ssh_host_count(db) > 0:
            if is_ajax:
                return log_response(["平台已完成初始化"], ok=True, message="平台已完成初始化。")
            return RedirectResponse("/hosts", status_code=303)
        logs.append("开始初始化第一台宿主机")
        host = build_ssh_host(
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            auth_type=auth_type,
            ssh_password=ssh_password,
            ssh_key_path=ssh_key_path,
            port_start=port_start,
            port_end=port_end,
            workspace_root=workspace_root,
            shared_mnt_path=shared_mnt_path,
            snapshot_root=snapshot_root,
            default_user_share=default_user_share,
            snapshot_keep_count=snapshot_keep_count,
            snapshot_interval_days=snapshot_interval_days,
            notes=notes,
        )
        logs.append("开始验证 SSH 与 Docker")
        validate_ssh_docker(host)
        logs.append("验证通过，准备写入数据库")
        db.add(host)
        db.commit()
        if is_ajax:
            return log_response(logs + ["平台初始化完成"], ok=True, message="平台初始化完成。")
        return RedirectResponse("/hosts", status_code=303)
    except RunnerError as exc:
        db.rollback()
        summary, error_log = runner_error_payload(exc, "SSH 或 Docker 检测失败。")
        if is_ajax:
            return log_response(logs + ["检测失败，已回滚数据库事务"], ok=False, message=summary, error_log=error_log, status_code=200)
        return render_initial_setup(request, message=summary, error_log=error_log)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        if is_ajax:
            return log_response(logs + ["数据库写入失败，已回滚事务"], ok=False, message=reason, error_log=reason, status_code=200)
        return render_initial_setup(request, message=reason, error_log=reason)
    finally:
        db.close()


@router.post("/hosts/{host_id}/snapshot-policy")
def update_host_snapshot_policy(
    request: Request,
    host_id: int,
    snapshot_keep_count: int = Form(...),
    snapshot_interval_days: int = Form(...),
):
    admin_response = require_admin_response(request, f"/hosts/{host_id}")
    if admin_response:
        return admin_response
    redirect_url = f"/hosts/{host_id}"

    db: Session = SessionLocal()
    try:
        host = visible_host_by_id(db, host_id)
        if not host:
            return operation_error(request, "未找到宿主机记录。", "/hosts", status_code=404)
        host.snapshot_keep_count = max(snapshot_keep_count, 0)
        host.snapshot_interval_days = max(snapshot_interval_days, 1)
        db.commit()
        return operation_success(request, "宿主机快照策略已更新。", redirect_url)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).replace("\n", " ").strip()
        return operation_error(request, reason, redirect_url)
    finally:
        db.close()


@router.post("/hosts/{host_id}/delete")
def delete_host(request: Request, host_id: int):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response
    redirect_url = "/hosts"

    db: Session = SessionLocal()
    try:
        host = visible_host_by_id(db, host_id)
        if not host:
            return operation_error(request, "未找到宿主机记录。", redirect_url, status_code=404)
        active_allocations = (
            db.query(Allocation)
            .filter(
                Allocation.host_id == host.id,
                Allocation.status != AllocationStatus.DELETED.value,
            )
            .count()
        )
        if active_allocations > 0:
            message = (
                "该宿主机仍存在未删除的容器分配记录，不能直接物理删除。"
                "如确认只是移除平台宿主机入口，可继续执行归档移除：平台会隐藏该宿主机记录，"
                "保留合法容器分配与快照记录，远端 Docker 容器不会被停止或删除；"
                "之后重新接入同一 SSH 宿主机时会自动认领这些合法镜像。"
            )
            if wants_json(request):
                return JSONResponse(
                    {
                        "ok": False,
                        "message": "该宿主机仍存在未删除的容器分配记录，不能直接物理删除。",
                        "error_log": message,
                        "next_action": {
                            "type": "host_delete_stream",
                            "url": f"/hosts/{host.id}/delete/stream",
                            "label": "继续归档移除",
                            "confirm": (
                                "确认继续归档移除该宿主机入口？平台会保留合法分配记录与远端容器，"
                                "但该宿主机会从总览和宿主机列表隐藏。"
                            ),
                        },
                    },
                    status_code=200,
                )
            return operation_error(request, "该宿主机仍存在未删除的容器分配记录，暂不能移除。", redirect_url)
        db.delete(host)
        db.commit()
        return operation_success(request, f"宿主机 {host.name} 已从平台记录中移除。", redirect_url)
    except SQLAlchemyError as exc:
        db.rollback()
        return operation_error(request, str(exc).strip(), redirect_url)
    finally:
        db.close()


@router.post("/hosts/{host_id}/delete/stream")
def delete_host_stream(request: Request, host_id: int):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response

    def generate():
        logs: list[str] = []
        emitted = 0
        db: Session | None = None

        def emit_logs(progress: int | None = None):
            nonlocal emitted
            while emitted < len(logs):
                emitted += 1
                yield stream_event("log", message=logs[emitted - 1], progress=progress)

        try:
            yield stream_event("log", message=f"开始移除宿主机记录 host_id={host_id}", progress=5)
            db = SessionLocal()
            host = visible_host_by_id(db, host_id)
            if not host:
                yield stream_event("error", ok=False, message="未找到宿主机记录。", error_log=f"host_id={host_id}", progress=100)
                return

            yield stream_event("log", message=f"读取宿主机：{host.name} ({host.address}:{host.ssh_port})", progress=15)
            active_allocations = (
                db.query(Allocation)
                .filter(
                    Allocation.host_id == host.id,
                    Allocation.status != AllocationStatus.DELETED.value,
                )
                .order_by(Allocation.host_port.asc())
                .all()
            )
            yield stream_event("log", message=f"发现合法分配记录 {len(active_allocations)} 条。", progress=30)
            if active_allocations:
                host_display_name = host.name
                yield stream_event("log", message="执行安全归档移除：仅隐藏宿主机入口，不停止、不删除远端 Docker 容器。", progress=45)
                allocation_count, snapshot_count = archive_host_record(db, host, logs=logs)
                yield from emit_logs(progress=65)
                db.commit()
                yield stream_event("log", message="数据库事务已提交，合法分配记录已保留。", progress=88)
                yield stream_event(
                    "done",
                    ok=True,
                    message=f"宿主机 {host_display_name} 已归档移除，保留 {allocation_count} 条合法分配和 {snapshot_count} 条快照记录。",
                    progress=100,
                    redirect_url="/hosts",
                )
                return

            yield stream_event("log", message="未发现合法分配记录，执行物理删除平台宿主机记录。", progress=55)
            host_name = host.name
            db.delete(host)
            db.commit()
            yield stream_event("log", message="数据库事务已提交。", progress=90)
            yield stream_event(
                "done",
                ok=True,
                message=f"宿主机 {host_name} 已从平台记录中移除。",
                progress=100,
                redirect_url="/hosts",
            )
        except SQLAlchemyError as exc:
            if db is not None:
                db.rollback()
            reason = str(exc).strip()
            yield stream_event(
                "error",
                ok=False,
                message="宿主机记录移除失败。",
                error_log=f"{reason}\n数据库事务已回滚，未保留半写入状态。",
                progress=100,
            )
        except Exception as exc:
            if db is not None:
                db.rollback()
            reason = f"{type(exc).__name__}: {exc}"
            yield stream_event("error", ok=False, message="宿主机记录移除失败。", error_log=reason, progress=100)
        finally:
            if db is not None:
                db.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.get("/hosts/{host_id}")
def host_detail(request: Request, host_id: int):
    db: Session = SessionLocal()
    try:
        platform_user = active_platform_user(request)
        host = visible_host_by_id(db, host_id)
        if not host:
            return RedirectResponse("/hosts?error=未找到宿主机记录。", status_code=303)
        cached = host_cache_payload(host)
        if (
            not cached["refresh_in_progress"]
            and (
                not cached["refreshed_at"]
                or (datetime.utcnow() - cached["refreshed_at"]).total_seconds() >= HOST_REFRESH_INTERVAL_SECONDS
            )
        ):
            schedule_host_refresh(host.id, full=not bool(cached["refreshed_at"]))
        host_images = parse_cached_images(host.cached_images)
        if not host_images and cached["container_rows"]:
            host_images = filter_supported_base_images(
                sorted(
                    {
                        (row.get("image_name") or "").strip()
                        for row in cached["container_rows"]
                        if (row.get("image_name") or "").strip()
                    }
                )
            )
            if host_images:
                host.cached_images = json.dumps(host_images, ensure_ascii=False)
                db.commit()
        db_allocations = (
            db.query(Allocation)
            .filter(Allocation.host_id == host.id)
            .order_by(Allocation.host_port.asc())
            .all()
        )
        snapshots = []
        if not platform_user:
            snapshots = (
                db.query(SnapshotRecord)
                .filter(SnapshotRecord.host_id == host.id)
                .order_by(SnapshotRecord.created_at.desc())
                .limit(20)
                .all()
            )
        rows_data = build_host_rows_from_cached_data(host, db_allocations, cached)
        allocation_rows = rows_data["allocation_rows"]
        unified_container_rows = rows_data["unified_container_rows"]
        resource_chart_rows = rows_data["resource_chart_rows"]
        resource_summary = summarize_resource_reservations(rows_data)
        docker_info = cached["docker_info"]
        root_disk_usage = resolve_root_disk_usage(host, docker_info)
        workspace_disk_usage = resolve_workspace_disk_usage(docker_info)
        owned_allocation_ids: set[int] = (
            user_allocation_ids(db, platform_user["account"]) if platform_user else set()
        )
        user_has_host_allocation = bool(
            platform_user
            and any(
                row["registered"] and row["allocation"].id in owned_allocation_ids
                for row in unified_container_rows
            )
        )
        owned_visible_container_count = sum(
            1
            for row in unified_container_rows
            if row["registered"] and row["allocation"].id in owned_allocation_ids
        )
        summary = {
            "reachable": cached["reachable"],
            "docker_info": docker_info,
            "stats": cached["stats"],
            "gpus": cached["gpus"],
            "active_allocations": sum(1 for row in allocation_rows if row["status"] == AllocationStatus.RUNNING.value),
            "container_count": len(cached["container_rows"]),
            "total_cpu_cores": float(docker_info.get("NCPU") or 0),
            "total_memory_gb": float(docker_info.get("MemTotal") or 0) / (1024**3),
            "root_disk_total_gb": float(root_disk_usage.get("total_gb") or 0),
            "root_disk_used_gb": float(root_disk_usage.get("used_gb") or 0),
            "workspace_disk_total_gb": float(workspace_disk_usage.get("total_gb") or root_disk_usage.get("total_gb") or 0),
            "workspace_disk_used_gb": float(workspace_disk_usage.get("used_gb") or root_disk_usage.get("used_gb") or 0),
            **resource_summary,
        }
        defaults = recommended_defaults(host, cached["docker_info"])
        port_state = host_port_pool_state(host, db_allocations, cached["container_rows"])
        used_ports = port_state["used_ports"]
        available_ports = port_state["available_ports"]
        running_allocations = [
            row for row in allocation_rows if row["allocation"].status == AllocationStatus.RUNNING.value
        ]
        stopped_allocations = [
            row for row in allocation_rows if row["allocation"].status == AllocationStatus.STOPPED.value
        ]
        return render(
            request,
            "host_detail.html",
            {
                "settings": settings,
                "host": host,
                "summary": summary,
                "defaults": defaults,
                "allocations": allocation_rows,
                "container_rows": unified_container_rows,
                "running_allocations": running_allocations,
                "stopped_allocations": stopped_allocations,
                "available_ports": available_ports,
                "used_ports": used_ports,
                "snapshots": snapshots,
                "base_image": settings.default_base_image,
                "host_images": host_images,
                "resource_chart_rows": resource_chart_rows,
                "visual": compute_visual_payload(db),
                "docker_error": "",
                "last_status_at": format_cache_time(cached["refreshed_at"]),
                "last_status_error": cached["error_log"] or "",
                "refresh_in_progress": cached["refresh_in_progress"],
                "refresh_started_at": format_cache_time(cached["refresh_started_at"]),
                "owned_allocation_ids": sorted(owned_allocation_ids),
                "owned_visible_container_count": owned_visible_container_count,
                "user_has_host_allocation": user_has_host_allocation,
            },
        )
    finally:
        db.close()


@router.get("/hosts/{host_id}/terminal")
def host_terminal(request: Request, host_id: int):
    admin_response = require_admin_response(request, f"/hosts/{host_id}")
    if admin_response:
        return admin_response
    admin = getattr(request.state, "admin", None) or {}
    db: Session = SessionLocal()
    try:
        host = visible_host_by_id(db, host_id)
        if not host:
            return RedirectResponse("/hosts?error=未找到宿主机记录。", status_code=303)
        cached = host_cache_payload(host)
        if not cached["refreshed_at"] or (datetime.utcnow() - cached["refreshed_at"]).total_seconds() > 300:
            schedule_host_refresh(host.id, full=cache_needs_full_refresh(host, cached, db))
        return render(
            request,
            "host_terminal.html",
            {
                "settings": settings,
                "host": host,
                "reachable": cached["reachable"],
                "last_status_at": format_cache_time(cached["refreshed_at"]),
                "last_status_error": cached["error_log"] or "",
                "terminal_token": sign_terminal_token(admin.get("account", ""), "host", host.id),
            },
        )
    finally:
        db.close()


@router.post("/hosts/{host_id}/terminal/run")
def host_terminal_run(
    request: Request,
    host_id: int,
    command: str = Form(...),
    timeout: int = Form(120),
):
    admin_response = require_admin_response(request, f"/hosts/{host_id}")
    if admin_response:
        return admin_response
    db: Session = SessionLocal()
    try:
        host = visible_host_by_id(db, host_id)
        if not host:
            return JSONResponse({"ok": False, "message": "未找到宿主机记录。", "error_log": f"host_id={host_id}"}, status_code=404)
        command_text = command.strip()
        if not command_text:
            return JSONResponse({"ok": False, "message": "请输入要执行的命令。", "error_log": ""}, status_code=200)
        try:
            runner = DockerService(host).runner
        except Exception as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "message": "SSH 连接不可用。",
                    "error_log": str(exc),
                },
                status_code=200,
            )
        try:
            result = runner.run(command_text, timeout=max(timeout, 10))
            payload = {
                "ok": result.ok,
                "message": "命令执行完成。" if result.ok else "命令执行失败。",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
            }
            if not result.ok:
                payload["error_log"] = result.stderr or result.stdout or f"exit_code={result.exit_code}"
            return JSONResponse(payload, status_code=200)
        except RunnerError as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "message": "SSH 命令执行失败。",
                    "error_log": exc.log_text() or str(exc),
                },
                status_code=200,
            )
    finally:
        db.close()


@router.websocket("/ws/hosts/{host_id}/terminal")
async def host_terminal_ws(websocket: WebSocket, host_id: int):
    db: Session = SessionLocal()
    client = None
    channel = None
    fallback_mode = False
    try:
        await websocket.accept()
        cookies = websocket_cookies(websocket)
        admin = websocket_admin(cookies, websocket, db)
        if not admin:
            admin = admin_from_account(db, terminal_token_account(websocket, "host", host_id))
        if not admin:
            await websocket.send_json(
                {
                    "event": "error",
                    "message": "当前账号无管理权限。",
                    "error_log": "未识别到有效管理员登录态。请从宿主机详情页重新点击 SSH 命令行；若仍失败，请确认当前浏览器使用的是管理员账号。",
                }
            )
            await websocket.close()
            return
        host = visible_host_by_id(db, host_id)
        if not host:
            await websocket.send_json({"event": "error", "message": "未找到宿主机记录。", "error_log": f"host_id={host_id}"})
            await websocket.close()
            return
        await websocket.send_json({"event": "progress", "message": "正在建立 SSH 连接...", "progress": 10})
        try:
            runner = get_runner(host)
        except RunnerError as exc:
            await websocket.send_json(
                {
                    "event": "error",
                    "message": "SSH 连接不可用。",
                    "error_log": exc.log_text() or str(exc),
                }
            )
            await websocket.close()
            return
        await websocket.send_json({"event": "progress", "message": "正在打开交互 shell...", "progress": 45})
        try:
            client, channel = await asyncio.to_thread(runner.open_shell, 30)
        except RunnerError as exc:
            fallback_mode = True
            await websocket.send_json(
                {
                    "event": "fallback",
                    "message": "SSH 交互 shell 不可用，已切换为 SSH 命令执行兼容模式。",
                    "error_log": exc.log_text() or str(exc),
                    "prompt": f"ssh@{host.name}$ ",
                }
            )
        else:
            await websocket.send_json({"event": "ready", "message": "SSH 命令行已连接。", "prompt": f"ssh@{host.name}$ "})

        async def forward_stdout():
            while True:
                if fallback_mode or channel is None:
                    break
                try:
                    if channel.recv_ready():
                        data = channel.recv(4096)
                        if data:
                            await websocket.send_json({"event": "output", "stream": "stdout", "data": data.decode(errors="ignore")})
                    if channel.recv_stderr_ready():
                        data = channel.recv_stderr(4096)
                        if data:
                            await websocket.send_json({"event": "output", "stream": "stderr", "data": data.decode(errors="ignore")})
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break
                except Exception as exc:
                    await websocket.send_json({"event": "error", "message": "终端流读取失败。", "error_log": str(exc)})
                    break
                await asyncio.sleep(0.05)

        forward_task = asyncio.create_task(forward_stdout())
        try:
            while True:
                message = await websocket.receive_text()
                if fallback_mode:
                    if message.strip().lower() in {"exit", "logout"}:
                        break
                    result = await asyncio.to_thread(runner.run, message, 30)
                    await websocket.send_json(
                        {
                            "event": "output",
                            "stream": "stdout",
                            "data": result.stdout,
                            "stderr": result.stderr,
                            "exit_code": result.exit_code,
                        }
                    )
                    if result.stderr:
                        await websocket.send_json(
                            {
                                "event": "output",
                                "stream": "stderr",
                                "data": result.stderr,
                                "exit_code": result.exit_code,
                            }
                        )
                    continue
                if channel is None:
                    continue
                if message.strip().lower() in {"exit", "logout"}:
                    channel.send("exit\n")
                    break
                channel.send(message + "\n")
        except WebSocketDisconnect:
            pass
        finally:
            if forward_task:
                forward_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await forward_task
            await websocket.close()
    finally:
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        db.close()


@router.get("/allocations/{allocation_id}/terminal")
def allocation_terminal(request: Request, allocation_id: int):
    db: Session = SessionLocal()
    try:
        allocation = _require_allocation(db, allocation_id)
        owner_response = allocation_owner_response(request, allocation)
        if owner_response:
            return owner_response
        terminal_account = ""
        admin = getattr(request.state, "admin", None)
        platform_user = getattr(request.state, "platform_user", None)
        if admin:
            terminal_account = admin.get("account", "")
        elif platform_user:
            terminal_account = platform_user.get("account", "")
        return render(
            request,
            "container_terminal.html",
            {
                "settings": settings,
                "allocation": allocation,
                "host": allocation.host,
                "terminal_token": sign_terminal_token(terminal_account, "allocation", allocation.id),
            },
        )
    except ValueError:
        return RedirectResponse("/?error=未找到容器分配记录。", status_code=303)
    finally:
        db.close()


@router.websocket("/ws/allocations/{allocation_id}/terminal")
async def allocation_terminal_ws(websocket: WebSocket, allocation_id: int):
    db: Session = SessionLocal()
    client = None
    channel = None
    fallback_mode = False
    runner = None
    allocation = None
    try:
        await websocket.accept()
        cookies = websocket_cookies(websocket)
        admin = websocket_admin(cookies, websocket, db)
        user_account = read_websocket_session(cookies, USER_SESSION_COOKIE, websocket)
        token_account = terminal_token_account(websocket, "allocation", allocation_id)
        if not admin:
            admin = admin_from_account(db, token_account)
        if not user_account and token_account and not admin:
            user_account = token_account

        allocation = db.query(Allocation).filter(Allocation.id == allocation_id).first()
        if not allocation or allocation.status == AllocationStatus.DELETED.value:
            await websocket.send_json({"event": "error", "message": "未找到有效容器分配记录。", "error_log": f"allocation_id={allocation_id}"})
            await websocket.close()
            return

        admin_allowed = bool(admin)
        user_allowed = bool(user_account and allocation.assignee == user_account)
        if not admin_allowed and not user_allowed:
            await websocket.send_json(
                {
                    "event": "error",
                    "message": "当前账号不能访问该容器终端。",
                    "error_log": (
                        "容器终端只允许管理员或该容器使用者访问。"
                        f" 当前识别账号：{user_account or (admin or {}).get('account') or '未识别'}；"
                        f" 容器使用者：{allocation.assignee}。请从宿主机详情页重新点击对应容器命令行。"
                    ),
                }
            )
            await websocket.close()
            return

        await websocket.send_json({"event": "progress", "message": "正在通过宿主机 SSH 建立容器终端...", "progress": 12})
        try:
            runner = get_runner(allocation.host)
        except RunnerError as exc:
            await websocket.send_json(
                {
                    "event": "error",
                    "message": "宿主机 SSH 连接不可用。",
                    "error_log": exc.log_text() or str(exc),
                }
            )
            await websocket.close()
            return

        await websocket.send_json({"event": "progress", "message": "正在检查容器状态...", "progress": 35})
        try:
            exists, runtime_status = await asyncio.to_thread(
                DockerService(allocation.host).inspect_container_runtime,
                allocation.container_name,
            )
        except RunnerError as exc:
            await websocket.send_json(
                {
                    "event": "error",
                    "message": "容器状态检查失败。",
                    "error_log": exc.log_text() or str(exc),
                }
            )
            await websocket.close()
            return
        if not exists:
            await websocket.send_json({"event": "error", "message": "容器不存在。", "error_log": allocation.container_name})
            await websocket.close()
            return
        if runtime_status != "running":
            await websocket.send_json(
                {
                    "event": "error",
                    "message": "容器未运行，无法打开命令行。",
                    "error_log": f"{allocation.container_name} status={runtime_status}",
                }
            )
            await websocket.close()
            return

        shell_command = f"docker exec -it {shlex.quote(allocation.container_name)} bash"
        fallback_prompt = f"container:{allocation.host_port}$ "
        await websocket.send_json({"event": "progress", "message": "正在进入容器 shell...", "progress": 62})
        try:
            client, channel = await asyncio.to_thread(runner.open_command_shell, shell_command, 30)
        except RunnerError as exc:
            fallback_mode = True
            await websocket.send_json(
                {
                    "event": "fallback",
                    "message": "容器交互 shell 不可用，已切换为容器命令兼容模式。",
                    "error_log": exc.log_text() or str(exc),
                    "prompt": fallback_prompt,
                }
            )
        else:
            await websocket.send_json({"event": "ready", "message": "容器命令行已连接。", "prompt": fallback_prompt})

        async def forward_stdout():
            while True:
                if fallback_mode or channel is None:
                    break
                try:
                    if channel.recv_ready():
                        data = channel.recv(4096)
                        if data:
                            await websocket.send_json({"event": "output", "stream": "stdout", "data": data.decode(errors="ignore")})
                    if channel.recv_stderr_ready():
                        data = channel.recv_stderr(4096)
                        if data:
                            await websocket.send_json({"event": "output", "stream": "stderr", "data": data.decode(errors="ignore")})
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break
                except Exception as exc:
                    await websocket.send_json({"event": "error", "message": "容器终端流读取失败。", "error_log": str(exc)})
                    break
                await asyncio.sleep(0.05)

        forward_task = asyncio.create_task(forward_stdout())
        try:
            while True:
                message = await websocket.receive_text()
                if message.strip().lower() in {"exit", "logout"}:
                    if channel is not None:
                        channel.send("exit\n")
                    break
                if fallback_mode:
                    command = f"docker exec {shlex.quote(allocation.container_name)} bash -lc {shlex.quote(message)}"
                    result = await asyncio.to_thread(runner.run, command, 60)
                    await websocket.send_json(
                        {
                            "event": "output",
                            "stream": "stdout",
                            "data": result.stdout,
                            "stderr": result.stderr,
                            "exit_code": result.exit_code,
                        }
                    )
                    if result.stderr:
                        await websocket.send_json(
                            {
                                "event": "output",
                                "stream": "stderr",
                                "data": result.stderr,
                                "exit_code": result.exit_code,
                            }
                        )
                    continue
                if channel is not None:
                    channel.send(message + "\n")
        except WebSocketDisconnect:
            pass
        finally:
            forward_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await forward_task
            await websocket.close()
    finally:
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        db.close()


@router.get("/hosts/{host_id}/metrics")
def host_metrics(host_id: int):
    db: Session = SessionLocal()
    try:
        host = visible_host_by_id(db, host_id)
        if not host:
            return JSONResponse(
                {"ok": False, "message": "未找到宿主机记录。", "error_log": f"host_id={host_id}"},
                status_code=404,
            )
        cached = host_cache_payload(host)
        if not cached["refresh_in_progress"]:
            if cache_needs_full_refresh(host, cached, db):
                schedule_host_refresh(host_id, full=True)
            elif (
                not cached["refreshed_at"]
                or (datetime.utcnow() - cached["refreshed_at"]).total_seconds() >= HOST_REFRESH_INTERVAL_SECONDS
            ):
                schedule_host_refresh(host_id, full=False)
        db_allocations = (
            db.query(Allocation)
            .filter(Allocation.host_id == host.id)
            .all()
        )
        rows_data = build_host_rows_from_cached_data(host, db_allocations, cached)
        resource_summary = summarize_resource_reservations(rows_data)
        allocated_payload = resource_summary["allocated_payload"]
        guaranteed_payload = resource_summary["guaranteed_payload"]
        reserved_payload = resource_summary["reserved_payload"]
        if cache_needs_full_refresh(host, cached, db) and not cached["container_rows"]:
            return JSONResponse(
                {
                    "ok": False,
                    "reachable": False,
                    "message": "监控状态正在初始化，请稍候。",
                    "error_log": "暂无缓存，已触发后台全量刷新。",
                    "updated_at": "正在更新",
                    "last_status_at": "正在更新",
                    "rows": rows_data["resource_chart_rows"],
                    "allocated": allocated_payload,
                    "guaranteed": guaranteed_payload,
                    "reserved": reserved_payload,
                    "gpus": cached["gpus"],
                    "refresh_in_progress": cached["refresh_in_progress"],
                },
                status_code=200,
            )
        if cached["refresh_in_progress"]:
            return JSONResponse(
                {
                    "ok": True,
                    "reachable": cached["reachable"],
                    "message": "正在后台刷新宿主机状态，当前展示稳定缓存。",
                    "updated_at": format_cache_time(cached["refreshed_at"]),
                    "last_status_at": format_cache_time(cached["refreshed_at"]),
                    "refresh_in_progress": True,
                    "refresh_started_at": format_cache_time(cached["refresh_started_at"]),
                    "rows": rows_data["resource_chart_rows"],
                    "allocated": allocated_payload,
                    "guaranteed": guaranteed_payload,
                    "reserved": reserved_payload,
                    "gpus": cached["gpus"],
                }
            )
        if not cached["reachable"]:
            return JSONResponse(
                {
                    "ok": False,
                    "reachable": False,
                    "message": "实时监控刷新失败，已保留最后缓存状态。",
                    "error_log": cached["error_log"] or "宿主机不可达",
                    "updated_at": format_cache_time(cached["refreshed_at"]),
                    "last_status_at": format_cache_time(cached["refreshed_at"]),
                    "rows": rows_data["resource_chart_rows"],
                    "allocated": allocated_payload,
                    "guaranteed": guaranteed_payload,
                    "reserved": reserved_payload,
                    "gpus": cached["gpus"],
                    "refresh_in_progress": cached["refresh_in_progress"],
                },
                status_code=200,
            )
        return JSONResponse(
            {
                "ok": True,
                "reachable": cached["reachable"],
                "updated_at": format_cache_time(cached["refreshed_at"]),
                "last_status_at": format_cache_time(cached["refreshed_at"]),
                "rows": rows_data["resource_chart_rows"],
                "allocated": allocated_payload,
                "guaranteed": guaranteed_payload,
                "reserved": reserved_payload,
                "gpus": cached["gpus"],
                "refresh_in_progress": cached["refresh_in_progress"],
            }
        )
    finally:
        db.close()


def _require_allocation(db: Session, allocation_id: int) -> Allocation:
    allocation = db.query(Allocation).filter(Allocation.id == allocation_id).first()
    if not allocation:
        raise ValueError(f"未找到分配记录 allocation_id={allocation_id}")
    return allocation


def _require_snapshot(db: Session, snapshot_id: int) -> SnapshotRecord:
    snapshot = db.query(SnapshotRecord).filter(SnapshotRecord.id == snapshot_id).first()
    if not snapshot:
        raise ValueError(f"未找到快照记录 snapshot_id={snapshot_id}")
    return snapshot


@router.post("/allocations")
def create_allocation(
    request: Request,
    host_id: int = Form(...),
    host_port: int = Form(...),
    assignee: str = Form(...),
    purpose: str = Form(...),
    root_password: str = Form(...),
    cpu_limit_cores: float = Form(...),
    memory_limit_gb: float = Form(...),
    workspace_limit_gb: float = Form(...),
    extra_mounts: str = Form(""),
    base_image_override: str = Form(""),
    notes: str = Form(""),
):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    redirect_url = f"/hosts/{host_id}"

    db: Session = SessionLocal()
    host = None
    container_name = ""
    container_created = False
    logs: list[str] = []
    try:
        host = visible_host_by_id(db, host_id)
        if not host:
            return operation_error(request, "未找到宿主机记录。", "/hosts", status_code=404)
        if host_port < host.port_start or host_port > host.port_end:
            return operation_error(request, f"端口 {host_port} 不在当前宿主机端口池 {host.port_start}-{host.port_end} 内。", redirect_url)
        docker = DockerService(host)
        container_name = f"{slugify(host.name)}-{host_port}"
        logs.append(f"准备创建容器 {container_name}")
        active_port_allocation = (
            db.query(Allocation)
            .filter(
                Allocation.host_id == host_id,
                Allocation.host_port == host_port,
                Allocation.status != AllocationStatus.DELETED.value,
            )
            .first()
        )
        if active_port_allocation:
            return operation_error(request, f"端口 {host_port} 已存在平台分配记录，请选择其他端口。", redirect_url)
        allocation = (
            db.query(Allocation)
            .filter(
                Allocation.host_id == host_id,
                Allocation.container_name == container_name,
            )
            .first()
        )
        if allocation and allocation.status != AllocationStatus.DELETED.value:
            return operation_error(request, f"端口 {host_port} 已存在历史分配记录且未删除完成，请先检查该容器状态。", redirect_url)

        logs.append("检查 Docker 端口占用")
        reachable = docker.ping()
        used_ports = docker.used_host_ports() if reachable else set()
        if host_port in used_ports:
            try:
                logs.append("发现端口被残留容器占用，尝试清理")
                docker.ensure_container_absent(container_name)
            except RunnerError:
                pass
            used_ports = docker.used_host_ports() if reachable else set()
            if host_port in used_ports:
                return operation_error(request, f"端口 {host_port} 已被 Docker 实际占用，请选择其他端口。", redirect_url)
        logs.append("检查弹性资源准入")
        decision = check_allocation(db, host, cpu_limit_cores, memory_limit_gb, workspace_limit_gb)
        logs.extend(decision.warnings or [])
        if not decision.allowed:
            reason = "；".join(decision.reasons)
            if is_ajax:
                return log_response(
                    logs + ["弹性资源准入失败"],
                    ok=False,
                    message=split_error_payload(reason)[0],
                    error_log=reason,
                    status_code=200,
                )
            return operation_error(request, reason, redirect_url)

        selected_image = (base_image_override or "").strip()
        if not selected_image:
            return operation_error(request, "请选择或填写该宿主机本地已有基础镜像。", redirect_url, status_code=400)
        if not is_supported_base_image(selected_image):
            return operation_error(
                request,
                "基础镜像格式不符合平台要求，请选择 pytorch:x.x.x-cudax.x-cudnn... 形式的镜像。",
                redirect_url,
                status_code=400,
            )

        if allocation is None:
            allocation = Allocation(
                host_id=host_id,
                host_port=host_port,
                container_name=container_name,
            )
            allocation.host = host
            db.add(allocation)

        allocation.image_name = selected_image
        allocation.assignee = assignee
        allocation.purpose = purpose
        allocation.root_password = root_password
        allocation.cpu_limit_cores = cpu_limit_cores
        allocation.memory_limit_gb = memory_limit_gb
        allocation.workspace_limit_gb = workspace_limit_gb
        allocation.status = AllocationStatus.PENDING.value
        allocation.shared_mnt_enabled = True
        allocation.all_gpus_visible = True
        allocation.x11_enabled = True
        allocation.extra_mounts = extra_mounts or None
        allocation.notes = notes or None
        allocation.pending_rebuild = False
        allocation.pending_rebuild_reason = None
        db.flush()
        logs.append("开始创建并验证容器")
        DockerService(host).create_container(allocation, logs=logs)
        container_created = True
        upsert_cached_container_row(
            db,
            host.id,
            allocation.container_name,
            allocation.host_port,
            allocation.image_name,
            AllocationStatus.RUNNING.value,
        )
        schedule_host_refresh(host.id, full=False)
        db.commit()
        if is_ajax:
            return log_response(logs + ["容器创建成功"], ok=True, message=f"容器创建成功，端口 {host_port} 已分配给 {assignee}。")
        return operation_success(request, f"容器创建成功，端口 {host_port} 已分配给 {assignee}。", redirect_url)
    except RunnerError as exc:
        if host and container_name:
            try:
                logs.append("创建失败，执行残留容器清理")
                DockerService(host).ensure_container_absent(container_name)
            except RunnerError:
                pass
        db.rollback()
        summary, error_log = runner_error_payload(exc, "容器创建失败。")
        if is_ajax:
            return log_response(logs + ["容器创建失败，已回滚数据库事务"], ok=False, message=summary, error_log=error_log, status_code=200)
        return operation_error(request, error_log, redirect_url)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        if host and container_name and container_created:
            try:
                logs.append("数据库写入失败，清理已创建容器以保持平台状态一致。")
                DockerService(host).cleanup_failed_container(allocation, logs=logs)
            except Exception as cleanup_exc:
                logs.append(f"已回滚数据库事务，但清理已创建容器失败：{cleanup_exc}")
        if is_ajax:
            return log_response(
                logs + ["数据库写入失败，已回滚事务"],
                ok=False,
                message="数据库写入失败。",
                error_log=localize_error_log(reason, "数据库写入失败。"),
                status_code=200,
            )
        return operation_error(request, reason, redirect_url)
    except Exception as exc:
        db.rollback()
        raw_error = str(exc).strip()
        reason = chinese_error_summary(raw_error, "容器创建失败。")
        if host and container_name and container_created:
            try:
                logs.append("创建流程后段失败，清理已创建容器以保持平台状态一致。")
                DockerService(host).cleanup_failed_container(allocation, logs=logs)
            except Exception as cleanup_exc:
                logs.append(f"已回滚数据库事务，但清理已创建容器失败：{cleanup_exc}")
        if is_ajax:
            return log_response(
                logs + ["容器创建失败，已回滚数据库事务"],
                ok=False,
                message=reason,
                error_log=localize_error_log(raw_error, reason),
                status_code=200,
            )
        return operation_error(request, raw_error or reason, redirect_url)
    finally:
        db.close()


@router.post("/allocations/{allocation_id}/resources")
def update_allocation_resources(
    request: Request,
    allocation_id: int,
    cpu_limit_cores: float = Form(...),
    memory_limit_gb: float = Form(...),
    workspace_limit_gb: float = Form(...),
):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    redirect_url = "/"

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
        redirect_url = f"/hosts/{allocation.host_id}"
        original_state = {
            "cpu_limit_cores": allocation.cpu_limit_cores,
            "memory_limit_gb": allocation.memory_limit_gb,
            "workspace_limit_gb": allocation.workspace_limit_gb,
        }
        decision = check_allocation(
            db,
            allocation.host,
            cpu_limit_cores,
            memory_limit_gb,
            workspace_limit_gb,
            exclude_allocation_id=allocation.id,
        )
        logs.extend(decision.warnings or [])
        if not decision.allowed:
            reason = "；".join(decision.reasons)
            if is_ajax:
                return log_response(
                    logs + ["弹性资源准入失败"],
                    ok=False,
                    message=split_error_payload(reason)[0],
                    error_log=reason,
                    status_code=200,
                )
            return operation_error(request, reason, redirect_url)

        allocation.cpu_limit_cores = cpu_limit_cores
        allocation.memory_limit_gb = memory_limit_gb
        allocation.workspace_limit_gb = workspace_limit_gb
        logs.append(f"更新端口 {allocation.host_port} 弹性资源上限")
        docker_updated = False
        try:
            DockerService(allocation.host).update_resources(allocation, logs=logs)
            docker_updated = True
            db.commit()
            if is_ajax:
                return log_response(logs + ["资源更新成功"], ok=True, message=f"端口 {allocation.host_port} 弹性资源上限已更新。")
            return operation_success(request, f"端口 {allocation.host_port} 弹性资源上限已更新。", redirect_url)
        except Exception:
            allocation.cpu_limit_cores = original_state["cpu_limit_cores"]
            allocation.memory_limit_gb = original_state["memory_limit_gb"]
            allocation.workspace_limit_gb = original_state["workspace_limit_gb"]
            db.rollback()
            if docker_updated:
                try:
                    logs.append("数据库提交失败，尝试恢复容器原弹性资源上限，避免 Docker 与平台记录不一致。")
                    DockerService(allocation.host).update_resources(allocation, logs=logs)
                except Exception as rollback_exc:
                    logs.append(f"容器原资源上限恢复失败，请人工核对该容器：{rollback_exc}")
            raise
    except RunnerError as exc:
        db.rollback()
        summary, error_log = runner_error_payload(exc, "资源更新失败。")
        if is_ajax:
            return log_response(logs + ["资源更新失败，数据库事务已回滚"], ok=False, message=summary, error_log=error_log, status_code=200)
        return operation_error(request, error_log, redirect_url)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        if is_ajax:
            return log_response(
                logs + ["数据库写入失败，已回滚事务"],
                ok=False,
                message="数据库写入失败。",
                error_log=reason,
                status_code=200,
            )
        return operation_error(request, reason, redirect_url)
    except ValueError as exc:
        db.rollback()
        return operation_error(request, str(exc), redirect_url, status_code=404)
    finally:
        db.close()


@router.post("/allocations/{allocation_id}/mounts")
def update_allocation_mounts(
    request: Request,
    allocation_id: int,
    extra_mounts: str = Form(""),
):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    redirect_url = "/"

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
        redirect_url = f"/hosts/{allocation.host_id}"
        original_state = {
            "extra_mounts": allocation.extra_mounts,
            "pending_rebuild": allocation.pending_rebuild,
            "pending_rebuild_reason": allocation.pending_rebuild_reason,
        }
        allocation.extra_mounts = extra_mounts or None
        allocation.pending_rebuild = True
        allocation.pending_rebuild_reason = "挂载配置变更需要重建容器后生效。"
        logs.append(f"记录端口 {allocation.host_port} 挂载变更")
        try:
            db.commit()
            if is_ajax:
                return log_response(logs + ["挂载变更记录成功"], ok=True, message=f"端口 {allocation.host_port} 挂载变更已记录，待下次启动时自动重建生效。")
            return operation_success(request, f"端口 {allocation.host_port} 挂载变更已记录，待下次启动时自动重建生效。", redirect_url)
        except Exception:
            allocation.extra_mounts = original_state["extra_mounts"]
            allocation.pending_rebuild = original_state["pending_rebuild"]
            allocation.pending_rebuild_reason = original_state["pending_rebuild_reason"]
            db.rollback()
            raise
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return operation_error(request, reason, redirect_url)
    except ValueError as exc:
        db.rollback()
        return operation_error(request, str(exc), redirect_url, status_code=404)
    finally:
        db.close()


@router.post("/allocations/{allocation_id}/action")
def allocation_action(
    request: Request,
    allocation_id: int,
    action: str = Form(...),
):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    redirect_url = "/"

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
        redirect_url = f"/hosts/{allocation.host_id}"
        docker = DockerService(allocation.host)
        original_status = allocation.status
        success_message = "操作执行完成。"
        if action == "stop":
            logs.append(f"尝试停止端口 {allocation.host_port}")
            docker.stop_container(allocation)
            if update_cached_container_status(
                db,
                allocation.host_id,
                allocation.container_name,
                allocation.status,
                allocation.host_port,
                allocation.image_name,
            ):
                logs.append("已同步平台缓存状态为 stopped")
            schedule_host_refresh(allocation.host_id, full=False)
            success_message = f"端口 {allocation.host_port} 已停止。"
        elif action == "start":
            if allocation.pending_rebuild:
                logs.append("检测到需重建状态，准备重建后启动")
                docker.rebuild_container(allocation, logs=logs)
                upsert_cached_container_row(
                    db,
                    allocation.host_id,
                    allocation.container_name,
                    allocation.host_port,
                    allocation.image_name,
                    allocation.status,
                )
                success_message = f"端口 {allocation.host_port} 已按最新挂载配置重建并启动。"
            else:
                logs.append(f"尝试启动端口 {allocation.host_port}")
                docker.start_container(allocation)
                if update_cached_container_status(
                    db,
                    allocation.host_id,
                    allocation.container_name,
                    allocation.status,
                    allocation.host_port,
                    allocation.image_name,
                ):
                    logs.append("已同步平台缓存状态为 running")
                success_message = f"端口 {allocation.host_port} 已启动。"
            schedule_host_refresh(allocation.host_id, full=False)
        elif action == "restart":
            if allocation.pending_rebuild:
                logs.append("检测到需重建状态，准备重建后启动")
                docker.rebuild_container(allocation, logs=logs)
                upsert_cached_container_row(
                    db,
                    allocation.host_id,
                    allocation.container_name,
                    allocation.host_port,
                    allocation.image_name,
                    allocation.status,
                )
                success_message = f"端口 {allocation.host_port} 已按最新配置重建并启动。"
            else:
                logs.append(f"尝试重启端口 {allocation.host_port}")
                docker.restart_container(allocation, logs=logs)
                if update_cached_container_status(
                    db,
                    allocation.host_id,
                    allocation.container_name,
                    allocation.status,
                    allocation.host_port,
                    allocation.image_name,
                ):
                    logs.append("已同步平台缓存状态为 running")
                success_message = f"端口 {allocation.host_port} 已重启。"
            schedule_host_refresh(allocation.host_id, full=False)
        elif action == "delete":
            logs.append(f"尝试删除端口 {allocation.host_port} 容器")
            deleted_container_name = allocation.container_name
            docker.remove_container(allocation)
            if remove_cached_container_row(db, allocation.host_id, deleted_container_name):
                logs.append("已从平台缓存中释放该端口")
            schedule_host_refresh(allocation.host_id, full=False)
            success_message = f"端口 {allocation.host_port} 对应容器已删除。"
        else:
            return operation_error(request, "不支持的操作类型。", redirect_url, status_code=400)
        try:
            db.commit()
            if is_ajax:
                return log_response(logs + ["维护操作成功"], ok=True, message=success_message)
            return operation_success(request, success_message, redirect_url)
        except Exception:
            allocation.status = original_status
            db.rollback()
            raise
    except RunnerError as exc:
        db.rollback()
        reconcile_db: Session = SessionLocal()
        try:
            allocation = _require_allocation(reconcile_db, allocation_id)
            DockerService(allocation.host).reconcile_allocation_state(allocation)
            reconcile_db.commit()
        except Exception:
            reconcile_db.rollback()
        finally:
            reconcile_db.close()
        action_message = {
            "stop": "停止操作失败。",
            "start": "启动操作失败。",
            "restart": "重启操作失败。",
            "delete": "删除操作失败。",
        }.get(action, "维护操作失败。")
        summary, error_log = runner_error_payload(exc, action_message)
        if is_ajax:
            return JSONResponse({"ok": False, "message": summary, "error_log": error_log}, status_code=200)
        return operation_error(request, error_log, redirect_url)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return operation_error(request, reason, redirect_url)
    except ValueError as exc:
        db.rollback()
        return operation_error(request, str(exc), redirect_url, status_code=404)
    finally:
        db.close()


@router.post("/hosts/{host_id}/containers/action")
def external_container_action(
    request: Request,
    host_id: int,
    container_name: str = Form(...),
    action: str = Form(...),
):
    admin_response = require_admin_response(request, f"/hosts/{host_id}")
    if admin_response:
        return admin_response
    db: Session = SessionLocal()
    try:
        host = visible_host_by_id(db, host_id)
        if not host:
            response = ajax_response(request, False, "未找到宿主机记录。", status_code=404)
            if response:
                return response
            return RedirectResponse("/hosts?error=未找到宿主机记录。", status_code=303)

        allocation = (
            db.query(Allocation)
            .filter(
                Allocation.host_id == host_id,
                Allocation.container_name == container_name,
                Allocation.status != AllocationStatus.DELETED.value,
            )
            .first()
        )
        if allocation:
            response = ajax_response(request, False, "该容器已纳入平台登记，请使用登记容器维护入口。")
            if response:
                return response
            return RedirectResponse(f"/hosts/{host_id}?error=该容器已纳入平台登记，请使用登记容器维护入口。", status_code=303)

        docker = DockerService(host)
        if action == "stop":
            runtime_status = docker.stop_container_by_name(container_name)
            update_cached_container_status(db, host.id, container_name, runtime_status)
            schedule_host_refresh(host.id, full=False)
            db.commit()
            message = f"容器 {container_name} 已停止。"
        elif action == "start":
            runtime_status = docker.start_container_by_name(container_name)
            update_cached_container_status(db, host.id, container_name, runtime_status)
            schedule_host_refresh(host.id, full=False)
            db.commit()
            message = f"容器 {container_name} 已启动。"
        elif action == "restart":
            runtime_status = docker.restart_container_by_name(container_name)
            update_cached_container_status(db, host.id, container_name, runtime_status)
            schedule_host_refresh(host.id, full=False)
            db.commit()
            message = f"容器 {container_name} 已重启。"
        elif action == "delete":
            docker.remove_container_by_name(container_name)
            cache_changed = remove_cached_container_row(db, host.id, container_name)
            if cache_changed:
                db.commit()
            schedule_host_refresh(host.id, full=False)
            message = f"容器 {container_name} 已删除。"
        else:
            response = ajax_response(request, False, "不支持的操作类型。", status_code=400)
            if response:
                return response
            return RedirectResponse(f"/hosts/{host_id}?error=不支持的操作类型。", status_code=303)

        response = ajax_response(request, True, message)
        if response:
            return response
        return RedirectResponse(f"/hosts/{host_id}", status_code=303)
    except RunnerError as exc:
        summary, error_log = runner_error_payload(exc, "外部容器维护失败。")
        response = ajax_response(request, False, summary, error_log=error_log)
        if response:
            return response
        return RedirectResponse(f"/hosts/{host_id}?error={summary}", status_code=303)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        response = ajax_response(request, False, reason, error_log=reason)
        if response:
            return response
        return RedirectResponse(f"/hosts/{host_id}?error={reason}", status_code=303)
    finally:
        db.close()


@router.post("/hosts/{host_id}/containers/restart-all/stream")
def restart_all_host_containers_stream(request: Request, host_id: int):
    admin_response = require_admin_response(request, f"/hosts/{host_id}")
    if admin_response:
        return admin_response

    def generate():
        db: Session = SessionLocal()
        try:
            host = visible_host_by_id(db, host_id)
            if not host:
                yield stream_event("error", ok=False, message="未找到宿主机记录。", error_log=f"host_id={host_id}")
                return

            docker = DockerService(host)
            yield stream_event("log", message=f"开始读取宿主机 {host.name} 的端口容器")
            container_rows = docker.managed_container_rows(host.port_start, host.port_end, timeout=120)
            restart_rows = [
                row for row in container_rows
                if row.get("status") != AllocationStatus.DELETED.value
            ]
            total = len(restart_rows)
            if total == 0:
                yield stream_event("done", ok=True, message="当前宿主机没有需要重启的端口容器。", completed=0, total=0, progress=100)
                return

            yield stream_event("progress", message=f"发现 {total} 个容器，准备逐个重启。", completed=0, total=total, progress=0)
            allocation_by_name = {
                allocation.container_name: allocation
                for allocation in db.query(Allocation)
                .filter(
                    Allocation.host_id == host.id,
                    Allocation.status != AllocationStatus.DELETED.value,
                )
                .all()
            }

            failures: list[str] = []
            for index, row in enumerate(restart_rows, start=1):
                container_name = row["container_name"]
                host_port = row["host_port"]
                yield stream_event("log", message=f"[{index}/{total}] 重启端口 {host_port} 容器 {container_name}")
                try:
                    runtime_status = docker.restart_container_by_name(container_name)
                    allocation = allocation_by_name.get(container_name)
                    if allocation:
                        allocation.status = runtime_status
                        db.commit()
                    progress = int(index * 100 / total)
                    yield stream_event(
                        "progress",
                        message=f"端口 {host_port} 已重启。",
                        completed=index,
                        total=total,
                        progress=progress,
                    )
                except RunnerError as exc:
                    db.rollback()
                    error_log = exc.log_text() or str(exc)
                    failures.append(f"端口 {host_port} ({container_name})：{error_log}")
                    yield stream_event(
                        "log",
                        message=f"端口 {host_port} 重启失败，继续尝试后续容器。",
                    )

            if failures:
                error_log = "\n\n".join(failures)
                yield stream_event(
                    "error",
                    ok=False,
                    message=f"批量重启完成，但 {len(failures)} 个容器失败。",
                    error_log=error_log,
                    completed=total - len(failures),
                    total=total,
                    progress=100,
                )
                return

            yield stream_event(
                "done",
                ok=True,
                message=f"已重启 {total} 个端口容器。",
                completed=total,
                total=total,
                progress=100,
            )
        except RunnerError as exc:
            db.rollback()
            summary, error_log = runner_error_payload(exc, "批量重启失败。")
            yield stream_event("error", ok=False, message=summary, error_log=error_log)
        except SQLAlchemyError as exc:
            db.rollback()
            reason = str(exc).strip()
            yield stream_event("error", ok=False, message=reason, error_log=reason)
        except Exception as exc:
            db.rollback()
            reason = f"{type(exc).__name__}: {exc}"
            yield stream_event("error", ok=False, message="批量重启失败。", error_log=reason)
        finally:
            db.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/allocations/{allocation_id}/snapshot")
def create_snapshot(
    request: Request,
    allocation_id: int,
    snapshot_keep_count: int = Form(...),
    snapshot_interval_days: int = Form(...),
):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    redirect_url = "/"

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
        redirect_url = f"/hosts/{allocation.host_id}"
        original_state = {
            "snapshot_policy_override": allocation.snapshot_policy_override,
            "snapshot_keep_count": allocation.snapshot_keep_count,
            "snapshot_interval_days": allocation.snapshot_interval_days,
        }
        allocation.snapshot_policy_override = True
        allocation.snapshot_keep_count = max(snapshot_keep_count, 0)
        allocation.snapshot_interval_days = max(snapshot_interval_days, 1)
        logs.append(f"创建端口 {allocation.host_port} 快照")
        try:
            db.flush()
            create_snapshot_record(db, allocation, logs=logs)
            db.commit()
            if is_ajax:
                return log_response(logs + ["环境快照创建成功"], ok=True,
                    message=(
                        f"环境快照创建完成。该端口已改为独立快照策略：保留 {allocation.snapshot_keep_count} 份，周期 {allocation.snapshot_interval_days} 天。"
                    )
                )
            return operation_success(
                request,
                f"环境快照创建完成。该端口已改为独立快照策略：保留 {allocation.snapshot_keep_count} 份，周期 {allocation.snapshot_interval_days} 天。",
                redirect_url,
            )
        except Exception:
            allocation.snapshot_policy_override = original_state["snapshot_policy_override"]
            allocation.snapshot_keep_count = original_state["snapshot_keep_count"]
            allocation.snapshot_interval_days = original_state["snapshot_interval_days"]
            db.rollback()
            raise
    except RunnerError as exc:
        db.rollback()
        summary, error_log = runner_error_payload(exc, "环境快照创建失败。")
        if is_ajax:
            return JSONResponse({"ok": False, "message": summary, "error_log": error_log}, status_code=200)
        return operation_error(request, error_log, redirect_url)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return operation_error(request, reason, redirect_url)
    except ValueError as exc:
        db.rollback()
        return operation_error(request, str(exc), redirect_url, status_code=404)
    finally:
        db.close()


@router.post("/allocations/{allocation_id}/snapshot/stream")
def create_snapshot_stream(
    request: Request,
    allocation_id: int,
    snapshot_keep_count: int = Form(...),
    snapshot_interval_days: int = Form(...),
):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response

    def generate():
        db: Session = SessionLocal()
        logs: list[str] = []
        emitted = 0

        def emit_new_logs():
            nonlocal emitted
            for line in logs[emitted:]:
                yield stream_event("log", message=line)
            emitted = len(logs)

        try:
            yield stream_event("log", message="快照请求已进入后端队列。")
            allocation = _require_allocation(db, allocation_id)
            original_state = {
                "snapshot_policy_override": allocation.snapshot_policy_override,
                "snapshot_keep_count": allocation.snapshot_keep_count,
                "snapshot_interval_days": allocation.snapshot_interval_days,
            }
            allocation.snapshot_policy_override = True
            allocation.snapshot_keep_count = max(snapshot_keep_count, 0)
            allocation.snapshot_interval_days = max(snapshot_interval_days, 1)
            logs.append(f"准备创建端口 {allocation.host_port} 的环境快照。")
            yield from emit_new_logs()
            try:
                db.flush()
                logs.append("数据库策略变更已暂存，尚未最终提交。")
                yield from emit_new_logs()
                create_snapshot_record(db, allocation, logs=logs)
                yield from emit_new_logs()
                db.commit()
                logs.append("数据库事务已提交。")
                yield from emit_new_logs()
                yield stream_event(
                    "done",
                    ok=True,
                    message=(
                        f"环境快照创建完成。该端口已改为独立快照策略：保留 {allocation.snapshot_keep_count} 份，周期 {allocation.snapshot_interval_days} 天。"
                    ),
                )
            except Exception:
                allocation.snapshot_policy_override = original_state["snapshot_policy_override"]
                allocation.snapshot_keep_count = original_state["snapshot_keep_count"]
                allocation.snapshot_interval_days = original_state["snapshot_interval_days"]
                db.rollback()
                logs.append("快照失败，数据库事务已回滚。")
                yield from emit_new_logs()
                raise
        except RunnerError as exc:
            db.rollback()
            summary, error_log = runner_error_payload(exc, "环境快照创建失败。")
            yield stream_event("error", ok=False, message=summary, error_log=error_log)
        except SQLAlchemyError as exc:
            db.rollback()
            reason = str(exc).strip()
            yield stream_event("error", ok=False, message=reason, error_log=reason)
        except ValueError as exc:
            db.rollback()
            yield stream_event("error", ok=False, message=str(exc), error_log=str(exc))
        except Exception as exc:
            db.rollback()
            reason = str(exc).strip() or "环境快照创建失败。"
            yield stream_event("error", ok=False, message=reason, error_log=reason)
        finally:
            db.close()

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/allocations/{allocation_id}/restore/{snapshot_id}")
def restore_snapshot(request: Request, allocation_id: int, snapshot_id: int):
    admin_response = require_admin_response(request, "/")
    if admin_response:
        return admin_response
    is_ajax = wants_json(request)
    redirect_url = "/"

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
        redirect_url = f"/hosts/{allocation.host_id}"
        snapshot = _require_snapshot(db, snapshot_id)
        original_status = allocation.status
        logs.append(f"恢复端口 {allocation.host_port} 到快照 {snapshot.snapshot_name}")
        DockerService(allocation.host).restore_snapshot(
            allocation,
            archive_path=snapshot.storage_path,
            image_ref=snapshot.image_ref,
            logs=logs,
        )
        allocation.status = AllocationStatus.RUNNING.value
        try:
            db.commit()
            if is_ajax:
                return log_response(logs + ["快照恢复成功"], ok=True, message=f"端口 {allocation.host_port} 已恢复到快照 {snapshot.snapshot_name}。")
            return operation_success(request, f"端口 {allocation.host_port} 已恢复到快照 {snapshot.snapshot_name}。", redirect_url)
        except Exception:
            allocation.status = original_status
            db.rollback()
            raise
    except RunnerError as exc:
        db.rollback()
        summary, error_log = runner_error_payload(exc, "快照恢复失败。")
        if is_ajax:
            return JSONResponse({"ok": False, "message": summary, "error_log": error_log}, status_code=200)
        return operation_error(request, error_log, redirect_url)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return operation_error(request, reason, redirect_url)
    except ValueError as exc:
        db.rollback()
        return operation_error(request, str(exc), redirect_url, status_code=404)
    finally:
        db.close()

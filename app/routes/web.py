from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import AdminStatus, AdminUser, Allocation, AllocationStatus, AuthType, ManagedHost, SnapshotRecord
from app.services.admission import check_allocation, recommended_defaults
from app.services.docker_engine import DockerService, slugify
from app.services.metrics import host_summary, parse_memory_usage, parse_percent
from app.services.snapshots import create_snapshot_record, effective_snapshot_policy
from app.services.ssh_client import RunnerError
from app.services.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE_SECONDS,
    client_ip,
    current_admin,
    password_hash,
    send_registration_email,
    sign_session,
    verify_password,
)


router = APIRouter()
settings = get_settings()


def render(request: Request, template_name: str, context: dict):
    context.setdefault("admin", getattr(request.state, "admin", None))
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


def runner_error_payload(exc: RunnerError, fallback_message: str) -> tuple[str, str]:
    summary = fallback_message.strip() or str(exc).strip() or "操作失败。"
    error_log = exc.log_text() or str(exc).strip()
    return summary, error_log


def ajax_response(request: Request, ok: bool, message: str, error_log: str = "", status_code: int = 200):
    accepts_json = "application/json" in (request.headers.get("accept") or "")
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or accepts_json:
        payload = {"ok": ok, "message": message}
        if error_log:
            payload["error_log"] = error_log
        return JSONResponse(payload, status_code=status_code)
    return None


def wants_json(request: Request) -> bool:
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )


def log_response(lines: list[str], ok: bool = True, message: str = "", error_log: str = "", status_code: int = 200):
    payload = {"ok": ok, "message": message, "logs": lines}
    if error_log:
        payload["error_log"] = error_log
    return JSONResponse(payload, status_code=status_code)


def stream_event(event: str, **payload) -> str:
    data = {"event": event, **payload}
    return json.dumps(data, ensure_ascii=False) + "\n"


def ssh_host_count(db: Session) -> int:
    return (
        db.query(ManagedHost)
        .filter(ManagedHost.auth_type.in_([AuthType.PASSWORD.value, AuthType.KEY.value]))
        .count()
    )


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
    reserve_cpu_cores: float,
    reserve_memory_gb: float,
    reserve_disk_gb: float,
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
        is_local=False,
        port_start=port_start,
        port_end=port_end,
        workspace_root=workspace_root,
        shared_mnt_path=shared_mnt_path,
        snapshot_root=snapshot_root,
        reserve_cpu_cores=reserve_cpu_cores,
        reserve_memory_gb=reserve_memory_gb,
        reserve_disk_gb=reserve_disk_gb,
        default_user_share=default_user_share,
        snapshot_keep_count=snapshot_keep_count,
        snapshot_interval_days=snapshot_interval_days,
        notes=notes or None,
    )


def validate_ssh_docker(host: ManagedHost) -> dict:
    docker = DockerService(host)
    docker_info = docker.docker_info()
    used_ports = docker.used_host_ports()
    available_ports = [
        port for port in range(host.port_start, host.port_end + 1) if port not in used_ports
    ]
    return {
        "docker_info": docker_info,
        "used_ports": sorted(used_ports),
        "available_ports": available_ports,
    }


def make_action_logs(*messages: str) -> list[str]:
    return [message for message in messages if message]


def parse_cached_images(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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
    return render(request, "login.html", {"settings": settings, "message": ""})


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


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
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
        if ssh_host_count(db) == 0:
            return render_initial_setup(request)
        hosts = db.query(ManagedHost).order_by(ManagedHost.name.asc()).all()
        allocations = db.query(Allocation).order_by(Allocation.created_at.desc()).limit(20).all()
        host_cards = []
        for host in hosts:
            summary = host_summary(db, host, timeout=5, include_heavy=False)
            total_cpu = float(summary["docker_info"].get("NCPU") or 0)
            total_memory_gb = float(summary["docker_info"].get("MemTotal") or 0) / (1024**3)
            host_cards.append(
                {
                    "host": host,
                    "summary": summary,
                    "total_cpu": total_cpu,
                    "total_memory_gb": total_memory_gb,
                }
            )
        return render(
            request,
            "dashboard.html",
            {
                "settings": settings,
                "host_cards": host_cards,
                "allocations": allocations,
            },
        )
    finally:
        db.close()


@router.get("/hosts")
def hosts_page(request: Request):
    db: Session = SessionLocal()
    try:
        if ssh_host_count(db) == 0:
            return render_initial_setup(request)
        hosts = db.query(ManagedHost).order_by(ManagedHost.name.asc()).all()
        host_rows = []
        for host in hosts:
            summary = host_summary(db, host, timeout=5, include_heavy=False)
            defaults = recommended_defaults(host, summary["docker_info"])
            host_rows.append({"host": host, "summary": summary, "defaults": defaults})
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
    is_local: bool = Form(False),
    port_start: int = Form(50000),
    port_end: int = Form(50050),
    workspace_root: str = Form("/workspace/tenants"),
    shared_mnt_path: str = Form("/mnt"),
    snapshot_root: str = Form("/mnt/docker_platform_snapshots"),
    reserve_cpu_cores: float = Form(8.0),
    reserve_memory_gb: float = Form(64.0),
    reserve_disk_gb: float = Form(200.0),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
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
            reserve_cpu_cores=reserve_cpu_cores,
            reserve_memory_gb=reserve_memory_gb,
            reserve_disk_gb=reserve_disk_gb,
            default_user_share=default_user_share,
            snapshot_keep_count=snapshot_keep_count,
            snapshot_interval_days=snapshot_interval_days,
            notes=notes,
        )
        logs.append("开始探测 SSH 与 Docker 环境")
        validate_ssh_docker(host)
        logs.append("探测通过，准备写入平台数据库")
        db.add(host)
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


@router.post("/setup/test")
def test_initial_host(
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
    reserve_cpu_cores: float = Form(8.0),
    reserve_memory_gb: float = Form(64.0),
    reserve_disk_gb: float = Form(200.0),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
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
            reserve_cpu_cores=reserve_cpu_cores,
            reserve_memory_gb=reserve_memory_gb,
            reserve_disk_gb=reserve_disk_gb,
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
    reserve_cpu_cores: float = Form(8.0),
    reserve_memory_gb: float = Form(64.0),
    reserve_disk_gb: float = Form(200.0),
    default_user_share: int = Form(10),
    snapshot_keep_count: int = Form(2),
    snapshot_interval_days: int = Form(14),
    notes: str = Form(""),
):
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
            reserve_cpu_cores=reserve_cpu_cores,
            reserve_memory_gb=reserve_memory_gb,
            reserve_disk_gb=reserve_disk_gb,
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
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse(f"/hosts/{host_id}", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts/{host_id}?error={message}", status_code=303)

    db: Session = SessionLocal()
    try:
        host = db.query(ManagedHost).filter(ManagedHost.id == host_id).first()
        host.snapshot_keep_count = max(snapshot_keep_count, 0)
        host.snapshot_interval_days = max(snapshot_interval_days, 1)
        db.commit()
        return respond_success("宿主机快照策略已更新。")
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).replace("\n", " ").strip()
        return respond_error(reason)
    finally:
        db.close()


@router.post("/hosts/{host_id}/delete")
def delete_host(request: Request, host_id: int):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse("/hosts", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts?error={message}", status_code=303)

    db: Session = SessionLocal()
    try:
        host = db.query(ManagedHost).filter(ManagedHost.id == host_id).first()
        if not host:
            return respond_error("未找到宿主机记录。", status_code=404)
        active_allocations = (
            db.query(Allocation)
            .filter(
                Allocation.host_id == host.id,
                Allocation.status != AllocationStatus.DELETED.value,
            )
            .count()
        )
        if active_allocations > 0:
            return respond_error("该宿主机仍存在未删除的容器分配记录，暂不能移除。")
        db.delete(host)
        db.commit()
        return respond_success(f"宿主机 {host.name} 已从平台记录中移除。")
    except SQLAlchemyError as exc:
        db.rollback()
        return respond_error(str(exc).strip())
    finally:
        db.close()


@router.get("/hosts/{host_id}")
def host_detail(request: Request, host_id: int):
    db: Session = SessionLocal()
    try:
        host = db.query(ManagedHost).filter(ManagedHost.id == host_id).first()
        if not host:
            return RedirectResponse("/hosts?error=未找到宿主机记录。", status_code=303)
        try:
            docker = DockerService(host)
            reachable = docker.ping()
        except RunnerError as exc:
            docker = None
            reachable = False
            docker_error = str(exc)
        else:
            docker_error = ""
        docker_info = docker.docker_info() if docker and reachable else {}
        host_images = parse_cached_images(host.cached_images)
        if not host_images and docker and reachable:
            try:
                host_images = docker.discover_images()
            except RunnerError:
                host_images = []
            if host_images:
                host.cached_images = json.dumps(host_images, ensure_ascii=False)
                db.commit()
        summary = {
            "reachable": reachable,
            "docker_info": docker_info,
            "stats": [],
            "gpus": docker.gpu_stats() if docker and reachable else [],
            "allocated_cpu": 0.0,
            "allocated_memory_gb": 0.0,
            "active_allocations": 0,
            "container_count": 0,
        }
        defaults = recommended_defaults(host, docker_info)
        db_allocations = (
            db.query(Allocation)
            .filter(Allocation.host_id == host.id)
            .order_by(Allocation.host_port.asc())
            .all()
        )
        snapshots = (
            db.query(SnapshotRecord)
            .filter(SnapshotRecord.host_id == host.id)
            .order_by(SnapshotRecord.created_at.desc())
            .limit(20)
            .all()
        )
        container_rows = (
            docker.managed_container_rows(host.port_start, host.port_end)
            if docker and reachable
            else []
        )
        if not host_images and container_rows:
            host_images = sorted(
                {
                    (row.get("image_name") or "").strip()
                    for row in container_rows
                    if (row.get("image_name") or "").strip()
                }
            )
            if host_images:
                host.cached_images = json.dumps(host_images, ensure_ascii=False)
                db.commit()
        stats_list = docker.list_container_stats() if docker and reachable else []
        stats_by_name = {item.get("Name"): item for item in stats_list}
        allocation_by_container_name = {
            allocation.container_name: allocation for allocation in db_allocations
        }
        allocation_by_host_port = {
            allocation.host_port: allocation for allocation in db_allocations
        }
        gpu_usage_by_name = docker.gpu_memory_by_container() if docker and reachable else {}
        workspace_usage_by_name = (
            docker.workspace_usage_gb_map(list(allocation_by_container_name.values()))
            if docker and reachable
            else {}
        )
        allocation_rows = []
        unified_container_rows = []
        resource_chart_rows = []
        visible_ports = set()
        for container_row in container_rows:
            allocation = allocation_by_container_name.get(
                container_row["container_name"]
            ) or allocation_by_host_port.get(container_row["host_port"])
            stats = stats_by_name.get(container_row["container_name"], {})
            cpu_percent = parse_percent(stats.get("CPUPerc", ""))
            memory_used_gb = parse_memory_usage(stats.get("MemUsage", ""))
            gpu_memory_used_mb = float(gpu_usage_by_name.get(container_row["container_name"], 0.0))
            workspace_used_gb = float(workspace_usage_by_name.get(container_row["container_name"], 0.0))

            if allocation:
                visible_ports.add(allocation.host_port)
                snapshot_keep_count, snapshot_interval_days = effective_snapshot_policy(allocation)
                allocation.status = container_row["status"]
                allocation.image_name = container_row["image_name"] or allocation.image_name
                allocation_rows.append(
                    {
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
                        "workspace_used_gb": workspace_used_gb,
                        "effective_snapshot_keep_count": snapshot_keep_count,
                        "effective_snapshot_interval_days": snapshot_interval_days,
                    }
                )
                unified_container_rows.append(allocation_rows[-1])
                resource_chart_rows.append(
                    {
                        "port": allocation.host_port,
                        "assignee": allocation.assignee,
                        "status": allocation.status,
                        "cpu_percent": round(cpu_percent, 2),
                        "memory_used_gb": round(memory_used_gb, 2),
                        "gpu_memory_used_mb": round(gpu_memory_used_mb, 2),
                        "workspace_used_gb": round(workspace_used_gb, 2),
                        "cpu_limit_cores": round(float(allocation.cpu_limit_cores or 0.0), 2),
                        "memory_limit_gb": round(float(allocation.memory_limit_gb or 0.0), 2),
                        "workspace_limit_gb": round(float(allocation.workspace_limit_gb or 0.0), 2),
                    }
                )
                continue

            external_row = (
                {
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
                    "workspace_used_gb": workspace_used_gb,
                    "allocation": None,
                    "stats": stats,
                    "effective_snapshot_keep_count": host.snapshot_keep_count,
                    "effective_snapshot_interval_days": host.snapshot_interval_days,
                }
            )
            unified_container_rows.append(external_row)

            resource_chart_rows.append(
                {
                    "port": container_row["host_port"],
                    "assignee": container_row["container_name"],
                    "status": container_row["status"],
                    "cpu_percent": round(cpu_percent, 2),
                    "memory_used_gb": round(memory_used_gb, 2),
                    "gpu_memory_used_mb": round(gpu_memory_used_mb, 2),
                    "workspace_used_gb": round(workspace_used_gb, 2),
                    "cpu_limit_cores": 0.0,
                    "memory_limit_gb": 0.0,
                    "workspace_limit_gb": 0.0,
                }
            )
        running_count = sum(1 for row in allocation_rows if row["allocation"].status == AllocationStatus.RUNNING.value)
        summary["active_allocations"] = running_count
        summary["container_count"] = len(container_rows)
        summary["allocated_cpu"] = sum(float(row["allocation"].cpu_limit_cores or 0.0) for row in allocation_rows)
        summary["allocated_memory_gb"] = sum(float(row["allocation"].memory_limit_gb or 0.0) for row in allocation_rows)
        used_ports = {row["host_port"] for row in container_rows}
        available_ports = [
            port for port in range(host.port_start, host.port_end + 1) if port not in used_ports
        ]
        running_allocations = [
            row for row in allocation_rows if row["allocation"].status == AllocationStatus.RUNNING.value
        ]
        stopped_allocations = [
            row for row in allocation_rows if row["allocation"].status == AllocationStatus.STOPPED.value
        ]
        unified_container_rows = sorted(unified_container_rows, key=lambda row: row["host_port"])
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
                "used_ports": sorted(used_ports),
                "snapshots": snapshots,
                "base_image": settings.default_base_image,
                "host_images": host_images,
                "resource_chart_rows": resource_chart_rows,
                "docker_error": docker_error,
            },
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
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse(f"/hosts/{host_id}", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts/{host_id}?error={message}", status_code=303)

    db: Session = SessionLocal()
    host = None
    container_name = ""
    logs: list[str] = []
    try:
        host = db.query(ManagedHost).filter(ManagedHost.id == host_id).first()
        if not host:
            return respond_error("未找到宿主机记录。", status_code=404)
        docker = DockerService(host)
        container_name = f"{slugify(host.name)}-{host_port}"
        logs.append(f"准备创建容器 {container_name}")
        allocation = (
            db.query(Allocation)
            .filter(
                Allocation.host_id == host_id,
                Allocation.container_name == container_name,
            )
            .first()
        )
        if allocation and allocation.status != AllocationStatus.DELETED.value:
            return respond_error(f"端口 {host_port} 已存在历史分配记录且未删除完成，请先检查该容器状态。")

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
                return respond_error(f"端口 {host_port} 已被 Docker 实际占用，请选择其他端口。")
        logs.append("检查准入配额")
        decision = check_allocation(db, host, cpu_limit_cores, memory_limit_gb)
        if not decision.allowed:
            reason = "；".join(decision.reasons)
            return respond_error(reason)

        selected_image = (base_image_override or "").strip()
        if not selected_image:
            return respond_error("请选择或填写该宿主机本地已有基础镜像。", status_code=400)

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
        db.commit()
        if is_ajax:
            return log_response(logs + ["容器创建成功"], ok=True, message=f"容器创建成功，端口 {host_port} 已分配给 {assignee}。")
        return respond_success(f"容器创建成功，端口 {host_port} 已分配给 {assignee}。")
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
        return respond_error(error_log)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return respond_error(reason)
    except Exception as exc:
        db.rollback()
        reason = str(exc).strip()
        return respond_error(reason)
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
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse(f"/hosts/{allocation.host_id}", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts/{allocation.host_id}?error={message}", status_code=303)

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
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
            exclude_allocation_id=allocation.id,
        )
        if not decision.allowed:
            reason = "；".join(decision.reasons)
            return respond_error(reason)

        allocation.cpu_limit_cores = cpu_limit_cores
        allocation.memory_limit_gb = memory_limit_gb
        allocation.workspace_limit_gb = workspace_limit_gb
        logs.append(f"更新端口 {allocation.host_port} 资源配置")
        try:
            DockerService(allocation.host).update_resources(allocation, logs=logs)
            db.commit()
            if is_ajax:
                return log_response(logs + ["资源更新成功"], ok=True, message=f"端口 {allocation.host_port} 资源上限已更新。")
            return respond_success(f"端口 {allocation.host_port} 资源上限已更新。")
        except Exception:
            allocation.cpu_limit_cores = original_state["cpu_limit_cores"]
            allocation.memory_limit_gb = original_state["memory_limit_gb"]
            allocation.workspace_limit_gb = original_state["workspace_limit_gb"]
            db.rollback()
            raise
    except RunnerError as exc:
        db.rollback()
        summary, error_log = runner_error_payload(exc, "资源更新失败。")
        if is_ajax:
            return JSONResponse({"ok": False, "message": summary, "error_log": error_log}, status_code=200)
        return respond_error(error_log)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return respond_error(reason)
    except ValueError as exc:
        db.rollback()
        return respond_error(str(exc), status_code=404)
    finally:
        db.close()


@router.post("/allocations/{allocation_id}/mounts")
def update_allocation_mounts(
    request: Request,
    allocation_id: int,
    extra_mounts: str = Form(""),
):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse(f"/hosts/{allocation.host_id}", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts/{allocation.host_id}?error={message}", status_code=303)

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
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
            return respond_success(f"端口 {allocation.host_port} 挂载变更已记录，待下次启动时自动重建生效。")
        except Exception:
            allocation.extra_mounts = original_state["extra_mounts"]
            allocation.pending_rebuild = original_state["pending_rebuild"]
            allocation.pending_rebuild_reason = original_state["pending_rebuild_reason"]
            db.rollback()
            raise
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return respond_error(reason)
    except ValueError as exc:
        db.rollback()
        return respond_error(str(exc), status_code=404)
    finally:
        db.close()


@router.post("/allocations/{allocation_id}/action")
def allocation_action(
    request: Request,
    allocation_id: int,
    action: str = Form(...),
):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse(f"/hosts/{allocation.host_id}", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts/{allocation.host_id}?error={message}", status_code=303)

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
        docker = DockerService(allocation.host)
        original_status = allocation.status
        success_message = "操作执行完成。"
        if action == "stop":
            logs.append(f"尝试停止端口 {allocation.host_port}")
            docker.stop_container(allocation)
            success_message = f"端口 {allocation.host_port} 已停止。"
        elif action == "start":
            if allocation.pending_rebuild:
                logs.append("检测到需重建状态，准备重建后启动")
                docker.rebuild_container(allocation)
                success_message = f"端口 {allocation.host_port} 已按最新挂载配置重建并启动。"
            else:
                logs.append(f"尝试启动端口 {allocation.host_port}")
                docker.start_container(allocation)
                success_message = f"端口 {allocation.host_port} 已启动。"
        elif action == "delete":
            logs.append(f"尝试删除端口 {allocation.host_port} 容器")
            docker.remove_container(allocation)
            success_message = f"端口 {allocation.host_port} 对应容器已删除。"
        else:
            return respond_error("不支持的操作类型。", status_code=400)
        try:
            db.commit()
            if is_ajax:
                return log_response(logs + ["维护操作成功"], ok=True, message=success_message)
            return respond_success(success_message)
        except Exception:
            allocation.status = original_status
            db.rollback()
            raise
    except RunnerError as exc:
        db.rollback()
        db = SessionLocal()
        try:
            allocation = _require_allocation(db, allocation_id)
            DockerService(allocation.host).reconcile_allocation_state(allocation)
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        action_message = {
            "stop": "停止操作失败。",
            "start": "启动操作失败。",
            "delete": "删除操作失败。",
        }.get(action, "维护操作失败。")
        summary, error_log = runner_error_payload(exc, action_message)
        if is_ajax:
            return JSONResponse({"ok": False, "message": summary, "error_log": error_log}, status_code=200)
        return respond_error(error_log)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return respond_error(reason)
    except ValueError as exc:
        db.rollback()
        return respond_error(str(exc), status_code=404)
    finally:
        db.close()


@router.post("/hosts/{host_id}/containers/action")
def external_container_action(
    request: Request,
    host_id: int,
    container_name: str = Form(...),
    action: str = Form(...),
):
    db: Session = SessionLocal()
    try:
        host = db.query(ManagedHost).filter(ManagedHost.id == host_id).first()
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
            docker.stop_container_by_name(container_name)
            message = f"容器 {container_name} 已停止。"
        elif action == "start":
            docker.start_container_by_name(container_name)
            message = f"容器 {container_name} 已启动。"
        elif action == "delete":
            docker.remove_container_by_name(container_name)
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


@router.post("/allocations/{allocation_id}/snapshot")
def create_snapshot(
    request: Request,
    allocation_id: int,
    snapshot_keep_count: int = Form(...),
    snapshot_interval_days: int = Form(...),
):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse(f"/hosts/{allocation.host_id}", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts/{allocation.host_id}?error={message}", status_code=303)

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
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
            return respond_success(
                f"环境快照创建完成。该端口已改为独立快照策略：保留 {allocation.snapshot_keep_count} 份，周期 {allocation.snapshot_interval_days} 天。"
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
        return respond_error(error_log)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return respond_error(reason)
    except ValueError as exc:
        db.rollback()
        return respond_error(str(exc), status_code=404)
    finally:
        db.close()


@router.post("/allocations/{allocation_id}/snapshot/stream")
def create_snapshot_stream(
    allocation_id: int,
    snapshot_keep_count: int = Form(...),
    snapshot_interval_days: int = Form(...),
):
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
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def respond_success(message: str):
        if is_ajax:
            return JSONResponse({"ok": True, "message": message})
        return RedirectResponse(f"/hosts/{allocation.host_id}", status_code=303)

    def respond_error(message: str, status_code: int = 200):
        if is_ajax:
            summary, error_log = split_error_payload(message)
            return JSONResponse(
                {"ok": False, "message": summary, "error_log": error_log},
                status_code=status_code,
            )
        return RedirectResponse(f"/hosts/{allocation.host_id}?error={message}", status_code=303)

    db: Session = SessionLocal()
    logs: list[str] = []
    try:
        allocation = _require_allocation(db, allocation_id)
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
            return respond_success(f"端口 {allocation.host_port} 已恢复到快照 {snapshot.snapshot_name}。")
        except Exception:
            allocation.status = original_status
            db.rollback()
            raise
    except RunnerError as exc:
        db.rollback()
        summary, error_log = runner_error_payload(exc, "快照恢复失败。")
        if is_ajax:
            return JSONResponse({"ok": False, "message": summary, "error_log": error_log}, status_code=200)
        return respond_error(error_log)
    except SQLAlchemyError as exc:
        db.rollback()
        reason = str(exc).strip()
        return respond_error(reason)
    except ValueError as exc:
        db.rollback()
        return respond_error(str(exc), status_code=404)
    finally:
        db.close()

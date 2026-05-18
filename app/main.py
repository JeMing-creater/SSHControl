from __future__ import annotations

import asyncio
import contextlib

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import Allocation, ManagedHost
from app.routes.web import router as web_router
from app.services.auth import current_admin, current_platform_user
from app.services.host_cache import schedule_host_refresh
from app.services.snapshots import create_snapshot_record, snapshot_due


settings = get_settings()


async def host_monitor_scheduler(app: FastAPI) -> None:
    while True:
        db = SessionLocal()
        try:
            host_ids = [
                item.id
                for item in db.query(ManagedHost.id).filter(ManagedHost.enabled == True).all()  # noqa: E712
            ]
        finally:
            db.close()
        for host_id in host_ids:
            schedule_host_refresh(host_id, full=False)
        await asyncio.sleep(settings.host_monitor_interval_seconds)


async def snapshot_scheduler(app: FastAPI) -> None:
    while True:
        db = SessionLocal()
        try:
            allocations = db.query(Allocation).all()
            for allocation in allocations:
                if not allocation.host.enabled:
                    continue
                if snapshot_due(allocation):
                    create_snapshot_record(db, allocation)
        except Exception:
            # Keep the platform available even if snapshot rotation fails.
            pass
        finally:
            db.close()
        await asyncio.sleep(settings.scheduler_interval_seconds)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    app.state.templates = Jinja2Templates(directory=str(settings.templates_dir))
    scheduler_task = asyncio.create_task(snapshot_scheduler(app))
    monitor_task = asyncio.create_task(host_monitor_scheduler(app))
    app.state.scheduler_task = scheduler_task
    app.state.monitor_task = monitor_task
    try:
        yield
    finally:
        scheduler_task.cancel()
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.middleware("http")
async def require_admin_login(request: Request, call_next):
    public_paths = {"/login", "/register", "/user-login"}
    if request.url.path.startswith("/static") or request.url.path in public_paths:
        request.state.admin = None
        request.state.platform_user = None
        return await call_next(request)
    db = SessionLocal()
    try:
        admin = current_admin(request, db)
        platform_user = None if admin else current_platform_user(request, db)
    finally:
        db.close()
    if not admin and not platform_user:
        return RedirectResponse("/user-login", status_code=303)
    request.state.admin = admin
    request.state.platform_user = platform_user
    return await call_next(request)


app.include_router(web_router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = []
    for error in exc.errors():
        loc = " -> ".join(str(part) for part in error.get("loc", []))
        msg = error.get("msg", "校验失败")
        details.append(f"{loc}: {msg}")
    message = "；".join(details) if details else "请求参数校验失败。"

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            {"ok": False, "message": message, "details": exc.errors()},
            status_code=422,
        )
    return JSONResponse({"detail": exc.errors(), "message": message}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    message = "服务端执行失败。"
    error_log = f"{type(exc).__name__}: {exc}"
    if request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(
            {"ok": False, "message": message, "error_log": error_log},
            status_code=500,
        )
    return JSONResponse(
        {"ok": False, "message": message, "error_log": error_log},
        status_code=500,
    )

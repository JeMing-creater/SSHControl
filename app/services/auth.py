from __future__ import annotations

import base64
import hashlib
import hmac
import json
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AdminStatus, AdminUser


SESSION_COOKIE = "control_admin_session"
SESSION_MAX_AGE_SECONDS = 5 * 24 * 60 * 60


def password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(password_hash(password), hashed)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def sign_session(account: str, ip_address: str) -> str:
    settings = get_settings()
    payload_data = {
        "account": account,
        "ip": ip_address,
        "exp": int((datetime.utcnow() + timedelta(seconds=SESSION_MAX_AGE_SECONDS)).timestamp()),
    }
    payload = base64.urlsafe_b64encode(json.dumps(payload_data, separators=(",", ":")).encode("utf-8")).decode("ascii")
    signature = hmac.new(
        settings.auth_secret_key.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{payload}.{signature}"


def read_session(value: str | None, ip_address: str) -> str | None:
    if not value or "." not in value:
        return None
    settings = get_settings()
    payload, signature = value.rsplit(".", 1)
    expected = hmac.new(
        settings.auth_secret_key.encode("utf-8"),
        payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return None
    if data.get("ip") != ip_address:
        return None
    if int(data.get("exp") or 0) < int(datetime.utcnow().timestamp()):
        return None
    return str(data.get("account") or "") or None


def current_admin(request: Request, db: Session) -> dict | None:
    settings = get_settings()
    account = read_session(request.cookies.get(SESSION_COOKIE), client_ip(request))
    if not account:
        return None
    if account == settings.root_admin_account:
        return {"account": account, "is_root": True}
    user = db.query(AdminUser).filter(AdminUser.account == account).first()
    if user and user.status == AdminStatus.APPROVED.value:
        return {"account": user.account, "is_root": False, "user": user}
    return None


def send_registration_email(user: AdminUser) -> str:
    settings = get_settings()
    if not settings.smtp_host:
        return "SMTP 未配置，已跳过邮件发送。"

    msg = EmailMessage()
    from_email = settings.smtp_from_email or settings.smtp_username
    msg["From"] = from_email
    msg["To"] = settings.registration_notify_email
    msg["Subject"] = f"Control Platform 管理员注册申请：{user.account}"
    msg.set_content(
        "\n".join(
            [
                "收到新的管理员注册申请：",
                f"账号：{user.account}",
                f"邮箱：{user.email}",
                f"学号：{user.student_id}",
                f"申请时间：{datetime.utcnow().isoformat()} UTC",
            ]
        )
    )

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
    return "注册通知邮件已发送。"

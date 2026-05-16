from __future__ import annotations

from dataclasses import dataclass

import paramiko

from app.models import AuthType, ManagedHost


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class RunnerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: str | None = None,
        exit_code: int | None = None,
        stderr: str | None = None,
        stdout: str | None = None,
    ):
        super().__init__(message)
        self.command = command
        self.exit_code = exit_code
        self.stderr = stderr or ""
        self.stdout = stdout or ""

    def log_text(self) -> str:
        parts: list[str] = []
        message = str(self).strip()
        if message:
            parts.append(message)
        if self.command:
            parts.append(f"$ {self.command}")
        if self.exit_code is not None:
            parts.append(f"[exit_code] {self.exit_code}")
        if self.stderr:
            parts.append("[stderr]")
            parts.append(self.stderr)
        if self.stdout:
            parts.append("[stdout]")
            parts.append(self.stdout)
        return "\n".join(part for part in parts if part is not None).strip()


class BaseRunner:
    def run(self, command: str, timeout: int = 120) -> CommandResult:
        raise NotImplementedError


class SSHRunner(BaseRunner):
    def __init__(self, host: ManagedHost):
        self.host = host

    def run(self, command: str, timeout: int = 120) -> CommandResult:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.host.address,
            "port": self.host.ssh_port,
            "username": self.host.ssh_user,
            "timeout": timeout,
        }
        if self.host.auth_type == AuthType.PASSWORD.value:
            connect_kwargs["password"] = self.host.ssh_password
        elif self.host.auth_type == AuthType.KEY.value:
            connect_kwargs["key_filename"] = self.host.ssh_key_path

        try:
            client.connect(**connect_kwargs)
            stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            return CommandResult(
                stdout=stdout.read().decode().strip(),
                stderr=stderr.read().decode().strip(),
                exit_code=exit_code,
            )
        except Exception as exc:
            raise RunnerError(
                f"SSH 连接或命令执行失败：{exc}",
                command=command,
            ) from exc
        finally:
            client.close()


def get_runner(host: ManagedHost) -> BaseRunner:
    if host.auth_type == AuthType.LOCAL.value:
        raise RunnerError(
            (
                f"宿主机 {host.name} 仍配置为 local 认证。当前平台已切换为 SSH-only 管理，"
                "请在宿主机记录中配置 password 或 key 认证。"
            )
        )
    return SSHRunner(host)

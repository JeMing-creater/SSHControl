from __future__ import annotations

import json
import re
import shlex
import time
from datetime import datetime

from app.models import Allocation, AllocationStatus, ManagedHost
from app.services.image_filters import filter_supported_base_images
from app.services.ssh_client import RunnerError, get_runner


SHM_REBUILD_REASON = (
    "/dev/shm 运行态已按内存弹性上限临时同步；Docker HostConfig 需要重建容器后才能持久生效，"
    "普通重启前请先执行平台重建启动。"
)
SHM_REBUILD_FAILED_REASON = (
    "/dev/shm 共享内存大小无法热更新；当前 Docker 内存上限已更新，"
    "共享内存将在下次重建容器时按内存弹性上限生效。"
)
REMOTE_COMMAND_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned.lower() or "host"


def parse_json_lines(payload: str) -> list[dict]:
    items: list[dict] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def parse_mounts(raw_mounts: str | None) -> list[str]:
    if not raw_mounts:
        return []
    chunks = re.split(r"[;\n]+", raw_mounts)
    mounts = []
    for chunk in chunks:
        mount = chunk.strip()
        if mount:
            mounts.append(mount)
    return mounts


def parse_docker_size_to_gb(value: str | None) -> float:
    text = (value or "").strip()
    if not text:
        return 0.0
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)", text, re.IGNORECASE)
    if not match:
        return 0.0
    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "b": 1 / (1024**3),
        "kb": 1 / (1000**2),
        "kib": 1 / (1024**2),
        "mb": 1 / 1000,
        "mib": 1 / 1024,
        "gb": 1.0,
        "gib": 1.0,
        "tb": 1000.0,
        "tib": 1024.0,
    }
    return number * factors.get(unit, 0.0)


def redact_sensitive_text(value: str | BaseException) -> str:
    text = str(value or "")
    if not text:
        return text
    return re.sub(
        r"(PASSWORD=)(?:'[^']*'|\"[^\"]*\"|\S+)",
        r"\1******",
        text,
    )


def memory_gb_to_docker_size(value: float | int | None) -> str:
    numeric = max(float(value or 0.0), 1.0)
    if numeric.is_integer():
        return f"{int(numeric)}g"
    return f"{numeric:g}g"


def append_rebuild_reason(allocation: Allocation, reason: str) -> None:
    normalized_reason = reason.strip()
    if not normalized_reason:
        return
    existing = (allocation.pending_rebuild_reason or "").strip()
    if normalized_reason in existing:
        allocation.pending_rebuild = True
        return
    allocation.pending_rebuild = True
    allocation.pending_rebuild_reason = f"{existing}；{normalized_reason}" if existing else normalized_reason


def explain_backend_error(raw: str | BaseException) -> str:
    text = str(raw or "").strip()
    lower = text.lower()
    if not text:
        return "后端命令执行失败，但未返回详细错误。"
    if "address already in use" in lower or "port is already allocated" in lower:
        return "端口已被占用，请更换端口或先清理占用该端口的旧容器。"
    if "no such image" in lower or "pull access denied" in lower:
        return "宿主机本地没有找到指定基础镜像，请重新选择本机已有镜像。"
    if "no space left on device" in lower:
        return "宿主机磁盘空间不足，Docker 无法继续写入。"
    if "minimum memory limit" in lower or "memoryswap" in lower or ("memory" in lower and "invalid" in lower):
        return "Docker 内存上限参数不合法，请调高内存弹性上限后重试。"
    if "cannot update memory limit" in lower or "memory limit should be smaller" in lower:
        return "Docker 拒绝更新内存上限，可能低于当前容器实际占用或 Docker 允许范围。"
    if "name is already in use" in lower or ("conflict" in lower and "container" in lower):
        return "容器名已被占用，平台会尝试清理残留后重试。"
    if "driver/library version mismatch" in lower or "failed to initialize nvml" in lower:
        return "NVIDIA 驱动与 NVML 库版本不匹配，需要修复宿主机 GPU 驱动环境。"
    if "nvidia-container" in lower or "nvidia runtime" in lower:
        return "NVIDIA 容器运行时异常，平台会尝试无破坏修复后重试。"
    if "permission denied" in lower:
        return "权限不足，当前 SSH 用户无法完成该 Docker 操作。"
    if "timeout" in lower or "timed out" in lower:
        return "远端命令执行超时，请检查 SSH、Docker 负载或稍后重试。"
    if "ssh" in lower:
        return "SSH 连接或远端命令执行失败，请检查宿主机网络和 SSH 服务。"
    return "后端命令执行失败，原始错误已保留在下方日志。"


def _container_host_ports(detail: dict) -> set[int]:
    ports: set[int] = set()
    port_bindings = detail.get("HostConfig", {}).get("PortBindings", {}) or {}
    for bindings in port_bindings.values():
        for binding in bindings or []:
            raw_port = binding.get("HostPort")
            try:
                ports.add(int(raw_port))
            except (TypeError, ValueError):
                continue
    return ports


class DockerService:
    def __init__(self, host: ManagedHost):
        self.host = host
        self.runner = get_runner(host)

    def _host_command(self, command: str) -> str:
        return f"export PATH={REMOTE_COMMAND_PATH}:$PATH; {command}"

    def _run_result(self, command: str, timeout: int = 120):
        return self.runner.run(self._host_command(command), timeout=timeout)

    def _log(self, logs: list[str] | None, message: str) -> None:
        if logs is not None:
            logs.append(redact_sensitive_text(message))

    def _run_with_retries(
        self,
        command: str,
        *,
        timeout: int = 120,
        logs: list[str] | None = None,
        retries: int = 1,
        recoverable: bool = True,
    ) -> str:
        last_exc: RunnerError | None = None
        for attempt in range(1, retries + 2):
            self._log(logs, f"[尝试 {attempt}] {command}")
            try:
                result = self._run(command, timeout=timeout)
                self._log(logs, f"[成功] {command}")
                return result
            except RunnerError as exc:
                last_exc = exc
                self._log(logs, f"[失败] {explain_backend_error(exc)}")
                self._log(logs, f"[原始错误] {exc}")
                if not recoverable or attempt > retries:
                    break
                if "already in use" in str(exc).lower() or "name is already in use" in str(exc).lower():
                    self._log(logs, "检测到容器名或端口冲突，尝试清理残留容器后重试。")
                    try:
                        container_name = command.split("--name", 1)[1].split()[0].strip("'\"")
                        self.ensure_container_absent(container_name)
                    except Exception:
                        pass
                elif "ssh" in str(exc).lower():
                    self._log(logs, "检测到 SSH 连接异常，准备重试。")
                    time.sleep(2)
                else:
                    time.sleep(1)
        if last_exc is not None:
            raise last_exc
        raise RunnerError(f"Command failed: {command}", command=command)

    def _run(self, command: str, timeout: int = 120) -> str:
        try:
            result = self._run_result(command, timeout=timeout)
        except RunnerError as exc:
            if not exc.command:
                exc.command = redact_sensitive_text(command)
            raise
        if not result.ok:
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=redact_sensitive_text(command),
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )
        return result.stdout

    def _error_text(self, exc: BaseException) -> str:
        if isinstance(exc, RunnerError):
            return exc.log_text()
        return str(exc)

    def _is_nvidia_runtime_error(self, exc: BaseException) -> bool:
        text = self._error_text(exc).lower()
        markers = (
            "nvidia-persistenced",
            "/run/nvidia-persistenced/socket",
            "nvidia-container",
            "nvidia runtime",
            "nvml",
            "driver/library version mismatch",
        )
        return any(marker in text for marker in markers)

    def recover_nvidia_runtime(self, logs: list[str] | None = None) -> bool:
        self._log(logs, "检测到 NVIDIA runtime 异常，尝试无破坏修复 nvidia-persistenced。")
        script = f"""
set -u
export PATH={REMOTE_COMMAND_PATH}:$PATH
if [ -S /run/nvidia-persistenced/socket ]; then
    echo "nvidia-persistenced socket 已存在。"
    exit 0
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl start nvidia-persistenced 2>&1 || true
fi

if [ ! -S /run/nvidia-persistenced/socket ] && command -v nvidia-persistenced >/dev/null 2>&1; then
    mkdir -p /run/nvidia-persistenced
    nvidia-persistenced --user root >/tmp/control-nvidia-persistenced.log 2>&1 || true
fi

sleep 1
if [ -S /run/nvidia-persistenced/socket ]; then
    echo "nvidia-persistenced socket 已恢复。"
    exit 0
fi

echo "nvidia-persistenced socket 仍不存在。" >&2
systemctl status nvidia-persistenced --no-pager -l 2>&1 | tail -40 >&2 || true
tail -40 /tmp/control-nvidia-persistenced.log 2>/dev/null >&2 || true
nvidia-smi 2>&1 | head -40 >&2 || true
exit 1
"""
        result = self._run_result(f"sh -c {shlex.quote(script)}", timeout=90)
        if result.ok:
            self._log(logs, result.stdout.strip() or "nvidia-persistenced 修复命令已完成。")
            return True
        error_log = "\n".join(part for part in [result.stderr, result.stdout] if part).strip()
        self._log(logs, "NVIDIA runtime 自动修复未成功。")
        if error_log:
            self._log(logs, error_log)
        return False

    def ping(self, timeout: int = 120) -> bool:
        try:
            self._run("docker info >/dev/null 2>&1", timeout=timeout)
            return True
        except RunnerError:
            return False

    def inspect_container_runtime(self, container_name: str) -> tuple[bool, str | None]:
        command = (
            "docker inspect -f '{{.State.Status}}' "
            f"{shlex.quote(container_name)}"
        )
        result = self._run_result(command, timeout=30)
        if result.ok:
            return True, (result.stdout or "").strip() or None

        error_text = "\n".join(part for part in [result.stderr, result.stdout] if part).strip()
        normalized_error_text = error_text.lower()
        if "no such object" in normalized_error_text or "no such container" in normalized_error_text:
            return False, None

        raise RunnerError(
            error_text or f"Command failed: {command}",
            command=command,
            exit_code=result.exit_code,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    def ensure_container_absent(self, container_name: str) -> None:
        exists, _ = self.inspect_container_runtime(container_name)
        if not exists:
            return

        command = f"docker rm -f {shlex.quote(container_name)}"
        result = self._run_result(command, timeout=600)
        if result.ok:
            return

        exists_after, _ = self.inspect_container_runtime(container_name)
        if not exists_after:
            return

        raise RunnerError(
            result.stderr or result.stdout or f"Command failed: {command}",
            command=command,
            exit_code=result.exit_code,
            stderr=result.stderr,
            stdout=result.stdout,
        )

    def reconcile_allocation_state(self, allocation: Allocation) -> str:
        exists, runtime_status = self.inspect_container_runtime(allocation.container_name)
        if not exists:
            allocation.status = AllocationStatus.DELETED.value
            return allocation.status

        if runtime_status == "running":
            allocation.status = AllocationStatus.RUNNING.value
        else:
            allocation.status = AllocationStatus.STOPPED.value
        return allocation.status

    def docker_info(self, timeout: int = 120) -> dict:
        raw = self._run("docker info --format '{{json .}}'", timeout=timeout)
        return json.loads(raw)

    def container_disk_usage_gb_map(self, timeout: int = 120) -> dict[str, dict[str, float | str]]:
        raw = self._run("docker ps -a --size --format '{{json .}}'", timeout=timeout)
        usage_by_name: dict[str, dict[str, float | str]] = {}
        for row in parse_json_lines(raw):
            name = (row.get("Names") or row.get("Name") or "").strip().lstrip("/")
            if not name:
                continue
            size_text = (row.get("Size") or "").strip()
            writable_text = size_text.split("(", 1)[0].strip()
            virtual_text = ""
            virtual_match = re.search(r"virtual\s+([^)]+)", size_text, re.IGNORECASE)
            if virtual_match:
                virtual_text = virtual_match.group(1).strip()
            usage_by_name[name] = {
                "disk_used_gb": parse_docker_size_to_gb(writable_text),
                "disk_virtual_gb": parse_docker_size_to_gb(virtual_text),
                "disk_size_text": size_text,
            }
        return usage_by_name

    def supports_container_disk_quota(self, timeout: int = 120) -> bool:
        try:
            info = self.docker_info(timeout=timeout)
        except RunnerError:
            return False

        driver = str(info.get("Driver") or info.get("StorageDriver") or "").strip().lower()
        driver_status: dict[str, str] = {}
        for item in info.get("DriverStatus") or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                driver_status[str(item[0]).strip().lower()] = str(item[1]).strip().lower()
        backing_fs = driver_status.get("backing filesystem") or str(info.get("BackingFilesystem") or "").strip().lower()
        if driver not in {"overlay2", "overlayfs"} or backing_fs != "xfs":
            return False

        root_dir = str(info.get("DockerRootDir") or "/var/lib/docker").strip() or "/var/lib/docker"
        try:
            result = self._run_result(f"findmnt -no OPTIONS {shlex.quote(root_dir)}", timeout=min(timeout, 30))
        except RunnerError:
            return False
        if not result.ok:
            return False
        options = {part.strip().lower() for part in (result.stdout or "").split(",") if part.strip()}
        return bool({"pquota", "prjquota"} & options)

    def container_storage_opt(self, allocation: Allocation, logs: list[str] | None = None) -> str | None:
        limit = float(allocation.workspace_limit_gb or 0.0)
        if limit <= 0:
            return None

        mode = (self.host.workspace_limit_mode or "metadata_only").strip().lower()
        if mode == "metadata_only":
            self._log(logs, "当前宿主机磁盘限额模式为 metadata_only，跳过 Docker writable-layer 限额。")
            return None

        supported = self.supports_container_disk_quota(timeout=60)
        if supported:
            return f"--storage-opt size={limit}g"

        if mode == "strict_storage_opt":
            raise RunnerError(
                "当前宿主机不满足 Docker 容器磁盘限额条件（需要 overlay2 + xfs + pquota/prjquota）。",
                command="docker run --storage-opt size=...",
            )

        self._log(logs, "当前宿主机不支持 Docker writable-layer 磁盘限额，已仅保留平台准入控制。")
        return None

    def configured_shm_size_bytes(self, allocation: Allocation, timeout: int = 30) -> int:
        command = (
            "docker inspect -f '{{.HostConfig.ShmSize}}' "
            f"{shlex.quote(allocation.container_name)}"
        )
        raw = self._run(command, timeout=timeout).strip()
        try:
            return int(raw)
        except ValueError:
            return 0

    def runtime_shm_size_bytes(self, allocation: Allocation, timeout: int = 30) -> int:
        script = f"""
set -eu
export PATH={REMOTE_COMMAND_PATH}:$PATH
name={shlex.quote(allocation.container_name)}
if line=$(docker exec "$name" df -B1 /dev/shm 2>/dev/null | tail -n 1) && [ -n "$line" ]; then
    set -- $line
    printf '%s\\n' "$2"
    exit 0
fi
line=$(docker exec "$name" df -k /dev/shm | tail -n 1)
set -- $line
printf '%s\\n' "$(($2 * 1024))"
"""
        raw = self._run(f"sh -c {shlex.quote(script)}", timeout=timeout).strip()
        try:
            return int(raw)
        except ValueError:
            return 0

    def desired_shm_size_bytes(self, allocation: Allocation) -> int:
        return int(max(float(allocation.memory_limit_gb or 0.0), 1.0) * (1024**3))

    def sync_runtime_shm_size(self, allocation: Allocation, logs: list[str] | None = None) -> bool:
        desired_size = memory_gb_to_docker_size(allocation.memory_limit_gb)
        current_size = self.runtime_shm_size_bytes(allocation)
        desired_bytes = self.desired_shm_size_bytes(allocation)
        if current_size >= desired_bytes:
            self._log(logs, f"/dev/shm 当前已满足内存弹性上限：{current_size / (1024**3):.1f} GB。")
            return True

        script = f"""
set -eu
export PATH={REMOTE_COMMAND_PATH}:$PATH
name={shlex.quote(allocation.container_name)}
size={shlex.quote(desired_size)}
pid=$(docker inspect -f '{{{{.State.Pid}}}}' "$name")
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
    echo "容器未运行，无法热调整 /dev/shm。"
    exit 2
fi
if ! command -v nsenter >/dev/null 2>&1; then
    echo "宿主机缺少 nsenter，无法热调整 /dev/shm。"
    exit 3
fi
nsenter --target "$pid" --mount -- mount -o remount,size="$size" /dev/shm
docker exec "$name" df -h /dev/shm | tail -1
"""
        command = f"sh -c {shlex.quote(script)}"
        try:
            output = self._run_with_retries(command, timeout=30, logs=logs, retries=0, recoverable=False)
        except RunnerError as exc:
            self._log(
                logs,
                "/dev/shm 无法热调整；Docker 不支持 docker update 修改共享内存，"
                "该值将在下次重建容器时按内存弹性上限生效。",
            )
            self._log(logs, f"/dev/shm 热调整失败原因：{exc}")
            return False
        self._log(logs, f"/dev/shm 已临时调整为 {desired_size}。{output.strip()}")
        return True

    def filesystem_usage_gb(self, path: str | None = None, timeout: int = 120) -> dict[str, float | str]:
        target = (path or self.host.workspace_root or "/").strip() or "/"
        script = f"""
set -u
target={shlex.quote(target)}
while [ ! -e "$target" ] && [ "$target" != "/" ]; do
    target=$(dirname "$target")
done
df_bin=df
if [ -x /usr/bin/df ]; then
    df_bin=/usr/bin/df
elif [ -x /bin/df ]; then
    df_bin=/bin/df
fi
last_line() {{
    line=
    while IFS= read -r current; do
        [ -n "$current" ] && line=$current
    done
    printf '%s\\n' "$line"
}}
if output=$(LC_ALL=C "$df_bin" -B1 --output=size,used,avail,target "$target" 2>/dev/null | last_line) && [ -n "$output" ]; then
    printf 'bytes %s\\n' "$output"
    exit 0
fi
if output=$(LC_ALL=C "$df_bin" -Pk "$target" 2>/dev/null | last_line) && [ -n "$output" ]; then
    printf 'kbytes %s\\n' "$output"
    exit 0
fi
if output=$(LC_ALL=C "$df_bin" -k "$target" 2>/dev/null | last_line) && [ -n "$output" ]; then
    printf 'kbytes %s\\n' "$output"
    exit 0
fi
exit 1
"""
        result = self._run_result(f"sh -c {shlex.quote(script)}", timeout=timeout)
        if not result.ok or not result.stdout.strip():
            return {}

        parts = result.stdout.strip().split()
        try:
            mode = parts[0]
            if mode == "bytes":
                if len(parts) < 5:
                    return {}
                total_bytes = float(parts[1])
                used_bytes = float(parts[2])
                avail_bytes = float(parts[3])
                mounted_on = " ".join(parts[4:])
            elif mode == "kbytes":
                if len(parts) < 6:
                    return {}
                total_bytes = float(parts[2]) * 1024
                used_bytes = float(parts[3]) * 1024
                avail_bytes = float(parts[4]) * 1024
                mounted_on = " ".join(parts[6:]) if len(parts) > 6 else parts[-1]
            else:
                return {}
        except (IndexError, ValueError):
            return {}
        return {
            "total_gb": total_bytes / (1024**3),
            "used_gb": used_bytes / (1024**3),
            "avail_gb": avail_bytes / (1024**3),
            "target": mounted_on,
        }

    def list_images(self, timeout: int = 120) -> list[str]:
        raw = self._run("docker images --format '{{.Repository}}:{{.Tag}}'", timeout=timeout)
        images: list[str] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            image = line.strip()
            if not image or image in seen or image.startswith("<none>:"):
                continue
            seen.add(image)
            images.append(image)
        return images

    def images_from_containers(self, timeout: int = 240) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()
        for detail in self.container_details(timeout=timeout):
            image = (detail.get("Config", {}) or {}).get("Image") or ""
            image = image.strip()
            if not image or image.startswith("<none>:") or image in seen:
                continue
            seen.add(image)
            images.append(image)
        return images

    def _images_from_dockerfile_text(self, text: str) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) >= 2 and parts[0].upper() == "FROM":
                image = parts[1].strip()
                if image and image.upper() != "SCRATCH" and image not in seen:
                    seen.add(image)
                    images.append(image)
        return images

    def images_from_root_dockerfile(self, timeout: int = 30) -> list[str]:
        result = self._run_result("cat /root/Dockerfile", timeout=timeout)
        if not result.ok or not result.stdout.strip():
            return []
        return self._images_from_dockerfile_text(result.stdout)

    def discover_images(self, timeout: int = 120) -> list[str]:
        command_timeout = max(4, timeout)
        try:
            images = filter_supported_base_images(self.list_images(timeout=command_timeout))
        except RunnerError:
            images = []
        if images:
            return images

        try:
            images = filter_supported_base_images(self.images_from_containers(timeout=command_timeout))
        except RunnerError:
            images = []
        if images:
            return images

        try:
            images = filter_supported_base_images(self.images_from_root_dockerfile(timeout=min(command_timeout, 8)))
        except RunnerError:
            images = []
        if images:
            return images

        command = (
            "find /root /workspace /mnt -maxdepth 4 -type f "
            "\\( -name Dockerfile -o -name dockerfile \\) 2>/dev/null | head -20 | "
            "while read -r file; do echo '###'\"$file\"; cat \"$file\"; done"
        )
        result = self._run_result(command, timeout=command_timeout)
        if not result.ok or not result.stdout.strip():
            return []
        return filter_supported_base_images(self._images_from_dockerfile_text(result.stdout))

    def container_details(self, timeout: int = 240) -> list[dict]:
        raw = self._run(
            "docker ps -aq --no-trunc | xargs -r docker inspect "
            "--format '{{json .}}'",
            timeout=timeout,
        )
        return parse_json_lines(raw)

    def used_host_ports(self, timeout: int = 240) -> set[int]:
        used_ports: set[int] = set()
        for detail in self.container_details(timeout=timeout):
            used_ports.update(_container_host_ports(detail))
        return used_ports

    def managed_container_rows(self, port_start: int, port_end: int, timeout: int = 240) -> list[dict]:
        rows: list[dict] = []
        for detail in self.container_details(timeout=timeout):
            container_name = (detail.get("Name") or "").lstrip("/")
            if not container_name:
                continue

            host_port = next(
                (
                    port
                    for port in sorted(_container_host_ports(detail))
                    if port_start <= port <= port_end
                ),
                None,
            )

            if host_port is None:
                continue

            state = detail.get("State", {}) or {}
            status = "running" if state.get("Running") else "stopped"
            image_name = detail.get("Config", {}).get("Image") or ""
            rows.append(
                {
                    "container_name": container_name,
                    "host_port": host_port,
                    "status": status,
                    "image_name": image_name,
                    "detail": detail,
                }
            )
        return sorted(rows, key=lambda item: item["host_port"])

    def list_container_stats(self, timeout: int = 120) -> list[dict]:
        raw = self._run("docker stats --no-stream --format '{{json .}}'", timeout=timeout)
        return parse_json_lines(raw)

    def container_id_name_map(self, timeout: int = 120) -> dict[str, str]:
        raw = self._run_result(
            "docker ps -q --no-trunc | xargs -r docker inspect --format '{{.Id}} {{.Name}}'",
            timeout=timeout,
        )
        if not raw.ok or not raw.stdout.strip():
            return {}

        mapping: dict[str, str] = {}
        for line in raw.stdout.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) != 2:
                continue
            container_id, container_name = parts
            mapping[container_id] = container_name.lstrip("/")
        return mapping

    def gpu_stats(self, timeout: int = 120) -> list[dict]:
        command = (
            "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.total,memory.used "
            "--format=csv,noheader,nounits"
        )
        try:
            raw = self._run(command, timeout=timeout)
        except RunnerError:
            return []

        records = []
        for line in raw.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) != 5:
                continue
            records.append(
                {
                    "index": parts[0],
                    "name": parts[1],
                    "utilization_gpu": float(parts[2] or 0),
                    "memory_total_mb": float(parts[3] or 0),
                    "memory_used_mb": float(parts[4] or 0),
                }
            )
        return records

    def gpu_memory_detail_by_container(self, timeout: int = 120) -> dict[str, list[dict]]:
        try:
            raw = self._run(
                "nvidia-smi --query-compute-apps=pid,gpu_uuid,used_memory --format=csv,noheader,nounits",
                timeout=timeout,
            )
        except RunnerError:
            return {}

        if not raw.strip():
            return {}

        gpu_index_by_uuid: dict[str, str] = {}
        try:
            gpu_raw = self._run(
                "nvidia-smi --query-gpu=index,uuid --format=csv,noheader,nounits",
                timeout=timeout,
            )
            for line in gpu_raw.splitlines():
                parts = [part.strip() for part in line.split(",")]
                if len(parts) >= 2:
                    gpu_index_by_uuid[parts[1]] = parts[0]
        except RunnerError:
            gpu_index_by_uuid = {}

        id_to_name = self.container_id_name_map(timeout=timeout)
        if not id_to_name:
            return {}

        usage_by_container_gpu: dict[str, dict[str, float]] = {}
        for line in raw.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 3:
                continue
            try:
                pid = int(parts[0])
                gpu_uuid = parts[1]
                used_memory_mb = float(parts[2] or 0)
            except ValueError:
                continue

            cgroup_result = self._run_result(f"cat /proc/{pid}/cgroup", timeout=min(timeout, 15))
            if not cgroup_result.ok or not cgroup_result.stdout.strip():
                continue

            container_id = None
            cgroup_text = cgroup_result.stdout
            for pattern in (
                r"docker[-/]([0-9a-f]{64})",
                r"/docker/([0-9a-f]{64})",
                r"([0-9a-f]{64})",
            ):
                match = re.search(pattern, cgroup_text)
                if match:
                    candidate = match.group(1)
                    if candidate in id_to_name:
                        container_id = candidate
                        break
                    for known_id in id_to_name:
                        if known_id.startswith(candidate):
                            container_id = known_id
                            break
                if container_id:
                    break

            if not container_id:
                continue

            container_name = id_to_name.get(container_id)
            if not container_name:
                continue
            gpu_index = gpu_index_by_uuid.get(gpu_uuid, gpu_uuid)
            container_usage = usage_by_container_gpu.setdefault(container_name, {})
            container_usage[gpu_index] = container_usage.get(gpu_index, 0.0) + used_memory_mb

        return {
            container_name: [
                {
                    "gpu_index": gpu_index,
                    "used_memory_mb": round(used_memory_mb, 2),
                }
                for gpu_index, used_memory_mb in sorted(
                    gpu_usage.items(),
                    key=lambda item: int(item[0]) if str(item[0]).isdigit() else str(item[0]),
                )
            ]
            for container_name, gpu_usage in usage_by_container_gpu.items()
        }

    def ensure_workspace_dir(self, allocation: Allocation) -> str:
        workspace_dir = f"{self.host.workspace_root}/{allocation.container_name}"
        self._run(f"mkdir -p {shlex.quote(workspace_dir)}")
        return workspace_dir

    def snapshot_dir(self, allocation: Allocation) -> str:
        host_slug = slugify(self.host.name)
        return f"{self.host.snapshot_root}/{host_slug}/{allocation.host_port}"

    def verify_container_access(self, allocation: Allocation, timeout_seconds: int = 20) -> None:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            checks = [
                "ps -ef | grep [s]shd",
            ]
            if allocation.x11_enabled:
                checks.extend(
                    [
                        "command -v xauth >/dev/null 2>&1",
                        "sshd -t",
                        "grep -Eq '^X11Forwarding[[:space:]]+yes$' /etc/ssh/sshd_config",
                    ]
                )

            all_ready = True
            for check in checks:
                result = self._run_result(
                    f"docker exec {shlex.quote(allocation.container_name)} sh -lc {shlex.quote(check)}",
                    timeout=15,
                )
                if not result.ok:
                    all_ready = False
                    break
            if all_ready:
                return
            time.sleep(2)
        raise RunnerError(
            (
                f"容器 {allocation.container_name} 已创建但自检失败，"
                "sshd 或 X11 运行条件未正常就绪，已阻止继续分配。"
            )
        )

    def cleanup_failed_container(self, allocation: Allocation, logs: list[str] | None = None) -> None:
        self.ensure_container_absent(allocation.container_name)
        workspace_dir = f"{self.host.workspace_root}/{allocation.container_name}"
        result = self._run_result(f"rmdir {shlex.quote(workspace_dir)} 2>/dev/null || true", timeout=30)
        if result.ok:
            self._log(logs, "已清理失败创建留下的空工作目录；非空目录会被保留以避免误删数据。")

    def prepare_x11_support(self, allocation: Allocation) -> None:
        if not allocation.x11_enabled:
            return

        script = """
set -e
if ! command -v apt-get >/dev/null 2>&1; then
    echo "当前镜像未提供 apt-get，平台无法自动安装 X11 依赖。" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
missing_packages=""
for pkg in xauth x11-apps; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        missing_packages="$missing_packages $pkg"
    fi
done

if [ -n "$missing_packages" ]; then
    apt-get update
    apt-get install -y --no-install-recommends $missing_packages
    rm -rf /var/lib/apt/lists/*
fi

mkdir -p /run/sshd /var/run/sshd
config=/etc/ssh/sshd_config
touch "$config"

ensure_sshd_option() {
    key="$1"
    value="$2"
    if grep -Eq "^[#[:space:]]*${key}[[:space:]]+" "$config"; then
        sed -ri "s|^[#[:space:]]*${key}[[:space:]]+.*$|${key} ${value}|g" "$config"
    else
        printf '%s %s\\n' "$key" "$value" >> "$config"
    fi
}

ensure_sshd_option X11Forwarding yes
ensure_sshd_option X11UseLocalhost yes
ensure_sshd_option AllowTcpForwarding yes

sshd -t
if pgrep -xo sshd >/dev/null 2>&1; then
    kill -HUP "$(pgrep -xo sshd)"
fi
"""
        command = (
            f"docker exec {shlex.quote(allocation.container_name)} "
            f"sh -lc {shlex.quote(script)}"
        )
        self._run(command, timeout=1800)

    def create_container(self, allocation: Allocation, logs: list[str] | None = None) -> None:
        workspace_dir = self.ensure_workspace_dir(allocation)
        self.ensure_container_absent(allocation.container_name)
        mounts = [f"-v {shlex.quote(workspace_dir)}:/workspace"]
        if allocation.shared_mnt_enabled:
            mounts.append(f"-v {shlex.quote(self.host.shared_mnt_path)}:/mnt")
        if allocation.extra_mounts:
            for mount in parse_mounts(allocation.extra_mounts):
                mounts.append(f"-v {mount}")
        storage_opt = self.container_storage_opt(allocation, logs=logs)

        command_parts = [
            "docker run -d --restart unless-stopped",
            f"--name {shlex.quote(allocation.container_name)}",
            f"-p {allocation.host_port}:22",
            f"--cpus {allocation.cpu_limit_cores}",
            f"--memory {allocation.memory_limit_gb}g",
            f"--memory-swap {allocation.memory_limit_gb}g",
            f"--shm-size {memory_gb_to_docker_size(allocation.memory_limit_gb)}",
            storage_opt or "",
            "--gpus all" if allocation.all_gpus_visible else "",
            "--label control.platform=1",
            f"--label {shlex.quote(f'control.host_port={allocation.host_port}')}",
            f"--label {shlex.quote(f'control.container_name={allocation.container_name}')}",
            f"--label {shlex.quote(f'control.assignee={allocation.assignee}')}",
            f"--label {shlex.quote(f'control.purpose={allocation.purpose}')}",
            f"--label {shlex.quote(f'control.cpu_limit_cores={allocation.cpu_limit_cores}')}",
            f"--label {shlex.quote(f'control.memory_limit_gb={allocation.memory_limit_gb}')}",
            f"--label {shlex.quote(f'control.workspace_limit_gb={allocation.workspace_limit_gb}')}",
            f"-e PASSWORD={shlex.quote(allocation.root_password)}",
            *mounts,
            shlex.quote(allocation.image_name),
        ]
        command = " ".join(part for part in command_parts if part)
        last_exc: BaseException | None = None
        recovered_nvidia = False
        try:
            for attempt in range(1, 4):
                self._log(logs, f"[尝试 {attempt}] {command}")
                try:
                    self._run(command, timeout=600)
                    self._log(logs, f"[成功] {command}")
                    last_exc = None
                    break
                except RunnerError as exc:
                    last_exc = exc
                    self._log(logs, f"[失败] {explain_backend_error(exc)}")
                    self._log(logs, f"[原始错误] {exc}")
                    try:
                        self._log(logs, "清理本次失败创建留下的同名容器，避免污染下一次重试。")
                        self.ensure_container_absent(allocation.container_name)
                    except RunnerError as cleanup_exc:
                        self._log(logs, f"残留容器清理失败：{cleanup_exc}")
                    if attempt >= 3:
                        break
                    if allocation.all_gpus_visible and self._is_nvidia_runtime_error(exc):
                        if recovered_nvidia:
                            self._log(logs, "NVIDIA runtime 已尝试修复但仍失败，停止继续重试。")
                            break
                        recovered_nvidia = True
                        if not self.recover_nvidia_runtime(logs=logs):
                            break
                        continue
                    if "already in use" in str(exc).lower() or "name is already in use" in str(exc).lower():
                        self._log(logs, "检测到容器名或端口冲突，已清理残留后重试。")
                        continue
                    if "ssh" in str(exc).lower():
                        self._log(logs, "检测到 SSH 连接异常，等待后重试。")
                        time.sleep(2)
                        continue
                    time.sleep(1)
            if last_exc is not None:
                raise last_exc
            self.prepare_x11_support(allocation)
            self.verify_container_access(allocation)
            allocation.status = AllocationStatus.RUNNING.value
            allocation.pending_rebuild = False
            allocation.pending_rebuild_reason = None
        except Exception:
            self.cleanup_failed_container(allocation, logs=logs)
            raise

    def update_resources(self, allocation: Allocation, logs: list[str] | None = None) -> None:
        command = (
            f"docker update --cpus {allocation.cpu_limit_cores} "
            f"--memory {allocation.memory_limit_gb}g "
            f"--memory-swap {allocation.memory_limit_gb}g "
            f"{shlex.quote(allocation.container_name)}"
        )
        self._run_with_retries(command, logs=logs, retries=1)
        configured_shm_ok = self.configured_shm_size_bytes(allocation) >= self.desired_shm_size_bytes(allocation)
        shm_synced = self.sync_runtime_shm_size(allocation, logs=logs)
        if not configured_shm_ok:
            append_rebuild_reason(
                allocation,
                SHM_REBUILD_REASON if shm_synced else SHM_REBUILD_FAILED_REASON,
            )

    def stop_container(self, allocation: Allocation) -> None:
        command = f"docker stop {shlex.quote(allocation.container_name)}"
        result = self._run_result(command, timeout=600)
        if not result.ok:
            exists, runtime_status = self.inspect_container_runtime(allocation.container_name)
            if not exists:
                allocation.status = AllocationStatus.DELETED.value
                return
            if runtime_status != "running":
                allocation.status = AllocationStatus.STOPPED.value
                return
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )
        allocation.status = AllocationStatus.STOPPED.value

    def stop_container_by_name(self, container_name: str) -> str:
        command = f"docker stop {shlex.quote(container_name)}"
        result = self._run_result(command, timeout=600)
        if not result.ok:
            exists, runtime_status = self.inspect_container_runtime(container_name)
            if not exists:
                return AllocationStatus.DELETED.value
            if runtime_status != "running":
                return AllocationStatus.STOPPED.value
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )
        return AllocationStatus.STOPPED.value

    def start_container(self, allocation: Allocation) -> None:
        command = f"docker start {shlex.quote(allocation.container_name)}"
        result = self._run_result(command, timeout=600)
        if not result.ok:
            exists, runtime_status = self.inspect_container_runtime(allocation.container_name)
            if not exists:
                allocation.status = AllocationStatus.DELETED.value
                raise RunnerError(
                    result.stderr or result.stdout or f"Command failed: {command}",
                    command=command,
                    exit_code=result.exit_code,
                    stderr=result.stderr,
                    stdout=result.stdout,
                )
            if runtime_status == "running":
                self.verify_container_access(allocation)
                allocation.status = AllocationStatus.RUNNING.value
                return
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )

        try:
            self.verify_container_access(allocation)
            if allocation.pending_rebuild:
                self.sync_runtime_shm_size(allocation)
        except Exception:
            self._run_result(f"docker stop {shlex.quote(allocation.container_name)}", timeout=120)
            self.reconcile_allocation_state(allocation)
            raise
        allocation.status = AllocationStatus.RUNNING.value

    def restart_container(self, allocation: Allocation, logs: list[str] | None = None) -> None:
        command = f"docker restart {shlex.quote(allocation.container_name)}"
        self._log(logs, f"重启端口 {allocation.host_port} 容器")
        result = self._run_result(command, timeout=900)
        if not result.ok:
            exists, runtime_status = self.inspect_container_runtime(allocation.container_name)
            if not exists:
                allocation.status = AllocationStatus.DELETED.value
            elif runtime_status == "running":
                allocation.status = AllocationStatus.RUNNING.value
            elif runtime_status:
                allocation.status = AllocationStatus.STOPPED.value
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )
        try:
            self.verify_container_access(allocation)
            if allocation.pending_rebuild:
                self.sync_runtime_shm_size(allocation, logs=logs)
        except Exception:
            self.reconcile_allocation_state(allocation)
            raise
        allocation.status = AllocationStatus.RUNNING.value
        self._log(logs, f"端口 {allocation.host_port} 容器已重启")

    def start_container_by_name(self, container_name: str) -> str:
        command = f"docker start {shlex.quote(container_name)}"
        result = self._run_result(command, timeout=600)
        if not result.ok:
            exists, runtime_status = self.inspect_container_runtime(container_name)
            if not exists:
                raise RunnerError(
                    result.stderr or result.stdout or f"Command failed: {command}",
                    command=command,
                    exit_code=result.exit_code,
                    stderr=result.stderr,
                    stdout=result.stdout,
                )
            if runtime_status == "running":
                return AllocationStatus.RUNNING.value
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )
        return AllocationStatus.RUNNING.value

    def restart_container_by_name(self, container_name: str, logs: list[str] | None = None) -> str:
        command = f"docker restart {shlex.quote(container_name)}"
        self._log(logs, f"重启容器 {container_name}")
        result = self._run_result(command, timeout=900)
        if not result.ok:
            exists, runtime_status = self.inspect_container_runtime(container_name)
            if not exists:
                return AllocationStatus.DELETED.value
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )
        self._log(logs, f"容器 {container_name} 已重启")
        return AllocationStatus.RUNNING.value

    def remove_container(self, allocation: Allocation) -> None:
        self.ensure_container_absent(allocation.container_name)
        allocation.status = AllocationStatus.DELETED.value

    def remove_container_by_name(self, container_name: str) -> str:
        self.ensure_container_absent(container_name)
        return AllocationStatus.DELETED.value

    def remove_image(self, image_ref: str) -> None:
        command = f"docker rmi -f {shlex.quote(image_ref)}"
        result = self._run_result(command, timeout=600)
        if not result.ok:
            raise RunnerError(
                result.stderr or result.stdout or f"Command failed: {command}",
                command=command,
                exit_code=result.exit_code,
                stderr=result.stderr,
                stdout=result.stdout,
            )

    def rebuild_container(self, allocation: Allocation, image_name: str | None = None, logs: list[str] | None = None) -> None:
        image_to_use = image_name or allocation.image_name
        self.ensure_container_absent(allocation.container_name)
        original_image = allocation.image_name
        allocation.image_name = image_to_use
        try:
            self.create_container(allocation, logs=logs)
        except Exception:
            allocation.image_name = original_image
            self.ensure_container_absent(allocation.container_name)
            allocation.status = AllocationStatus.DELETED.value
            raise

    def create_snapshot(self, allocation: Allocation, logs: list[str] | None = None) -> tuple[str, str]:
        snapshot_timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        image_ref = f"control-snapshot/{slugify(self.host.name)}:{allocation.host_port}-{snapshot_timestamp}"
        snapshot_dir = self.snapshot_dir(allocation)
        archive_path = f"{snapshot_dir}/{snapshot_timestamp}.tar"
        self._run_with_retries(f"mkdir -p {shlex.quote(snapshot_dir)}", logs=logs, retries=1)
        self._run_with_retries(
            f"docker commit {shlex.quote(allocation.container_name)} {shlex.quote(image_ref)}",
            timeout=600,
            logs=logs,
            retries=1,
        )
        self._run_with_retries(
            f"docker save -o {shlex.quote(archive_path)} {shlex.quote(image_ref)}",
            timeout=3600,
            logs=logs,
            retries=1,
        )
        return image_ref, archive_path

    def restore_snapshot(self, allocation: Allocation, archive_path: str, image_ref: str, logs: list[str] | None = None) -> None:
        self._run_with_retries(f"docker load -i {shlex.quote(archive_path)}", timeout=3600, logs=logs, retries=1)
        self.rebuild_container(allocation, image_name=image_ref, logs=logs)

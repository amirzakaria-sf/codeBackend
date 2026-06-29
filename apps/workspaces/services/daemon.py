import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx
from django.conf import settings

from .provisioning import ProvisioningError, _validate_project_name


class DaemonStartError(Exception):
    """Raised when OpenCode daemon startup fails."""


def _venv_bin_path(project_root: Path) -> Path:
    backend_venv = project_root / "backend" / "venv"
    unix_bin = backend_venv / "bin"
    windows_bin = backend_venv / "Scripts"

    if unix_bin.exists():
        return unix_bin
    if windows_bin.exists():
        return windows_bin

    raise DaemonStartError(
        f"Backend venv bin directory does not exist for project at: {project_root}"
    )


def project_root_from_name(project_name: str) -> Path:
    normalized_name = _validate_project_name(project_name)
    managed_root_raw = settings.MANAGED_PROJECTS_ROOT
    if not managed_root_raw:
        raise ProvisioningError("MANAGED_PROJECTS_ROOT is not configured.")

    managed_root = Path(managed_root_raw).expanduser().resolve()
    project_root = (managed_root / normalized_name).resolve()

    try:
        project_root.relative_to(managed_root)
    except ValueError:
        raise DaemonStartError("Resolved project path escapes MANAGED_PROJECTS_ROOT.")
    if not project_root.exists():
        raise DaemonStartError(f"Project directory does not exist: {project_root}")

    return project_root


def start_opencode_daemon(project_name: str, allocated_port: int) -> subprocess.Popen:
    """
    Start an isolated OpenCode daemon for a project by injecting backend venv/bin
    into PATH and binding server to localhost + allocated port.
    """

    project_root = project_root_from_name(project_name)
    venv_bin_path = _venv_bin_path(project_root)
    backend_venv = project_root / "backend" / "venv"

    env = os.environ.copy()
    original_path = env.get("PATH", "")
    env["PATH"] = f"{venv_bin_path}{os.pathsep}{original_path}" if original_path else str(venv_bin_path)
    env["VIRTUAL_ENV"] = str(backend_venv)

    opencode_binary = settings.OPENCODE_BINARY_PATH
    opencode_subcommand = settings.OPENCODE_WEB_SUBCOMMAND

    command = [
        opencode_binary,
        opencode_subcommand,
        "--port",
        str(allocated_port),
        "--hostname",
        "127.0.0.1",
    ]

    return subprocess.Popen(
        command,
        cwd=project_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=False,
        start_new_session=True,
    )


def allocate_available_port(start: int = 8010, end: int = 9000) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise DaemonStartError("No available daemon port found in configured range.")


def is_daemon_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop_opencode_daemon(pid: int, timeout_seconds: float = 8.0) -> bool:
    if not is_daemon_running(pid):
        return True

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True

    started_at = time.monotonic()
    while time.monotonic() - started_at < timeout_seconds:
        if not is_daemon_running(pid):
            return True
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return True

    return not is_daemon_running(pid)


def daemon_health(port: int | None) -> dict:
    if not port:
        return {"reachable": False, "healthy": False}
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/global/health", timeout=3)
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001
        return {"reachable": False, "healthy": False}

    return {
        "reachable": True,
        "healthy": bool(payload.get("healthy", False)),
        "version": payload.get("version"),
    }

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from hashlib import sha1
from pathlib import Path

import httpx
from django.conf import settings

from .provisioning import ProvisioningError, managed_projects_root

logger = logging.getLogger(__name__)


class DaemonStartError(Exception):
    """Raised when OpenCode daemon startup fails."""


def _venv_bin_path(project_root: Path) -> Path | None:
    backend_venv = project_root / "backend" / "venv"
    unix_bin = backend_venv / "bin"
    windows_bin = backend_venv / "Scripts"

    if unix_bin.exists():
        return unix_bin
    if windows_bin.exists():
        return windows_bin

    return None


def project_root_from_path(project_absolute_path: str) -> Path:
    managed_root = managed_projects_root()
    project_root = Path(project_absolute_path).expanduser().resolve()

    try:
        project_root.relative_to(managed_root)
    except ValueError:
        raise DaemonStartError("Resolved project path escapes MANAGED_PROJECTS_ROOT.")
    if not project_root.exists():
        raise DaemonStartError(f"Project directory does not exist: {project_root}")

    return project_root


def daemon_directory_for_project(project_absolute_path: str) -> str:
    project_root = project_root_from_path(project_absolute_path)
    sandbox_mode = (getattr(settings, "OPENCODE_DAEMON_SANDBOX_MODE", "host") or "host").strip().lower()
    if sandbox_mode == "docker":
        return (getattr(settings, "OPENCODE_DAEMON_CONTAINER_WORKDIR", "/workspace") or "/workspace").strip() or "/workspace"
    return str(project_root)


def wait_for_daemon_ready(process: subprocess.Popen, allocated_port: int, *, project_absolute_path: str | None = None) -> dict:
    timeout_seconds = max(3.0, float(getattr(settings, "DAEMON_STARTUP_TIMEOUT_SECONDS", 20)))
    started_at = time.monotonic()
    last_status = {"state": "unreachable", "pid_alive": False, "port_reachable": False, "reachable": False, "healthy": False, "busy": False}

    while time.monotonic() - started_at < timeout_seconds:
        return_code = process.poll()
        if return_code is not None:
            raise DaemonStartError(
                f"OpenCode daemon exited before startup completed (exit code {return_code}) on port {allocated_port}."
            )

        last_status = daemon_runtime_status(process.pid, allocated_port)
        if last_status.get("reachable") and (last_status.get("healthy") or last_status.get("busy")):
            return last_status

        time.sleep(0.5)

    if project_absolute_path:
        stop_opencode_daemon(process.pid, allocated_port=allocated_port, project_absolute_path=project_absolute_path)
    else:
        stop_opencode_daemon(process.pid, allocated_port=allocated_port)

    raise DaemonStartError(
        "OpenCode daemon failed to become ready "
        f"within {timeout_seconds:.0f}s on port {allocated_port} "
        f"(state={last_status.get('state')}, pid_alive={last_status.get('pid_alive')}, "
        f"port_reachable={last_status.get('port_reachable')}, healthy={last_status.get('healthy')}, busy={last_status.get('busy')})."
    )


def _ensure_project_opencode_config(project_root: Path) -> None:
    destination = project_root / "opencode.json"
    if not destination.exists():
        template_path_raw = (getattr(settings, "GLOBAL_TEMPLATE_PATH", "") or "").strip()
        if not template_path_raw:
            raise DaemonStartError("Workspace opencode.json is required but GLOBAL_TEMPLATE_PATH is not configured.")

        template_path = Path(template_path_raw).expanduser().resolve()
        if not template_path.exists() or not template_path.is_file():
            raise DaemonStartError("GLOBAL_TEMPLATE_PATH does not point to a valid file.")

        shutil.copy2(template_path, destination)

    _validate_project_opencode_config(destination)


def _allowed_worker_agents() -> list[str]:
    raw = getattr(settings, "ORCHESTRATION_ALLOWED_WORKER_AGENTS", "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _required_orchestration_agents() -> list[str]:
    ordered: list[str] = []
    for agent_name in [settings.PLAN_AGENT_NAME, settings.SUPERVISOR_AGENT_NAME, *_allowed_worker_agents(), settings.ORCHESTRATION_FALLBACK_WORKER_AGENT]:
        cleaned = str(agent_name or "").strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def _build_config_diagnostics(config_path: Path, raw_payload: dict, *, valid_json: bool = True) -> dict:
    agent_payload = raw_payload.get("agent") if isinstance(raw_payload, dict) else None
    configured_agents = sorted(
        str(key).strip()
        for key in (agent_payload.keys() if isinstance(agent_payload, dict) else [])
        if str(key).strip()
    )
    default_agent = str(raw_payload.get("default_agent") or "").strip() if isinstance(raw_payload, dict) else ""
    required_agents = _required_orchestration_agents()
    allowed_worker_agents = _allowed_worker_agents()
    fallback_agent = str(getattr(settings, "ORCHESTRATION_FALLBACK_WORKER_AGENT", "") or "").strip()
    resolved_plan_agent = str(getattr(settings, "PLAN_AGENT_NAME", "") or "").strip()
    resolved_supervisor_agent = str(getattr(settings, "SUPERVISOR_AGENT_NAME", "") or "").strip()
    missing_agents = [agent_name for agent_name in required_agents if agent_name not in configured_agents]
    worker_agent_status = {agent_name: agent_name in configured_agents for agent_name in allowed_worker_agents}

    warnings: list[str] = []
    if default_agent == resolved_plan_agent:
        warnings.append("default_agent is set to the planner agent; unexpected fallback could still produce empty diffs.")
    if fallback_agent and fallback_agent not in configured_agents:
        warnings.append("Configured orchestration fallback agent is missing from opencode.json.")

    return {
        "config_path": str(config_path),
        "exists": config_path.exists(),
        "valid_json": valid_json,
        "default_agent": default_agent,
        "available_agents": configured_agents,
        "required_agents": required_agents,
        "missing_agents": missing_agents,
        "resolved_plan_agent": resolved_plan_agent,
        "resolved_plan_agent_exists": resolved_plan_agent in configured_agents,
        "resolved_supervisor_agent": resolved_supervisor_agent,
        "resolved_supervisor_agent_exists": resolved_supervisor_agent in configured_agents,
        "requested_worker_agents": allowed_worker_agents,
        "requested_worker_agent_exists": worker_agent_status,
        "fallback_worker_agent": fallback_agent,
        "fallback_worker_agent_exists": fallback_agent in configured_agents if fallback_agent else False,
        "warnings": warnings,
        "orchestration_ready": not missing_agents and bool(configured_agents),
    }


def inspect_project_opencode_config(config_path: Path) -> dict:
    if not config_path.exists() or not config_path.is_file():
        return {
            "config_path": str(config_path),
            "exists": False,
            "valid_json": False,
            "available_agents": [],
            "required_agents": _required_orchestration_agents(),
            "missing_agents": _required_orchestration_agents(),
            "resolved_plan_agent": settings.PLAN_AGENT_NAME,
            "resolved_plan_agent_exists": False,
            "resolved_supervisor_agent": settings.SUPERVISOR_AGENT_NAME,
            "resolved_supervisor_agent_exists": False,
            "requested_worker_agents": _allowed_worker_agents(),
            "requested_worker_agent_exists": {agent_name: False for agent_name in _allowed_worker_agents()},
            "fallback_worker_agent": settings.ORCHESTRATION_FALLBACK_WORKER_AGENT,
            "fallback_worker_agent_exists": False,
            "warnings": [],
            "orchestration_ready": False,
            "error": f"Workspace config file is missing: {config_path}",
        }

    try:
        raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return {
            "config_path": str(config_path),
            "exists": True,
            "valid_json": False,
            "available_agents": [],
            "required_agents": _required_orchestration_agents(),
            "missing_agents": _required_orchestration_agents(),
            "resolved_plan_agent": settings.PLAN_AGENT_NAME,
            "resolved_plan_agent_exists": False,
            "resolved_supervisor_agent": settings.SUPERVISOR_AGENT_NAME,
            "resolved_supervisor_agent_exists": False,
            "requested_worker_agents": _allowed_worker_agents(),
            "requested_worker_agent_exists": {agent_name: False for agent_name in _allowed_worker_agents()},
            "fallback_worker_agent": settings.ORCHESTRATION_FALLBACK_WORKER_AGENT,
            "fallback_worker_agent_exists": False,
            "warnings": [],
            "orchestration_ready": False,
            "error": f"Workspace opencode.json is invalid JSON: {error}",
        }
    except OSError as error:
        return {
            "config_path": str(config_path),
            "exists": True,
            "valid_json": False,
            "available_agents": [],
            "required_agents": _required_orchestration_agents(),
            "missing_agents": _required_orchestration_agents(),
            "resolved_plan_agent": settings.PLAN_AGENT_NAME,
            "resolved_plan_agent_exists": False,
            "resolved_supervisor_agent": settings.SUPERVISOR_AGENT_NAME,
            "resolved_supervisor_agent_exists": False,
            "requested_worker_agents": _allowed_worker_agents(),
            "requested_worker_agent_exists": {agent_name: False for agent_name in _allowed_worker_agents()},
            "fallback_worker_agent": settings.ORCHESTRATION_FALLBACK_WORKER_AGENT,
            "fallback_worker_agent_exists": False,
            "warnings": [],
            "orchestration_ready": False,
            "error": f"Unable to read workspace config {config_path}: {error}",
        }

    if not isinstance(raw_payload, dict):
        return {
            **_build_config_diagnostics(config_path, {}, valid_json=True),
            "error": "Workspace opencode.json must contain a top-level JSON object.",
            "orchestration_ready": False,
        }

    diagnostics = _build_config_diagnostics(config_path, raw_payload, valid_json=True)
    agent_payload = raw_payload.get("agent")
    if not isinstance(agent_payload, dict) or not agent_payload:
        diagnostics["error"] = "Workspace opencode.json must define a non-empty 'agent' object."
        diagnostics["orchestration_ready"] = False
    return diagnostics


def _validate_project_opencode_config(config_path: Path) -> dict:
    diagnostics = inspect_project_opencode_config(config_path)
    if diagnostics.get("error"):
        raise DaemonStartError(str(diagnostics["error"]))

    missing_agents = diagnostics.get("missing_agents", [])
    if missing_agents:
        raise DaemonStartError(
            "Workspace opencode.json is missing required orchestrator agents: " + ", ".join(missing_agents),
        )
    return diagnostics


def project_opencode_config_summary(project_absolute_path: str) -> dict:
    project_root = project_root_from_path(project_absolute_path)
    config_path = project_root / "opencode.json"
    return inspect_project_opencode_config(config_path)


def _docker_command(project_root: Path, allocated_port: int, opencode_binary: str, opencode_subcommand: str) -> list[str]:
    image = (getattr(settings, "OPENCODE_DAEMON_DOCKER_IMAGE", "") or "").strip()
    if not image:
        raise DaemonStartError("OPENCODE_DAEMON_DOCKER_IMAGE must be configured when OPENCODE_DAEMON_SANDBOX_MODE=docker.")

    container_workdir = (getattr(settings, "OPENCODE_DAEMON_CONTAINER_WORKDIR", "/workspace") or "/workspace").strip() or "/workspace"
    container_name = _docker_container_name(project_root, allocated_port)
    return [
        "docker",
        "run",
        "--rm",
        "--init",
        "--name",
        container_name,
        "-v",
        f"{project_root}:{container_workdir}",
        "-w",
        container_workdir,
        "-p",
        f"127.0.0.1:{allocated_port}:{allocated_port}",
        image,
        opencode_binary,
        opencode_subcommand,
        "--config",
        "opencode.json",
        "--port",
        str(allocated_port),
        "--hostname",
        "0.0.0.0",
    ]


def _docker_container_name(project_root: Path, allocated_port: int) -> str:
    digest = sha1(f"{project_root}:{allocated_port}".encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"opencode-daemon-{digest}"


def daemon_container_name(project_absolute_path: str, allocated_port: int) -> str:
    project_root = project_root_from_path(project_absolute_path)
    return _docker_container_name(project_root, allocated_port)


def start_opencode_daemon(project_absolute_path: str, allocated_port: int) -> subprocess.Popen:
    """
    Start an isolated OpenCode daemon for a project by injecting backend venv/bin
    into PATH and binding server to localhost + allocated port.
    """

    project_root = project_root_from_path(project_absolute_path)
    _ensure_project_opencode_config(project_root)
    config_summary = project_opencode_config_summary(project_absolute_path)
    venv_bin_path = _venv_bin_path(project_root)
    backend_venv = project_root / "backend" / "venv"

    env = os.environ.copy()
    original_path = env.get("PATH", "")
    if venv_bin_path:
        env["PATH"] = f"{venv_bin_path}{os.pathsep}{original_path}" if original_path else str(venv_bin_path)
        env["VIRTUAL_ENV"] = str(backend_venv)

    opencode_binary = settings.OPENCODE_BINARY_PATH
    opencode_subcommand = settings.OPENCODE_WEB_SUBCOMMAND
    sandbox_mode = (getattr(settings, "OPENCODE_DAEMON_SANDBOX_MODE", "host") or "host").strip().lower()

    if sandbox_mode == "docker":
        command = _docker_command(project_root, allocated_port, opencode_binary, opencode_subcommand)
    else:
        command = [
            opencode_binary,
            opencode_subcommand,
            "--config",
            "opencode.json",
            "--port",
            str(allocated_port),
            "--hostname",
            "127.0.0.1",
        ]

    logger.info(
        "Starting OpenCode daemon for project_root=%s port=%s config=%s default_agent=%s agents=%s warnings=%s",
        project_root,
        allocated_port,
        config_summary.get("config_path"),
        config_summary.get("default_agent") or "(unset)",
        ",".join(config_summary.get("available_agents", [])),
        "; ".join(config_summary.get("warnings", [])) or "none",
    )

    process = subprocess.Popen(
        command,
        cwd=project_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=False,
        start_new_session=True,
    )

    wait_for_daemon_ready(process, allocated_port, project_absolute_path=str(project_root))
    return process


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


def _kill_pid(pid: int, timeout_seconds: float) -> bool:
    if not is_daemon_running(pid):
        return True

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
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
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return True

    return not is_daemon_running(pid)


def _pids_listening_on_port(port: int) -> set[int]:
    try:
        output = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:  # noqa: BLE001
        return set()

    pids: set[int] = set()
    for raw in output.splitlines():
        value = raw.strip()
        if not value:
            continue
        try:
            pids.add(int(value))
        except ValueError:
            continue
    return pids


def is_port_reachable(port: int | None) -> bool:
    if not port:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _stop_port_bound_processes(port: int, timeout_seconds: float) -> bool:
    listener_pids = _pids_listening_on_port(port)
    if not listener_pids:
        return not is_port_reachable(port)

    all_stopped = True
    for listener_pid in listener_pids:
        if not _kill_pid(listener_pid, timeout_seconds=timeout_seconds):
            all_stopped = False
    return all_stopped and not is_port_reachable(port)


def _stop_docker_container(project_absolute_path: str, allocated_port: int) -> bool:
    sandbox_mode = (getattr(settings, "OPENCODE_DAEMON_SANDBOX_MODE", "host") or "host").strip().lower()
    if sandbox_mode != "docker":
        return True

    try:
        project_root = project_root_from_path(project_absolute_path)
    except Exception:  # noqa: BLE001
        return False

    container_name = _docker_container_name(project_root, allocated_port)
    result = subprocess.run(
        ["docker", "rm", "-f", container_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0 or not is_port_reachable(allocated_port)


def daemon_runtime_status(pid: int | None, allocated_port: int | None) -> dict:
    pid_alive = is_daemon_running(pid)
    port_reachable = is_port_reachable(allocated_port)
    health = daemon_health(allocated_port if port_reachable else None)
    return {
        "pid_alive": pid_alive,
        "port_reachable": port_reachable,
        "reachable": bool(health.get("reachable")) or port_reachable,
        "healthy": bool(health.get("healthy")),
        "busy": bool(health.get("busy")),
        "timed_out": bool(health.get("timed_out")),
        "state": health.get("state", "unreachable"),
        "version": health.get("version"),
        "running": pid_alive or port_reachable,
    }


def stop_opencode_daemon(
    pid: int | None,
    timeout_seconds: float = 8.0,
    allocated_port: int | None = None,
    project_absolute_path: str | None = None,
) -> bool:
    initial_status = daemon_runtime_status(pid, allocated_port)
    if not initial_status["running"]:
        return True

    stopped_by_pid = True
    if pid:
        stopped_by_pid = _kill_pid(pid, timeout_seconds=timeout_seconds)

    stopped_by_port = True
    if allocated_port:
        stopped_by_port = _stop_port_bound_processes(allocated_port, timeout_seconds=timeout_seconds)

    stopped_docker = True
    if allocated_port and project_absolute_path:
        stopped_docker = _stop_docker_container(project_absolute_path, allocated_port)

    final_status = daemon_runtime_status(pid, allocated_port)
    return stopped_by_pid and stopped_by_port and stopped_docker and not final_status["running"]


def daemon_health(port: int | None) -> dict:
    if not port:
        return {"reachable": False, "healthy": False, "busy": False, "state": "unreachable"}

    timeout_seconds = max(3, float(getattr(settings, "DAEMON_HEALTHCHECK_TIMEOUT_SECONDS", 15)))
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/global/health", timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        return {
            "reachable": True,
            "healthy": True,
            "busy": True,
            "timed_out": True,
            "state": "busy",
        }
    except (httpx.ConnectError, httpx.NetworkError):
        return {"reachable": False, "healthy": False, "busy": False, "state": "unreachable"}
    except Exception:  # noqa: BLE001
        return {"reachable": False, "healthy": False, "busy": False, "state": "unreachable"}

    healthy = bool(payload.get("healthy", False))
    return {
        "reachable": True,
        "healthy": healthy,
        "busy": bool(payload.get("busy", False)),
        "state": "healthy" if healthy else "unhealthy",
        "version": payload.get("version"),
    }

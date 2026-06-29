import os
import re
import shutil
import subprocess
import venv
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from ..models import Project, WorkspaceTarget


class ProvisioningError(Exception):
    """Raised when project provisioning fails."""


VALID_PROJECT_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
VALID_RELATIVE_PATH = re.compile(r"^[a-zA-Z0-9_./-]+$")
VALID_GITHUB_REMOTE_PREFIX = "https://github.com/"


@dataclass(frozen=True)
class WorkspaceTargetSpec:
    name: str
    role: str
    source_type: str
    relative_path: str
    remote_url: str = ""
    default_branch: str = ""
    is_primary: bool = False
    is_editable: bool = True


@dataclass(frozen=True)
class ProvisionWorkspaceRequest:
    path_owner_username: str
    project_name: str
    workspace_mode: str
    starter_template: str
    bootstrap_enabled: bool
    clone_remote_url: str = ""
    clone_branch: str = ""
    clone_target_name: str = ""
    clone_target_role: str = WorkspaceTarget.Role.CUSTOM
    custom_targets: tuple[WorkspaceTargetSpec, ...] = ()


@dataclass(frozen=True)
class ProvisionedWorkspace:
    path_owner_username: str
    project_root: Path
    workspace_mode: str
    starter_template: str
    bootstrap_enabled: bool
    setup_status: str
    targets: tuple[WorkspaceTargetSpec, ...]


def _validate_project_name(project_name: str) -> str:
    normalized = project_name.strip()
    if not normalized:
        raise ProvisioningError("Project name cannot be empty.")
    if not VALID_PROJECT_NAME.match(normalized):
        raise ProvisioningError(
            "Project name can only contain letters, numbers, underscores, and hyphens.",
        )
    return normalized


def normalize_path_owner_username(username: str) -> str:
    normalized = username.strip().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-.")
    if not normalized:
        raise ProvisioningError("Authenticated user does not have a valid filesystem-safe username.")
    return normalized


def _validate_git_remote(remote_url: str) -> str:
    normalized = remote_url.strip()
    if not normalized:
        raise ProvisioningError("Git remote URL cannot be empty.")
    if not normalized.startswith(VALID_GITHUB_REMOTE_PREFIX):
        raise ProvisioningError("Only HTTPS GitHub remotes are supported for managed workspace provisioning.")
    return normalized


def _resolve_env_paths() -> tuple[Path, Path]:
    managed_root_raw = settings.MANAGED_PROJECTS_ROOT
    template_path_raw = settings.GLOBAL_TEMPLATE_PATH

    if not managed_root_raw:
        raise ProvisioningError("MANAGED_PROJECTS_ROOT is not configured.")
    if not template_path_raw:
        raise ProvisioningError("GLOBAL_TEMPLATE_PATH is not configured.")

    managed_root = Path(managed_root_raw).expanduser().resolve()
    template_path = Path(template_path_raw).expanduser().resolve()

    if not template_path.exists() or not template_path.is_file():
        raise ProvisioningError("GLOBAL_TEMPLATE_PATH does not point to a valid file.")

    managed_root.mkdir(parents=True, exist_ok=True)
    return managed_root, template_path


def _resolve_managed_root() -> Path:
    managed_root_raw = settings.MANAGED_PROJECTS_ROOT
    if not managed_root_raw:
        raise ProvisioningError("MANAGED_PROJECTS_ROOT is not configured.")
    managed_root = Path(managed_root_raw).expanduser().resolve()
    managed_root.mkdir(parents=True, exist_ok=True)
    return managed_root


def managed_projects_root() -> Path:
    return _resolve_managed_root()


def managed_projects_host_root() -> Path | None:
    host_root_raw = (getattr(settings, "MANAGED_PROJECTS_HOST_ROOT", "") or "").strip()
    if not host_root_raw:
        return None
    return Path(host_root_raw).expanduser().resolve()


def managed_project_relative_path(path_owner_username: str, project_name: str) -> Path:
    normalized_owner = normalize_path_owner_username(path_owner_username)
    normalized_project_name = _validate_project_name(project_name)
    return Path(normalized_owner) / normalized_project_name


def host_path_from_runtime_path(runtime_path: str) -> str:
    host_root = managed_projects_host_root()
    if host_root is None:
        return ""

    runtime_root = managed_projects_root()
    resolved_runtime_path = Path(runtime_path).expanduser().resolve()
    try:
        relative_path = resolved_runtime_path.relative_to(runtime_root)
    except ValueError as error:
        raise ProvisioningError("Runtime path escapes MANAGED_PROJECTS_ROOT.") from error

    return str((host_root / relative_path).resolve())


def _venv_bin_directory(venv_path: Path) -> Path:
    unix_bin = venv_path / "bin"
    windows_bin = venv_path / "Scripts"
    if unix_bin.exists():
        return unix_bin
    if windows_bin.exists():
        return windows_bin
    raise ProvisioningError("Unable to locate virtual environment binary directory.")


def _safe_relative_path(raw_path: str, *, fallback_name: str) -> str:
    normalized = (raw_path or fallback_name).strip().strip("/")
    if not normalized:
        normalized = fallback_name
    if not VALID_RELATIVE_PATH.match(normalized):
        raise ProvisioningError(f"Invalid target path: {raw_path or fallback_name}")

    candidate = Path(normalized)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProvisioningError(f"Target path escapes project root: {normalized}")
    return candidate.as_posix()


def _git_clone(remote_url: str, destination: Path, branch: str = "") -> None:
    command = ["git", "clone"]
    if branch.strip():
        command.extend(["--branch", branch.strip()])
    command.extend([remote_url, str(destination)])
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _git_init(project_root: Path) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=project_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _create_backend_venv(project_root: Path, relative_path: str) -> None:
    backend_dir = project_root / relative_path
    venv_dir = backend_dir / "venv"
    venv.EnvBuilder(with_pip=True).create(str(venv_dir))

    venv_bin = _venv_bin_directory(venv_dir)
    pip_binary = venv_bin / ("pip.exe" if os.name == "nt" else "pip")
    if not pip_binary.exists():
        raise ProvisioningError("Failed to locate project-specific pip binary in backend venv.")

    subprocess.run(
        [str(pip_binary), "install", "django", "djangorestframework"],
        cwd=project_root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _write_template_if_missing(project_root: Path, template_path: Path) -> None:
    destination = project_root / "opencode.json"
    if destination.exists():
        return
    shutil.copy2(template_path, destination)


def _starter_targets(template: str) -> tuple[WorkspaceTargetSpec, ...]:
    if template == Project.StarterTemplate.FRONTEND:
        return (
            WorkspaceTargetSpec(
                name="frontend",
                role=WorkspaceTarget.Role.FRONTEND,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="frontend",
                is_primary=True,
                is_editable=True,
            ),
            WorkspaceTargetSpec(
                name="documents",
                role=WorkspaceTarget.Role.DOCS,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="documents",
                is_primary=False,
                is_editable=True,
            ),
        )

    if template == Project.StarterTemplate.BACKEND:
        return (
            WorkspaceTargetSpec(
                name="backend",
                role=WorkspaceTarget.Role.BACKEND,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="backend",
                is_primary=True,
                is_editable=True,
            ),
            WorkspaceTargetSpec(
                name="documents",
                role=WorkspaceTarget.Role.DOCS,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="documents",
                is_primary=False,
                is_editable=True,
            ),
        )

    if template == Project.StarterTemplate.MOBILE_BACKEND:
        return (
            WorkspaceTargetSpec(
                name="mobile",
                role=WorkspaceTarget.Role.MOBILE,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="mobile",
                is_primary=True,
                is_editable=True,
            ),
            WorkspaceTargetSpec(
                name="backend",
                role=WorkspaceTarget.Role.BACKEND,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="backend",
                is_primary=False,
                is_editable=True,
            ),
            WorkspaceTargetSpec(
                name="documents",
                role=WorkspaceTarget.Role.DOCS,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="documents",
                is_primary=False,
                is_editable=True,
            ),
        )

    if template == Project.StarterTemplate.DESKTOP_BACKEND:
        return (
            WorkspaceTargetSpec(
                name="desktop",
                role=WorkspaceTarget.Role.DESKTOP,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="desktop",
                is_primary=True,
                is_editable=True,
            ),
            WorkspaceTargetSpec(
                name="backend",
                role=WorkspaceTarget.Role.BACKEND,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="backend",
                is_primary=False,
                is_editable=True,
            ),
            WorkspaceTargetSpec(
                name="documents",
                role=WorkspaceTarget.Role.DOCS,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="documents",
                is_primary=False,
                is_editable=True,
            ),
        )

    return (
        WorkspaceTargetSpec(
            name="frontend",
            role=WorkspaceTarget.Role.FRONTEND,
            source_type=WorkspaceTarget.SourceType.SCAFFOLD,
            relative_path="frontend",
            is_primary=True,
            is_editable=True,
        ),
        WorkspaceTargetSpec(
            name="backend",
            role=WorkspaceTarget.Role.BACKEND,
            source_type=WorkspaceTarget.SourceType.SCAFFOLD,
            relative_path="backend",
            is_primary=False,
            is_editable=True,
        ),
        WorkspaceTargetSpec(
            name="documents",
            role=WorkspaceTarget.Role.DOCS,
            source_type=WorkspaceTarget.SourceType.SCAFFOLD,
            relative_path="documents",
            is_primary=False,
            is_editable=True,
        ),
    )


def _clone_target_name(remote_url: str, fallback: str) -> str:
    sanitized = remote_url.rstrip("/").split("/")[-1]
    if sanitized.endswith(".git"):
        sanitized = sanitized[:-4]
    sanitized = sanitized or fallback
    return re.sub(r"[^a-zA-Z0-9_-]", "-", sanitized)


def _normalize_target_specs(request: ProvisionWorkspaceRequest) -> tuple[WorkspaceTargetSpec, ...]:
    if request.workspace_mode == Project.WorkspaceMode.STARTER:
        return _starter_targets(request.starter_template)

    if request.workspace_mode == Project.WorkspaceMode.ACTIVE_CLONE:
        remote_url = _validate_git_remote(request.clone_remote_url)
        target_name = request.clone_target_name.strip() or _clone_target_name(remote_url, request.project_name)
        return (
            WorkspaceTargetSpec(
                name=target_name,
                role=request.clone_target_role or WorkspaceTarget.Role.CUSTOM,
                source_type=WorkspaceTarget.SourceType.GIT_CLONE,
                relative_path=".",
                remote_url=remote_url,
                default_branch=request.clone_branch.strip(),
                is_primary=True,
                is_editable=True,
            ),
        )

    if request.workspace_mode == Project.WorkspaceMode.REFERENCE_CLONE:
        remote_url = _validate_git_remote(request.clone_remote_url)
        target_name = request.clone_target_name.strip() or _clone_target_name(remote_url, "reference")
        return (
            WorkspaceTargetSpec(
                name=target_name,
                role=WorkspaceTarget.Role.REFERENCE,
                source_type=WorkspaceTarget.SourceType.GIT_CLONE,
                relative_path=f"references/{_safe_relative_path(target_name, fallback_name='reference')}",
                remote_url=remote_url,
                default_branch=request.clone_branch.strip(),
                is_primary=False,
                is_editable=False,
            ),
            WorkspaceTargetSpec(
                name="documents",
                role=WorkspaceTarget.Role.DOCS,
                source_type=WorkspaceTarget.SourceType.SCAFFOLD,
                relative_path="documents",
                is_primary=False,
                is_editable=True,
            ),
        )

    normalized_targets: list[WorkspaceTargetSpec] = []
    for index, target in enumerate(request.custom_targets, start=1):
        fallback_name = re.sub(r"[^a-zA-Z0-9_-]", "-", target.name.strip()) or f"target-{index}"
        relative_path = _safe_relative_path(target.relative_path, fallback_name=fallback_name)
        remote_url = ""
        if target.source_type == WorkspaceTarget.SourceType.GIT_CLONE:
            remote_url = _validate_git_remote(target.remote_url)
        normalized_targets.append(
            WorkspaceTargetSpec(
                name=target.name.strip(),
                role=target.role,
                source_type=target.source_type,
                relative_path=relative_path,
                remote_url=remote_url,
                default_branch=target.default_branch.strip(),
                is_primary=target.is_primary,
                is_editable=target.is_editable,
            ),
        )

    if normalized_targets and not any(target.is_primary for target in normalized_targets):
        first_editable_index = next(
            (index for index, target in enumerate(normalized_targets) if target.is_editable and target.role != WorkspaceTarget.Role.REFERENCE),
            0,
        )
        target = normalized_targets[first_editable_index]
        normalized_targets[first_editable_index] = WorkspaceTargetSpec(
            name=target.name,
            role=target.role,
            source_type=target.source_type,
            relative_path=target.relative_path,
            remote_url=target.remote_url,
            default_branch=target.default_branch,
            is_primary=True,
            is_editable=target.is_editable,
        )

    return tuple(normalized_targets)


def _setup_status_for_request(request: ProvisionWorkspaceRequest) -> str:
    if request.bootstrap_enabled or request.workspace_mode == Project.WorkspaceMode.CUSTOM:
        return Project.SetupStatus.DRAFT
    return Project.SetupStatus.READY


def _create_target_directory(project_root: Path, relative_path: str) -> Path:
    target_path = (project_root / relative_path).resolve()
    try:
        target_path.relative_to(project_root)
    except ValueError as error:
        raise ProvisioningError(f"Target path escapes project root: {relative_path}") from error
    target_path.mkdir(parents=True, exist_ok=False)
    return target_path


def provision_project_structure(request: ProvisionWorkspaceRequest) -> ProvisionedWorkspace:
    normalized_owner = normalize_path_owner_username(request.path_owner_username)
    normalized_name = _validate_project_name(request.project_name)
    managed_root, template_path = _resolve_env_paths()

    owner_root = (managed_root / normalized_owner).resolve()
    project_root = (owner_root / normalized_name).resolve()
    try:
        project_root.relative_to(managed_root)
    except ValueError as error:
        raise ProvisioningError("Resolved project root escapes MANAGED_PROJECTS_ROOT.") from error
    if project_root.exists():
        raise ProvisioningError(f"Project '{normalized_name}' already exists.")

    normalized_targets = _normalize_target_specs(request)
    setup_status = _setup_status_for_request(request)

    try:
        owner_root.mkdir(parents=True, exist_ok=True)
        project_root.mkdir(parents=False, exist_ok=False)

        if request.workspace_mode == Project.WorkspaceMode.ACTIVE_CLONE:
            primary_target = normalized_targets[0]
            _git_clone(primary_target.remote_url, project_root, primary_target.default_branch)
            _write_template_if_missing(project_root, template_path)
        else:
            _write_template_if_missing(project_root, template_path)
            for target in normalized_targets:
                target_path = _create_target_directory(project_root, target.relative_path)
                if target.source_type == WorkspaceTarget.SourceType.GIT_CLONE:
                    shutil.rmtree(target_path)
                    _git_clone(target.remote_url, target_path, target.default_branch)

            if request.workspace_mode == Project.WorkspaceMode.STARTER:
                _git_init(project_root)

        for target in normalized_targets:
            if target.role == WorkspaceTarget.Role.BACKEND and target.source_type != WorkspaceTarget.SourceType.GIT_CLONE:
                _create_backend_venv(project_root, target.relative_path)
    except subprocess.CalledProcessError as error:
        shutil.rmtree(project_root, ignore_errors=True)
        raise ProvisioningError(error.stderr.strip() or str(error)) from error
    except Exception:
        shutil.rmtree(project_root, ignore_errors=True)
        raise

    return ProvisionedWorkspace(
        path_owner_username=normalized_owner,
        project_root=project_root,
        workspace_mode=request.workspace_mode,
        starter_template=request.starter_template,
        bootstrap_enabled=request.bootstrap_enabled,
        setup_status=setup_status,
        targets=normalized_targets,
    )
